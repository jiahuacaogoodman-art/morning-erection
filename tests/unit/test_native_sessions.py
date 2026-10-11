import threading
import time

from test_native_pi import native as real_native

from wakecore.app.pulse.native_sessions import NativeSessions

native = real_native


def wait_until(predicate, seconds=8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('native runtime condition did not settle')


def test_real_pi_sessions_execute_concurrently_and_keep_own_history(native):
    host, _ = native
    pool = NativeSessions(host)
    errors, results = [], {}
    try:
        host.rpc('set_thinking_level', {'level': 'low'})
        alpha = pool.snapshot()['state']['sessionId']
        def run(sid, name, command):
            try:
                with pool.request_scope(sid):
                    results[name] = pool.command('bash', {'command': command})
            except Exception as exc:
                errors.append(exc)
        first = threading.Thread(target=run, args=(alpha, 'alpha', 'sleep 2; printf ALPHA_NATIVE'))
        first.start()
        wait_until(lambda: any(item['id'] == alpha and item['running'] for item in pool.activities()))
        beta = pool.command('new_session', {})['sessionId']
        assert alpha != beta
        assert first.is_alive(), 'Creating another view must not wait for or abort the original task'
        with pool.request_scope(beta):
            pool.command('set_thinking_level', {'level': 'high'})
        second = threading.Thread(target=run, args=(beta, 'beta', 'sleep 1; printf BETA_NATIVE'))
        second.start()
        wait_until(lambda: sum(item['running'] for item in pool.activities()) == 2)
        first.join(8)
        second.join(8)
        assert not errors
        assert 'ALPHA_NATIVE' in results['alpha']['output']
        assert 'BETA_NATIVE' in results['beta']['output']
        with pool.request_scope(alpha):
            assert pool.host.rpc('get_state')['thinkingLevel'] == 'low'
            messages = pool.host.rpc('get_messages')['messages']
            assert any('ALPHA_NATIVE' in message.get('output', '') for message in messages)
            assert not any('BETA_NATIVE' in message.get('output', '') for message in messages)
        with pool.request_scope(beta):
            assert pool.host.rpc('get_state')['thinkingLevel'] == 'high'
            assert pool.host.process.pid != host.process.pid
        assert {alpha, beta} <= {item['id'] for item in pool.sessions()}
    finally:
        pool.close()


def test_stopping_one_runtime_does_not_stop_another(native):
    host, _ = native
    pool = NativeSessions(host)
    threads, outputs = [], {}
    try:
        host.control('start_agent')
        alpha = pool.snapshot()['state']['sessionId']
        beta = pool.command('new_session', {})['sessionId']
        def run(sid):
            with pool.request_scope(sid):
                outputs[sid] = pool.command('bash', {'command': 'sleep 3; printf FINISHED'})
        for sid in (alpha, beta):
            thread = threading.Thread(target=run, args=(sid,))
            threads.append(thread)
            thread.start()
        wait_until(lambda: sum(item['running'] for item in pool.activities()) == 2)
        pool.stop_session(alpha)
        wait_until(lambda: alpha in outputs)
        assert outputs[alpha]['cancelled']
        assert any(item['id'] == beta and item['running'] for item in pool.activities())
        for thread in threads:
            thread.join(8)
        assert 'FINISHED' in outputs[beta]['output']
    finally:
        pool.close()


def test_restart_recovers_each_native_history_and_saved_capacity(native):
    import json
    import subprocess

    from wakecore.adapters.agent_runtime.native import NativePiHost

    host, _ = native
    # Build native conversation fixtures using Pi's SDK. A bash-only scratch
    # session is intentionally not persisted by Pi until it has a user message.
    code = """import {SessionManager} from '@earendil-works/pi-coding-agent';
const result=[]; for(const name of ['alpha','beta']) {
 const s=SessionManager.create(process.argv[1],process.argv[2]);
 s.appendMessage({role:'user',content:'Restore fixture '+name,timestamp:Date.now()});
 result.push(s.getSessionFile());
} console.log(JSON.stringify(result));"""
    files = json.loads(subprocess.check_output(['node','--input-type=module','-e',code,str(host.workspace),str(host.state_dir/'pi'/'sessions')], cwd=host.runtime_path.parent, text=True))
    pool = NativeSessions(host)
    pool.set_limit(2)
    try:
        alpha = pool.command('switch_session', {'sessionPath':files[0]})['sessionId']
        pool.command('bash', {'command':'printf RESTORE_ALPHA'})
        beta = pool.command('switch_session', {'sessionPath':files[1]})['sessionId']
        pool.command('bash', {'command':'printf RESTORE_BETA'})
    finally:
        pool.close()
    resumed_host = NativePiHost(runtime_path=host.runtime_path, workspace=host.workspace, state_dir=host.state_dir, gateways=host.gateways, agent_dir=host.agent_dir)
    resumed = NativeSessions(resumed_host)
    try:
        assert resumed.limit == 2
        resumed_host.control('start_agent')
        assert resumed.snapshot()['state']['sessionId'] == beta
        for sid, own, other in ((alpha,'RESTORE_ALPHA','RESTORE_BETA'),(beta,'RESTORE_BETA','RESTORE_ALPHA')):
            with resumed.request_scope(sid):
                output = '\n'.join(message.get('output','') for message in resumed.host.rpc('get_messages')['messages'])
                assert own in output and other not in output
        third = resumed.command('new_session', {})['sessionId']
        assert third not in (alpha, beta)
        assert len(resumed.activities()) == 2
        with resumed.request_scope(alpha):
            assert resumed.host.rpc('get_state')['sessionId'] == alpha
        assert len(resumed.activities()) == 2
    finally:
        resumed.close()
