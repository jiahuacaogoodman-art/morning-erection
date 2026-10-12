"""Concurrent native Pi runtimes; no replacement agent loop or conversation format.

Each loaded session keeps its own NativePiHost and NativeConversation. The console
selects a view, not a different conversation inside a busy Pi process.
"""
import json
import os
import threading
import time
import uuid
from collections import ChainMap, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from wakecore.adapters.agent_runtime.native import NativePiError, NativePiHost

from .native_agent import NativeConversation


class SessionBus:
    """Lightweight in-process message bus for Pi session coordination.
    Modeled on AutoGen GroupChat message structure: each message has a sender,
    content, and optional role. Bounded at 200 messages; oldest drop first."""
    MAX = 200

    def __init__(self):
        self._messages = deque(maxlen=self.MAX)
        self._lock = threading.RLock()

    def post(self, sender_session_id, role, content, metadata=None):
        import time, uuid
        msg = {"id": uuid.uuid4().hex[:16], "sender_session_id": sender_session_id,
               "role": role, "content": content, "metadata": metadata or {},
               "at": time.time()}
        with self._lock:
            self._messages.append(msg)
        return msg

    def read(self, since=0, limit=20, role=None):
        with self._lock:
            msgs = [m for m in self._messages if m["at"] > since and (role is None or m["role"] == role)]
        return msgs[-limit:]

    def public(self):
        return self.read(limit=50)


