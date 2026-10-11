"""Session-owned task surfaces. Pi owns reasoning; PTYs own independent processes."""
import codecs
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from wakecore.kernel.service import Response

LIMIT = 128 * 1024
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)')


def preview_url(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError('请输入有效的网页地址。')
    parsed = urlsplit(value.strip())
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('预览只接受不含登录凭据的 HTTP(S) 地址。')
    try:
        parsed.port
    except ValueError:
        raise ValueError('网址端口无效。') from None
    return parsed.geturl()


class TaskWorkspace:
    def __init__(self, store, context, redact=lambda value: value, runtime_dir=None):
        self.store, self.context, self.redact = store, context, redact
        self.worker = Path(runtime_dir or Path(__file__).resolve().parents[6] / "apps" / "agent-runtime") / "terminal-worker.mjs"
        self.lock = threading.RLock()
        self.running = {}
        self.stopping = False
        for item in store.list('terminal', states=('running', 'starting')):
            store.save('terminal', item['id'], {**item, 'state': 'interrupted', 'error': '服务已重启；进程没有自动重跑。'})

    def state(self, context=None):
        context = context or self.context()
        session_id = context.get('session_id')
        return {**context, 'available': os.name == 'posix',
                'terminals': [self.public(item) for item in self.store.list('terminal', limit=100) if item.get('session_id') == session_id],
                'previews': [item for item in self.store.list('preview', limit=60) if item.get('session_id') == session_id],
                'concurrency': {'terminal_limit': 6, 'desktop': 'one_action_per_device'}}

    @staticmethod
    def public(item):
        return {k: v for k, v in item.items() if k not in ('request_digest', 'request_id')}

    def owned(self, kind, key, context):
        item = self.store.get(kind, key)
        if not item or item.get('session_id') != context.get('session_id'):
            raise ValueError('这个资源不属于当前会话；请返回原对话。')
        return item

    def command(self, body):
        context = self.context()
        if not context.get('session_id'):
            raise ValueError('请先启动 Pi 会话，再打开任务工作区。')
        if body.get('session_id') and body['session_id'] != context['session_id']:
            raise ValueError('会话已切换；本次操作未执行。')
        op = body.get('operation')
        if op == 'terminal_create':
            return self.create(body, context)
        if op in ('terminal_read', 'terminal_input', 'terminal_resize', 'terminal_stop', 'terminal_remove'):
            item = self.owned('terminal', body.get('id'), context)
            with self.lock:
                live = self.running.get(item['id'])
                if op == 'terminal_read':
                    cursor = body.get('cursor', 0)
                    if type(cursor) is not int or cursor < 0:
                        raise ValueError('终端读取位置无效。')
                    if not live:
                        return {'terminal': self.public(item), 'output': '', 'cursor': 0, 'reset': True, 'urls': []}
                    start, end = live['start'], live['end']
                    reset = not start <= cursor <= end
                    output = live['output'][max(0, cursor - start):] if not reset else live['output']
                    plain = ANSI.sub('', live['output'])
                    urls = []
                    for found in re.findall(r'https?://[^\s<>"\x00-\x20]+', plain):
                        try:
                            url = preview_url(found.rstrip(').,;'))
                            if url not in urls:
                                urls.append(url)
                        except ValueError:
                            pass
                    return {'terminal': self.public(item), 'output': self.redact(output), 'cursor': end, 'reset': reset, 'urls': urls[-8:]}
                if op == 'terminal_remove':
                    if item['state'] == 'running':
                        raise ValueError('先停止终端，再移除记录。')
                    self.running.pop(item['id'], None)
                    self.store.remove('terminal', item['id'])
                    return {'removed': True}
                if op == 'terminal_stop':
                    if live and live['process'].poll() is None:
                        live['stopped'] = True
                        self._signal(live, signal.SIGTERM)
                        threading.Thread(target=self._reap, args=(live,), daemon=True).start()
                    return self.public(self.store.get('terminal', item['id']))
                if not live or live['process'].poll() is not None or live['fd'] is None:
                    raise ValueError('终端已经结束；可以打开一个新终端。')
                if op == 'terminal_input':
                    data, key = body.get('data'), body.get('input_id')
                    if not isinstance(data, str) or len(data) > 32768 or not isinstance(key, str) or not 1 <= len(key) <= 160:
                        raise ValueError('终端输入无效。')
                    digest = hashlib.sha256(data.encode()).hexdigest()
                    if key in live['inputs']:
                        if live['inputs'][key] != digest:
                            raise ValueError('同一输入请求不能包含不同内容。')
                        return {'accepted': True, 'duplicate': True}
                    encoded = self._frame({'type': 'input', 'data': data})
                    if len(live['input']) + len(encoded) > 65536:
                        raise ValueError('终端尚未处理完输入，请稍后继续。')
                    live['input'].extend(encoded)
                    live['inputs'][key] = digest
                    if len(live['inputs']) > 256:
                        live['inputs'].pop(next(iter(live['inputs'])))
                    return {'accepted': True}
                self._dimensions(body.get('cols'), body.get('rows'))
                live['input'].extend(self._frame({'type': 'resize', 'cols': body['cols'], 'rows': body['rows']}))
                return {'resized': True}
        if op == 'preview_open':
            url = preview_url(body.get('url'))
            for old in self.store.list('preview'):
                if old.get('session_id') == context['session_id'] and old.get('url') == url:
                    return old
            title = self.redact(str(body.get('title') or urlsplit(url).netloc))[:100]
            return self.store.save('preview', self.store.new_id('preview'), {'session_id': context['session_id'], 'url': url, 'title': title, 'state': 'ready'})
        if op == 'preview_remove':
            item = self.owned('preview', body.get('id'), context)
            self.store.remove('preview', item['id'])
            return {'removed': True}
        raise ValueError('无法识别的工作区操作。')

    @staticmethod
    def _dimensions(cols, rows):
        if type(cols) is not int or type(rows) is not int or not 20 <= cols <= 300 or not 4 <= rows <= 100:
            raise ValueError('终端尺寸超出范围。')

    @staticmethod
    def _frame(value):
        return (json.dumps(value, ensure_ascii=False) + '\n').encode()

    def create(self, body, context):
        if os.name != 'posix':
            raise ValueError('当前执行节点尚不支持原生终端。')
        if not shutil.which('node') or not self.worker.exists():
            raise ValueError('终端运行时未安装。')
        cols, rows = body.get('cols', 90), body.get('rows', 24)
        self._dimensions(cols, rows)
        request_id = body.get('request_id')
        command = body.get('command')
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 160:
            raise ValueError('启动终端需要唯一请求标识。')
        if command is not None and (not isinstance(command, str) or not command.strip() or len(command) > 16000 or '\0' in command):
            raise ValueError('终端命令无效。')
        workspace = str(Path(context['workspace']).resolve(strict=True))
        request_digest = hashlib.sha256(json.dumps([workspace, command, context['session_id']]).encode()).hexdigest()
        receipt_id = 'terminal-request-' + hashlib.sha256((context['session_id'] + ':' + request_id).encode()).hexdigest()
        with self.lock:
            receipt = self.store.get('terminal_request', receipt_id)
            if receipt:
                if receipt['request_digest'] != request_digest:
                    raise ValueError('同一启动请求不能执行不同命令。')
                old = self.store.get('terminal', receipt['terminal_id'])
                if old:
                    return self.public(old)
                raise ValueError('该请求已经处理或启动结果待核验；没有重复执行。')
            for old in self.store.list('terminal', limit=500):
                if old.get('request_id') == request_id and old.get('session_id') == context['session_id']:
                    if old['request_digest'] != request_digest:
                        raise ValueError('同一启动请求不能执行不同命令。')
                    return self.public(old)
            if self.stopping or sum(v['process'].poll() is None for v in self.running.values()) >= 6:
                raise ValueError('已有 6 个终端在运行；先停止不需要的进程。')
            shell = '/bin/zsh' if Path('/bin/zsh').exists() else '/bin/bash'
            args = ['-f', '-c', command] if command is not None else ['-f', '-i']
            env = {k: v for k, v in os.environ.items() if not any(part in k.upper() for part in ('API_KEY', 'AUTH_TOKEN', 'ACCESS_TOKEN', 'SECRET', 'PASSWORD', 'MORNING_', 'WAKECORE_', 'NODE_OPTIONS'))}
            env.update(TERM='xterm-256color', COLORTERM='truecolor', HISTFILE='/dev/null', PS1='%~ ❯ ' if shell.endswith('zsh') else '\\w $ ')
            key = self.store.new_id('terminal')
            item = {'session_id': context['session_id'], 'session_path': context.get('session_path'), 'workspace': workspace,
                    'state': 'starting', 'title': self.redact(str(body.get('title') or ('后台命令' if command else '终端')))[:100],
                    'request_id': request_id, 'request_digest': request_digest, 'created': time.time(), 'exit_code': None}
            # Persist intent before spawning. An interrupted launch is never replayed.
            self.store.save('terminal_request', receipt_id, {'terminal_id': key, 'request_digest': request_digest, 'state': 'accepted'})
            self.store.save('terminal', key, item)
            process = None
            try:
                process = subprocess.Popen([shutil.which('node'), str(self.worker)], cwd=workspace,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           env=env, start_new_session=True, close_fds=True, bufsize=0)
                process.stdin.write(self._frame({'type': 'start', 'shell': shell, 'args': args, 'cwd': workspace, 'cols': cols, 'rows': rows}))
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    if not selector.select(5):
                        raise ValueError('终端启动超时。')
                ready = json.loads(process.stdout.readline(8192))
                if ready.get('type') != 'ready':
                    raise ValueError('原生终端未能启动，请检查 node-pty 安装。')
                os.set_blocking(process.stdout.fileno(), False)
                os.set_blocking(process.stdin.fileno(), False)
            except BaseException:
                if process:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                    process.stdin.close()
                    process.stdout.close()
                self.store.save('terminal', key, {**item, 'state': 'failed', 'error': '终端未能启动；没有自动重试。'})
                raise
            item['state'] = 'running'
            live = {'fd': process.stdout.fileno(), 'exit_code': None, 'process': process, 'output': '', 'start': 0, 'end': 0, 'inputs': {}, 'input': bytearray(), 'stopped': False}
            try:
                item = self.store.save('terminal', key, item)
            except BaseException:
                self._signal(live, signal.SIGKILL)
                process.wait(timeout=3)
                process.stdin.close()
                process.stdout.close()
                raise
            self.running[key] = live
            threading.Thread(target=self._read, args=(key, live), name='morning-terminal', daemon=True).start()
            self.store.event('terminal.started', key, {'session_id': context['session_id']})
            return self.public(item)

    def _read(self, key, live):
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        process, fd = live['process'], live['fd']
        buffered, writing = '', False
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(fd, selectors.EVENT_READ)
                while True:
                    with self.lock:
                        pending = bytes(live['input'][:16384])
                    if pending and not writing:
                        selector.register(process.stdin, selectors.EVENT_WRITE)
                        writing = True
                    elif not pending and writing:
                        selector.unregister(process.stdin)
                        writing = False
                    events = selector.select(.08)
                    if not events and process.poll() is not None:
                        break
                    ended = False
                    for ready, mask in events:
                        if mask & selectors.EVENT_WRITE:
                            try:
                                written = os.write(process.stdin.fileno(), pending)
                                with self.lock:
                                    del live['input'][:written]
                            except BlockingIOError:
                                pass
                            except BrokenPipeError:
                                ended = True
                            continue
                        try:
                            chunk = os.read(fd, 65536)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            ended = True
                            break
                        buffered += decoder.decode(chunk)
                        if len(buffered) > 1024 * 1024:
                            raise ValueError('终端数据帧过大。')
                        while '\n' in buffered:
                            line, buffered = buffered.split('\n', 1)
                            message = json.loads(line)
                            if message['type'] == 'data':
                                text = message['data']
                                with self.lock:
                                    live['end'] += len(text)
                                    live['output'] = (live['output'] + text)[-LIMIT:]
                                    live['start'] = live['end'] - len(live['output'])
                            elif message['type'] == 'exit':
                                live['exit_code'] = message.get('exitCode', 1)
                    if ended:
                        break
        except (OSError, ValueError, KeyError):
            self._signal(live, signal.SIGTERM)
        finally:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._signal(live, signal.SIGTERM)
                self._reap(live)
            with self.lock:
                process.stdout.close()
                process.stdin.close()
                live['fd'] = None
                item = self.store.get('terminal', key)
                if item:
                    exit_code = live['exit_code']
                    state = 'cancelled' if live['stopped'] else 'completed' if exit_code == 0 else 'failed'
                    self.store.save('terminal', key, {**item, 'state': state, 'exit_code': exit_code})
                    self.store.event('terminal.' + state, key, {'exit_code': exit_code})

    @staticmethod
    def _signal(live, sig):
        try:
            live['process'].send_signal(sig)
        except ProcessLookupError:
            pass

    def _reap(self, live):
        try:
            live['process'].wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._signal(live, signal.SIGKILL)
            live['process'].wait(timeout=3)

    def close(self):
        with self.lock:
            self.stopping = True
            active = [live for live in self.running.values() if live['process'].poll() is None]
            for live in active:
                live['stopped'] = True
                self._signal(live, signal.SIGTERM)
        for live in active:
            self._reap(live)


def mount_workspace(api, workspace):
    def get(req, principal, args):
        return Response(200, workspace.state())

    def post(req, principal, args):
        try:
            return Response(200, workspace.command(req.json()))
        except (ValueError, OSError) as exc:
            return Response(422, {'error': {'code': 'workspace_operation_failed', 'message': workspace.redact(str(exc))}})
    api._add('GET', r'/pulse/workspace', get)
    api._add('POST', r'/pulse/workspace', post)
