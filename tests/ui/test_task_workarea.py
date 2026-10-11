"""Task work area against real native Pi, HTTP and PTY processes."""
import re
import socket
import threading
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import pytest
from playwright.sync_api import expect, sync_playwright

from wakecore.adapters.agent_runtime.native import NativePiHost
from wakecore.app.bootstrap import build
from wakecore.app.pulse.gateways import GatewayStore
from wakecore.app.pulse.server import LocalPulseApp

ROOT = Path(__file__).resolve().parents[2]
PROOF = Path('/tmp/morning-task-flow')


class Vault:
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


@pytest.fixture
def workarea(tmp_path):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    (workspace / 'index.html').write_text('<!doctype html><html lang="en"><title>Task preview</title><h1>REAL WORKSPACE PREVIEW</h1><p>Served by the terminal in this conversation.</p></html>')
    state = tmp_path / 'state'
    store = GatewayStore(state, vault=Vault())
    host = NativePiHost(runtime_path=ROOT / 'apps/agent-runtime/native-bootstrap.mjs', workspace=workspace, state_dir=state, gateways=store, agent_dir=tmp_path / 'agent')
    kernel = build(db_url=f'sqlite:///{tmp_path}/kernel.db', with_model=False)
    app = LocalPulseApp(kernel, native_host=host, gateways=store, state_dir=state, platform_enabled=True, dist=ROOT / 'apps/pulse/dist')
    host.control('start_agent')
    app.api.native.snapshot()
    server = make_server('127.0.0.1', 0, app, server_class=Server, handler_class=Quiet)
    base = f'http://127.0.0.1:{server.server_port}'
    app.allowed_hosts.add(f'127.0.0.1:{server.server_port}')
    threading.Thread(target=server.serve_forever, daemon=True).start()
    PROOF.mkdir(exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={'width': 1440, 'height': 900}, reduced_motion='reduce')
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base)
            expect(page.get_by_role('combobox', name='给 Agent 的消息', exact=True)).to_be_visible()
            yield page, app, workspace, base, errors
            browser.close()
    finally:
        app.platform.close()
        app.api.native.close()
        server.shutdown()
        server.server_close()
        kernel.ctx.store.close()


def post(page, operation, **values):
    return page.request.post('/api/pulse/workspace', data={'operation': operation, **values}, headers={'X-Pulse-Client': '1', 'Origin': page.url.split('/#')[0].rstrip('/')}).json()


def accessible(page):
    page.add_script_tag(path=str(ROOT / 'apps/pulse/node_modules/axe-core/axe.min.js'))
    violations = page.evaluate("async()=> (await axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa']}})).violations.map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)}))")
    assert not violations, violations
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_real_terminal_preview_and_collapse_keep_chat_usable(workarea):
    page, app, workspace, base, errors = workarea
    assert page.get_by_role('button', name='Computer', exact=True).count() == 0
    page.get_by_role('button', name=re.compile(r'^终端(?: \d+)?$')).click()
    page.get_by_role('button', name='打开终端', exact=True).click()
    terminal = page.get_by_label('终端输入', exact=True)
    expect(terminal).to_be_attached()
    page.locator('.task-terminal-screen .xterm-screen').click()
    page.keyboard.type("printf 'WORKAREA_PTY_OK\\n'")
    page.keyboard.press('Enter')
    page.wait_for_function("document.querySelector('.task-terminal-screen').textContent.includes('WORKAREA_PTY_OK')")
    item = app.platform.workspace.state()['terminals'][0]
    assert 'WORKAREA_PTY_OK' in app.platform.workspace.command({'operation': 'terminal_read', 'id': item['id']})['output']
    # A genuine dev server keeps running while a second terminal and conversation work.
    with socket.socket() as available:
        available.bind(('127.0.0.1', 0))
        port = available.getsockname()[1]
    server = app.platform.workspace.command({'operation': 'terminal_create', 'request_id': 'ui-server', 'title': '预览服务',
        'command': f'python3 -u -m http.server {port} --bind 127.0.0.1'})
    expect(page.get_by_role('tab', name='预览服务', exact=True)).to_be_visible(timeout=10000)
    page.get_by_role('tab', name='预览服务', exact=True).click()
    link = page.get_by_role('button', name=f'http://127.0.0.1:{port}/', exact=True)
    expect(link).to_be_visible(timeout=10000)
    link.click()
    expect(page.frame_locator('iframe[title="任务网页预览"]').get_by_role('heading', name='REAL WORKSPACE PREVIEW')).to_be_visible()
    expect(page.get_by_role('combobox', name='给 Agent 的消息', exact=True)).to_be_enabled()
    page.screenshot(path=str(PROOF / 'chat-with-real-preview-1440.png'))
    page.get_by_role('button', name='收起任务工作区', exact=True).click()
    expect(page.get_by_role('complementary', name='任务工作区')).to_be_hidden()
    assert app.platform.workspace.command({'operation': 'terminal_read', 'id': server['id']})['terminal']['state'] == 'running'
    page.get_by_role('button', name=re.compile(r'^终端(?: \d+)?$')).click()
    page.get_by_role('tab', name='预览服务', exact=True).click()
    page.screenshot(path=str(PROOF / 'chat-with-terminals-1440.png'))
    accessible(page)
    page.set_viewport_size({'width': 390, 'height': 844})
    expect(page.get_by_role('combobox', name='给 Agent 的消息', exact=True)).to_be_visible()
    accessible(page)
    page.screenshot(path=str(PROOF / 'chat-with-terminal-390.png'))
    page.get_by_role('button', name='停止这个终端', exact=True).click()
    expect(page.get_by_text('已停止', exact=True)).to_be_visible()
    assert app.platform.workspace.command({'operation': 'terminal_read', 'id': item['id']})['terminal']['state'] == 'running'
    assert not errors


def test_new_session_keeps_old_process_and_draft_owned_by_its_conversation(workarea):
    page, app, workspace, base, errors = workarea
    app.api.native.command('set_session_name', {'name': '第一段任务会话'})
    first = app.api.native.snapshot()['state']['sessionId']
    page.reload()
    page.get_by_role('combobox', name='给 Agent 的消息', exact=True).fill('第一段会话的草稿')
    job = app.platform.workspace.command({'operation': 'terminal_create', 'request_id': 'first-owned', 'title': '第一段任务', 'command': 'sleep 20'})
    page.get_by_role('button', name='新建会话', exact=True).click()
    expect(page.get_by_role('combobox', name='给 Agent 的消息', exact=True)).to_have_value('')
    page.wait_for_function('(id)=>sessionStorage.getItem("morning:pi-session")!==id', arg=first)
    second = page.evaluate('sessionStorage.getItem("morning:pi-session")')
    assert first != second
    page.get_by_role('button', name=re.compile(r'^终端(?: \d+)?$')).click()
    assert page.get_by_role('tab', name='第一段任务', exact=True).count() == 0
    # Explicitly return through the GUI's real native switch command, then reload the view.
    with page.expect_response(lambda response: response.url.endswith('/pulse/pi/control')):
        page.get_by_role('button', name='全部会话', exact=True).click()
    page.locator('.pi-session').filter(has_text='第一段任务会话').click()
    expect(page.get_by_role('combobox', name='给 Agent 的消息', exact=True)).to_have_value('第一段会话的草稿')
    with app.api.native.request_scope(first):
        assert app.platform.workspace.command({'operation': 'terminal_read', 'id': job['id']})['terminal']['state'] == 'running'
    assert not errors
