"""Opt-in acceptance with the saved Gateway, real Pi runtimes and real PTYs.

The source credentials/session are read-only. All work runs in a temporary workspace.
--computer requires the disposable dev.morning.acceptance fixture app to be open;
its exact action is approved through the temporary console, never automatically.
"""
import argparse
import json
import tempfile
import threading
import time
import uuid
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from wakecore.adapters.agent_runtime.credentials import MacKeychain
from wakecore.adapters.agent_runtime.native import NativePiHost
from wakecore.app.bootstrap import build
from wakecore.app.pulse.gateways import GatewayStore
from wakecore.app.pulse.server import LocalPulseApp

ROOT = Path(__file__).resolve().parents[1]


class MemoryVault:
    def __init__(self):
        self.values = {}

    def read(self, key):
        return self.values.get(key)

    def write(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


class Server(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


def wait_for(predicate, timeout=180):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if result := predicate():
            return result
        time.sleep(.2)
    raise RuntimeError('验收等待超时')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-state', type=Path, default=ROOT / '.pulse')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--computer', action='store_true')
    args = parser.parse_args()
    source = json.loads((args.source_state / 'gateways.json').read_text())
    profile = next(item for item in source['profiles'] if item['id'] == source['active_id'])
    secret = MacKeychain(source['installation_id']).read(profile['provider_id'])
    if not secret or not secret.get('key'):
        raise SystemExit('所选 Gateway 缺少凭据，未发起验收请求。')
    report = {'gateway': profile['base_url'], 'model': profile['model'], 'ok': False}
    vault = MemoryVault()
    with tempfile.TemporaryDirectory(prefix='morning-workarea-live-') as directory:
        root = Path(directory)
        workspace, state = root / 'project', root / 'state'
        workspace.mkdir()
        gateways = GatewayStore(state, vault=vault)
        gateways.save({**{key: profile[key] for key in ('name', 'base_url', 'api', 'model', 'request_format', 'model_options') if key in profile}, 'api_key': secret['key']})
        host = NativePiHost(runtime_path=ROOT / 'apps/agent-runtime/native-bootstrap.mjs', workspace=workspace, state_dir=state, gateways=gateways, agent_dir=root / 'agent')
        kernel = build(db_url=f'sqlite:///{root}/kernel.db', with_model=False)
        app = LocalPulseApp(kernel, native_host=host, gateways=gateways, state_dir=state, platform_enabled=True, dist=ROOT / 'apps/pulse/dist')
        app.platform.devices.vault = MemoryVault()
        pool = app.api.native
        server = None
        try:
            host.control('start_agent')
            alpha = pool.snapshot()['state']['sessionId']
            beta = pool.command('new_session', {})['sessionId']
            report['sessions'] = [alpha, beta]
            timeline, errors, tool_events = {}, [], []
            def event(value):
                if value.get('type') in ('tool_execution_start', 'tool_execution_end'):
                    tool_events.append({**value, 'at':time.time()})
                if value.get('type') in ('agent_start', 'agent_settled'):
                    sid = pool.slot['state'].get('sessionId')
                    timeline.setdefault(sid, {})[value['type']] = time.time()
            pool.add_listener(event)
            def run(sid, name):
                try:
                    with pool.request_scope(sid):
                        pool.command('set_thinking_level', {'level': 'low'})
                        marker = name + '_' + uuid.uuid4().hex[:8]
                        report[name] = {'marker': marker}
                        pool.prompt({'message': f'只在当前工作目录执行一次原生 bash：sleep 3; printf {marker} > {name}.txt; cat {name}.txt。完成后回复工具读出的内容。不要读取其他目录。'}, 'live-' + name)
                except Exception as exc:
                    errors.append(str(exc))
            threads = [threading.Thread(target=run, args=(sid, name)) for sid, name in ((alpha, 'alpha'), (beta, 'beta'))]
            for thread in threads:
                thread.start()
            def settled():
                return all('agent_settled' in timeline.get(sid, {}) for sid in (alpha, beta)) or errors
            wait_for(settled)
            for thread in threads:
                thread.join(5)
            assert not errors, errors
            assert max(timeline[sid]['agent_start'] for sid in (alpha, beta)) < min(timeline[sid]['agent_settled'] for sid in (alpha, beta)), '两个真实 Agent 轮次没有重叠'
            for sid, name, other in ((alpha, 'alpha', 'beta'), (beta, 'beta', 'alpha')):
                with pool.request_scope(sid):
                    history = pool.host.rpc('get_messages')['messages']
                    assert (workspace / f'{name}.txt').read_text() == report[name]['marker']
                    assert any(m.get('role') == 'toolResult' and m.get('toolName') == 'bash' for m in history)
                    assert report[other]['marker'] not in json.dumps(history)
                    report[name].update(tool='bash', artifact_verified=True, history_isolated=True)
            report['overlap_seconds'] = min(timeline[sid]['agent_settled'] for sid in (alpha, beta)) - max(timeline[sid]['agent_start'] for sid in (alpha, beta))
            print(json.dumps({'phase':'parallel_agents_verified','overlap_seconds':report['overlap_seconds']}), flush=True)
            # Two long-lived real PTYs belong to one task and outlive its visible pane.
            with pool.request_scope(beta):
                terminal = app.platform.workspace
                one = terminal.command({'operation':'terminal_create','request_id':'parallel-one','command':'sleep 20; printf ONE'})
                two = terminal.command({'operation':'terminal_create','request_id':'parallel-two','command':'sleep 20; printf TWO'})
                assert all(item['state'] == 'running' for item in terminal.state()['terminals'])
                terminal.command({'operation':'terminal_stop','id':one['id']})
                wait_for(lambda: terminal.command({'operation':'terminal_read','id':one['id']})['terminal']['state'] == 'cancelled', 10)
                assert terminal.command({'operation':'terminal_read','id':two['id']})['terminal']['state'] == 'running'
                terminal.command({'operation':'terminal_stop','id':two['id']})
                report['task_processes'] = {'parallel':True,'independent_stop':True,'engine':'node-pty'}
                # Keep Pi's own parallel tool scheduler; do not add another one.
                tool_events.clear()
                timeline[beta].pop('agent_settled', None)
                pool.prompt({'message':'执行原生工具并行验收。请在同一条 assistant 消息中发起恰好两个独立 bash 工具调用，不要合并为一个命令：第一个命令是 sleep 3; printf FIRST > tool-first.txt，第二个命令是 sleep 3; printf SECOND > tool-second.txt。两次工具都返回后，只回复完成。不要读取其他目录。'}, 'live-native-parallel-tools')
                wait_for(lambda:'agent_settled' in timeline.get(beta, {}))
                intervals = {}
                for item in tool_events:
                    if item.get('toolName') == 'bash':
                        intervals.setdefault(item['toolCallId'], {})[item['type']] = item['at']
                assert len(intervals) == 2, '模型没有发出两个独立原生 bash 工具调用'
                overlap = min(item['tool_execution_end'] for item in intervals.values()) - max(item['tool_execution_start'] for item in intervals.values())
                assert overlap > 1, '原生工具没有并行执行'
                assert (workspace/'tool-first.txt').read_text() == 'FIRST' and (workspace/'tool-second.txt').read_text() == 'SECOND'
                report['native_parallel_tools'] = {'tools':2,'overlap_seconds':overlap,'artifacts_verified':True,'scheduler':'Pi native'}
                print(json.dumps({'phase':'native_parallel_tools_verified','overlap_seconds':overlap}), flush=True)
                report['core_ok'] = True
                if args.computer:
                    app.platform.start()
                    app.platform.command({'operation':'permissions','permissions':{'observe_apps':['dev.morning.acceptance'],'operate_apps':['dev.morning.acceptance'],'screens':[]}})
                    try:
                        app.platform.command({'operation':'observe','app':'dev.morning.acceptance'})
                    except ValueError as exc:
                        report['computer'] = {'state':'blocked','code':getattr(exc,'code','observation_failed'),'message':str(exc)}
                        raise
                    server = make_server('127.0.0.1', 0, app, server_class=Server, handler_class=Quiet)
                    app.allowed_hosts.add(f'127.0.0.1:{server.server_port}')
                    threading.Thread(target=server.serve_forever, daemon=True).start()
                    url = f'http://127.0.0.1:{server.server_port}/#chat'
                    print(json.dumps({'phase':'computer_started','url':url}), flush=True)
                    pool.prompt({'message':'请仅通过 morning_computer 观察 dev.morning.acceptance 这个独立验收应用，读取真实按钮编号，提议点击一次“增加”，目标是计数从 0 变为 1。等待界面批准，批准后重新观察核验。不要操作其他应用，不要用 bash、脚本或文件修改代替电脑操作。'}, 'live-computer')
                    pending = wait_for(lambda: next(iter(app.platform.store.list('computer', states=('approval',))), None))
                    print(json.dumps({'phase':'awaiting_gui_approval','url':url,'action':pending['id']}), flush=True)
                    executed = wait_for(lambda: (job if (job := app.platform.store.get('computer',pending['id']))['state'] != 'approval' else None), 115)
                    assert executed['state'] in ('queued','running','observed'), executed['state']
                    wait_for(lambda: app.platform.store.get('computer',pending['id'])['state'] in ('observed','failed','unknown'))
                    final = app.platform.store.get('computer',pending['id'])
                    assert final['state'] == 'observed', final.get('error')
                    observed = app.platform.command({'operation':'observe','app':'dev.morning.acceptance'})
                    assert '计数 1' in observed['text']
                    report['computer'] = {'app':'dev.morning.acceptance','approval':'GUI','state':'observed','count':1}
                    print(json.dumps({'phase':'computer_verified','url':url}), flush=True)
                    wait_for(lambda: all(item['state'] in ('delivered','failed','unknown') for item in app.platform.store.list('wakeup')), 90)
                    report['computer']['wakeups'] = [item['state'] for item in app.platform.store.list('wakeup')]
                    # Leave the verified UI available briefly for a final screenshot.
                    time.sleep(45)
            report['ok'] = True
        except Exception as exc:
            report['error'] = str(exc).replace(secret['key'], '[redacted]')
        finally:
            app.platform.close()
            pool.close()
            if server:
                server.shutdown()
                server.server_close()
            kernel.ctx.store.close()
            vault.values.clear()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