class NativeSessions:
    def __init__(self, host):
        self.base = host
        self.operations = threading.RLock()
        self.lock = threading.RLock()
        self.local = threading.local()
        self.listeners = []
        self.tool_factory = None
        self.root_selection = host.selection_path
        self.registry_path = host.state_dir / 'pi' / 'runtime-sessions.json'
        self.directory = host.state_dir / 'pi' / 'runtime-state'
        self.limit, self.metadata = 4, {}
        self.bus = SessionBus()
        if self.registry_path.exists():
            try:
                saved = json.loads(self.registry_path.read_text())
                self.limit = max(2, min(8, int(saved.get('limit', 4))))
                entries = saved.get('sessions', {})
                self.metadata = {sid: item for sid, item in entries.items() if isinstance(item, dict) and isinstance(item.get('selection'), str) and Path(item['selection']).name == item['selection'] and isinstance(item.get('slot'), str)} if isinstance(entries, dict) else {}
            except (ValueError, OSError, TypeError):
                pass
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        initial = uuid.uuid4().hex
        host.selection_path = self.directory / (initial + '.json')
        if self.root_selection.exists():
            try:
                self._write(host.selection_path, json.loads(self.root_selection.read_text()))
            except (ValueError, OSError):
                pass
        self.slots = {}
        self.current = self._attach(initial, host)
        if host.selection_path.exists():
            saved = json.loads(host.selection_path.read_text())
            self.current['hint_path'] = saved.get('path')
            self.current['hint_id'] = saved.get('session_id')
            if not self.current['hint_id'] and saved.get('path') and Path(saved['path']).is_file():
                try:
                    with open(saved['path']) as stream:
                        self.current['hint_id'] = json.loads(stream.readline()).get('id')
                except (ValueError, OSError):
                    pass
        self.stopping = False

    @staticmethod
    def _write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix('.tmp')
        with open(os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), 'w') as stream:
            json.dump(data, stream, ensure_ascii=False)
        temporary.replace(path)

    def _save(self):
        with self.lock:
            self._write(self.registry_path, {'limit': self.limit, 'sessions': self.metadata})

    def _attach(self, key, host):
        conversation = NativeConversation(host)
        slot = {'key': key, 'conversation': conversation, 'running': False, 'calls': 0, 'last_used': time.time(), 'created': time.time(), 'state': {}}
        self.slots[key] = slot
        host.listeners.append(lambda event: self._event(slot, event))
        conversation.on_tool_event = self._on_tool_event
        if self.tool_factory:
            self._wire_tools(slot)
        return slot

    def _on_tool_event(self, kind, payload):
        platform = getattr(self.base, 'platform', None)
        if platform and hasattr(platform, 'store'):
            platform.store.event(kind, payload.get("tool_call_id", ""), payload)

    def _wire_tools(self, slot):
        handler = self.tool_factory()
        def call(name, params):
            with self.use(slot):
                return handler(name, params)
        slot['conversation'].host.tool_handler = call

    def configure_tools(self, factory):
        self.tool_factory = factory
        for slot in list(self.slots.values()):
            self._wire_tools(slot)

    def add_listener(self, listener):
        self.listeners.append(listener)

    @contextmanager
    def use(self, slot):
        old = getattr(self.local, 'slot', None)
        self.local.slot = slot
        try:
            yield slot['conversation']
        finally:
            self.local.slot = old

    @property
    def slot(self):
        return getattr(self.local, 'slot', None) or self.current

    @property
    def host(self):
        return self.slot['conversation'].host

    @property
    def exports(self):
        return ChainMap(self.slot['conversation'].exports, *(slot['conversation'].exports for slot in self.slots.values() if slot is not self.slot))

    def _event(self, slot, event):
        kind = event.get('type')
        if kind == 'agent_start':
            slot['running'] = True
        elif kind in ('agent_end', 'agent_settled', 'process_ended'):
            slot['running'] = False
        # Never issue an RPC inside the reader callback that must deliver its reply.
        with self.use(slot):
            for listener in tuple(self.listeners):
                listener(event)

    def _capture(self, slot, state=None):
        host = slot['conversation'].host
        if not host.agent_ready:
            return {}
        state = state or host.rpc('get_state', timeout=5)
        previous_state = slot['state']
        slot['state'] = state
        host.remember_session(state.get('sessionFile'), state=state, workspace=host.info.get('workspace', str(host.workspace)))
        sid = state['sessionId']
        with self.lock:
            # A native fork/new-session can change the session owned by this slot.
            for old, item in list(self.metadata.items()):
                if item.get('slot') == slot['key'] and old != sid:
                    archived = uuid.uuid4().hex
                    selection = archived + '.json'
                    self.metadata[old] = {**item, 'slot': archived, 'selection': selection}
                    if not item.get('path') or not Path(item['path']).is_file():
                        model = previous_state.get('model') or {}
                        self._write(self.directory / selection, {'path': item.get('path'), 'empty': True, 'session_id': old, 'workspace': item['cwd'],
                            'model': {key: model[key] for key in ('provider', 'id') if key in model}, 'thinking_level': previous_state.get('thinkingLevel'), 'name': item.get('name')})
            path = Path(state['sessionFile']) if state.get('sessionFile') else None
            updated = path.stat().st_mtime if path and path.is_file() else slot['created']
            metadata = {'slot': slot['key'], 'id': sid, 'path': state.get('sessionFile'), 'cwd': host.info.get('workspace', str(host.workspace)),
                                  'name': state.get('sessionName'), 'firstMessage': getattr(host, 'current_prompt', ''),
                                  'modified': datetime.fromtimestamp(updated, timezone.utc).isoformat(), 'selection': host.selection_path.name}
            changed = self.metadata.get(sid) != metadata
            self.metadata[sid] = metadata
            if slot is self.current and host.selection_path.exists():
                saved = json.loads(host.selection_path.read_text())
                try:
                    existing = json.loads(self.root_selection.read_text())
                except (OSError, ValueError):
                    existing = None
                if existing != saved:
                    self._write(self.root_selection, saved)
            if changed:
                self._save()
        return state

    def _busy(self, slot, include_io=False):
        host = slot['conversation'].host
        if slot['calls'] or slot['running'] or host.pending_ui or include_io and (host.pending_control or host.pending):
            return True
        return bool(host.agent_ready and not host.control('context', timeout=5).get('isIdle'))

    def _capacity(self):
        alive = [slot for slot in self.slots.values() if slot['conversation'].host.process and slot['conversation'].host.process.poll() is None]
        if len(alive) < self.limit:
            return
        candidates = sorted((slot for slot in alive if slot is not self.current and slot is not self.slot), key=lambda slot: slot['last_used'])
        for slot in candidates:
            if not self._busy(slot, include_io=True):
                self._capture(slot)
                slot['conversation'].host.close()
                self.slots.pop(slot['key'], None)
                return
        raise NativePiError(f'已有 {self.limit} 个 Pi 会话在使用运行时；先停止一个任务，或在设置中提高并行上限。')

    def _spawn(self, *, path=None, metadata=None, fresh=False):
        self._capacity()
        key = metadata['slot'] if metadata else uuid.uuid4().hex
        selection = self.directory / (metadata['selection'] if metadata else key + '.json')
        if metadata and (not path or not Path(path).is_file()):
            try:
                saved = json.loads(selection.read_text())
                if not saved.get('empty') or saved.get('session_id') != metadata['id']:
                    raise ValueError('missing native session')
            except (ValueError, OSError):
                raise NativePiError('原生会话文件已不存在；没有回退到其他会话。') from None
        host = NativePiHost(runtime_path=self.base.runtime_path, workspace=(metadata or {}).get('cwd', self.host.info.get('workspace', str(self.host.workspace))),
                            state_dir=self.base.state_dir, gateways=self.base.gateways, agent_dir=self.base.agent_dir, selection_path=selection)
        host.platform_tools = self.base.platform_tools
        host.watch_proposal_schema = self.base.watch_proposal_schema
        slot = self._attach(key, host)
        try:
            host.start(session_file=path if path and Path(path).is_file() else None, fresh=fresh)
            host.control('start_agent', timeout=120)
            self._capture(slot)
            return slot
        except BaseException:
            host.close()
            self.slots.pop(key, None)
            raise

    def find(self, *, session_id=None, path=None, load=True):
        for slot in list(self.slots.values()):
            state = slot['state']
            if session_id and (state.get('sessionId') == session_id or not state and slot.get('hint_id') == session_id) or path and (state.get('sessionFile') == path or not state and slot.get('hint_path') == path):
                return slot
        entry = self.metadata.get(session_id) if session_id else next((item for item in self.metadata.values() if item.get('path') == path), None)
        if not load:
            return None
        if entry:
            return self._spawn(path=entry.get('path'), metadata=entry)
        if path and Path(path).is_file():
            return self._spawn(path=path)
        raise NativePiError('会话尚未载入或已不存在，请从会话列表重新选择。')

    @contextmanager
    def request_scope(self, session_id=None):
        if session_id:
            slot = self.find(session_id=session_id, load=False)
            if slot is None:
                with self.operations:
                    if not self.current['state'] and self.current['conversation'].host.agent_ready:
                        self._capture(self.current)
                    slot = self.find(session_id=session_id)
        else:
            slot = self.slot
        slot['last_used'] = time.time()
        with self.use(slot):
            yield

    def snapshot(self):
        slot = self.slot
        result = slot['conversation'].snapshot()
        if result.get('state'):
            self._capture(slot, result['state'])
        result['session_bus'] = self.bus.public()
        result['runtime_sessions'] = self.activities()
        result['runtime_limit'] = self.limit
        result['runtime_busy'] = bool(slot['running'] or slot['calls'])
        return result

    def activities(self):
        result = []
        for slot in list(self.slots.values()):
            state, host = slot['state'], slot['conversation'].host
            if not state:
                continue
            running = slot['running'] or bool(slot['calls'])
            result.append({'id': state['sessionId'], 'path': state.get('sessionFile'), 'name': state.get('sessionName') or getattr(host, 'current_prompt', '') or '新会话',
                           'running': running, 'attention': bool(host.pending_ui), 'loaded': host.agent_ready, 'current': slot is self.slot})
        return result

    def sessions(self):
        history = self.host.control('sessions')
        active = {item['id']: item for item in self.activities()}
        records = {item['id']: item for item in history}
        for sid, item in self.metadata.items():
            if sid not in records:
                records[sid] = {**item, 'messageCount': 0}
        return [{**item, **{k: v for k, v in active.get(sid, {}).items() if k in ('running', 'attention', 'loaded')}} for sid, item in records.items()]

    def prompt(self, body, key=None, prepare=None):
        slot = self.slot
        with self.operations:
            if prepare:
                prepare()
            slot['calls'] += 1
        try:
            slot['conversation'].host.control('start_agent')
            self._capture(slot)
            result = slot['conversation'].prompt(body, key)
            self._capture(slot)
            return result
        finally:
            slot['calls'] -= 1

    def command(self, command, params, prepare=None):
        slot = self.slot
        if command in ('new_session', 'switch_session'):
            with self.operations:
                self._capture(slot)
                if command == 'switch_session':
                    target = self.find(session_id=params.get('sessionId'), path=params.get('sessionPath'))
                    self.current = target
                else:
                    self.current = self._spawn(fresh=True)
                state = self._capture(self.current)
                return {'sessionId': state['sessionId'], 'sessionFile': state.get('sessionFile')}
        active = command in ('bash', 'compact', 'prompt', 'steer', 'follow_up')
        with self.operations:
            if prepare:
                prepare()
            slot['calls'] += active
        try:
            result = slot['conversation'].command(command, params)
            if command in ('fork', 'clone', 'set_session_name'):
                state = self._capture(slot)
                result = {**result, 'sessionId': state.get('sessionId')}
            return result
        finally:
            slot['calls'] -= active

    def assert_idle(self):
        for slot in list(self.slots.values()):
            if self._busy(slot):
                raise NativePiError('还有 Pi 会话正在执行；请先停止或等待完成，再修改共用配置。')

    def sync_gateways(self):
        for slot in list(self.slots.values()):
            host = slot['conversation'].host
            if host is not self.host:
                host.update_gateways()

    def stop_session(self, session_id):
        slot = self.find(session_id=session_id, load=False) if session_id else self.slot
        if not slot:
            return {'stopped': False}
        host = slot['conversation'].host
        for request_id in list(host.pending_ui):
            try:
                host.answer_ui(request_id, {'cancelled': True})
            except NativePiError:
                pass
        queued = host.rpc('clear_queue')
        host.rpc('abort')
        host.rpc('abort_bash')
        return {'stopped': True, 'sessionId': session_id, 'queued': queued}

    def set_limit(self, value):
        if type(value) is not int or not 2 <= value <= 8:
            raise NativePiError('并行会话上限需要是 2 到 8。')
        if sum(self._busy(slot) for slot in list(self.slots.values())) > value:
            raise NativePiError('正在运行的会话超过这个上限；请先停止部分任务。')
        self.limit = value
        self._save()
        return {'limit': value}

    def safe_json(self, value):
        return self.slot['conversation'].safe_json(value)

    def close(self):
        self.stopping = True
        for slot in list(self.slots.values()):
            try:
                self._capture(slot)
            except NativePiError:
                pass
            slot['conversation'].host.close()
