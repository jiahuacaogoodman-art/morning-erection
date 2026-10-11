import time

import pytest

from wakecore.app.pulse.platform_store import PlatformStore
from wakecore.app.pulse.workspace import TaskWorkspace, preview_url


def eventually(read, predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = read()
        if predicate(value):
            return value
        time.sleep(.02)
    raise AssertionError(value)


@pytest.fixture
def workspace(tmp_path):
    context = {'session_id': 'session-one', 'workspace': str(tmp_path), 'session_path': str(tmp_path / 'native.jsonl')}
    ws = TaskWorkspace(PlatformStore(tmp_path / 'state'), lambda: dict(context))
    yield ws, context, tmp_path
    ws.close()


def start(ws, key, command=None):
    return ws.command({'operation': 'terminal_create', 'request_id': key, **({'command': command} if command else {})})


def read(ws, item, cursor=0):
    return ws.command({'operation': 'terminal_read', 'id': item['id'], 'cursor': cursor})


def test_real_pty_input_resize_reconnect_and_stop(workspace):
    ws, _, _ = workspace
    item = start(ws, 'interactive')
    ws.command({'operation': 'terminal_resize', 'id': item['id'], 'cols': 77, 'rows': 19})
    data = {'operation': 'terminal_input', 'id': item['id'], 'input_id': 'input-one', 'data': "printf 'PTY_RESULT'; stty size\n"}
    ws.command(data)
    assert ws.command(data)['duplicate']
    with pytest.raises(ValueError, match='不同内容'):
        ws.command({**data, 'data': 'unexpected'})
    output = eventually(lambda: read(ws, item), lambda value: '19 77' in value['output'])
    assert 'PTY_RESULT' in output['output']
    assert read(ws, item)['output'] == output['output']
    ws.command({'operation': 'terminal_stop', 'id': item['id']})
    eventually(lambda: read(ws, item), lambda value: value['terminal']['state'] == 'cancelled')
    assert ws.running[item['id']]['fd'] is None


def test_parallel_processes_and_duplicate_start_do_not_block_each_other(workspace):
    ws, _, root = workspace
    slow = start(ws, 'slow', 'sleep 5')
    fast = start(ws, 'fast', "printf 'ONCE\\n' >> once.txt; printf 'http://127.0.0.1:41099/\\n'")
    assert start(ws, 'fast', "printf 'ONCE\\n' >> once.txt; printf 'http://127.0.0.1:41099/\\n'")['id'] == fast['id']
    with pytest.raises(ValueError, match='不同命令'):
        start(ws, 'fast', 'touch twice')
    result = eventually(lambda: read(ws, fast), lambda value: value['terminal']['state'] == 'completed')
    assert result['urls'] == ['http://127.0.0.1:41099/']
    assert (root / 'once.txt').read_text() == 'ONCE\n'
    assert read(ws, slow)['terminal']['state'] == 'running'
    ws.command({'operation': 'terminal_stop', 'id': slow['id']})
    eventually(lambda: read(ws, slow), lambda value: value['terminal']['state'] == 'cancelled')


def test_binding_preview_validation_and_restart_do_not_replay(workspace):
    ws, context, root = workspace
    item = start(ws, 'history', 'printf recorded')
    eventually(lambda: read(ws, item), lambda value: value['terminal']['state'] == 'completed')
    preview = ws.command({'operation': 'preview_open', 'url': 'http://localhost:4321/'})
    assert ws.command({'operation': 'preview_open', 'url': 'http://localhost:4321/'})['id'] == preview['id']
    context['session_id'] = 'session-two'
    assert ws.state()['terminals'] == []
    for op, key in [('terminal_stop', item['id']), ('preview_remove', preview['id'])]:
        with pytest.raises(ValueError, match='不属于'):
            ws.command({'operation': op, 'id': key})
    with pytest.raises(ValueError, match='切换'):
        ws.command({'operation': 'terminal_create', 'session_id': 'session-one', 'request_id': 'stale'})
    context['session_id'] = 'session-one'
    ws.close()
    restarted = TaskWorkspace(PlatformStore(root / 'state'), lambda: dict(context))
    try:
        assert restarted.state()['terminals'][0]['state'] == 'completed'
        assert start(restarted, 'history', 'printf recorded')['id'] == item['id']
        assert not restarted.running
        assert restarted.state()['previews'][0]['id'] == preview['id']
    finally:
        restarted.close()


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'file:///etc/passwd', 'https://user:password@example.com', 'https://example.com:bad', 'http://example.com/\n'])
def test_preview_rejects_non_web_or_credential_urls(url):
    with pytest.raises(ValueError):
        preview_url(url)


def test_buffer_is_bounded_and_stale_cursor_recovers(workspace):
    ws, _, _ = workspace
    item = start(ws, 'large-output', "python3 -c 'print(\"a\"*180000)' ")
    result = eventually(lambda: read(ws, item), lambda value: value['terminal']['state'] != 'running')
    assert result['terminal']['state'] == 'completed'
    assert result['reset']
    assert len(result['output']) <= 128 * 1024
    assert read(ws, item, result['cursor'])['output'] == ''
