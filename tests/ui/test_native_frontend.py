"""Browser regression checks against a real Pi process; no model is called here."""
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
DIST = ROOT / "apps/pulse/dist"
AXE = ROOT / "apps/pulse/node_modules/axe-core/axe.min.js"


class Vault:
    def __init__(self):
        self.values = {}

    def read(self, provider):
        return self.values.get(provider)

    def write(self, provider, credential):
        self.values[provider] = credential

    def delete(self, provider):
        self.values.pop(provider, None)


class ThreadedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


def assert_accessible(page):
    page.add_script_tag(path=str(AXE))
    violations = page.evaluate("async () => (await axe.run(document, {runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa']}})).violations")
    assert not violations, [(item["id"], [node["target"] for node in item["nodes"]]) for item in violations]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.fixture
def native_ui(tmp_path):
    state, workspace, agent = tmp_path / "state", tmp_path / "workspace", tmp_path / "agent"
    workspace.mkdir()
    extensions = agent / "extensions"
    extensions.mkdir(parents=True)
    (extensions / "ui.js").write_text('''export default function(pi) {
      pi.registerCommand('ui-check',{description:'Native dialog test',handler:async(_args,ctx)=>{
        const choice=await ctx.ui.select('Native select',['Continue','Cancel']);
        if(choice !== 'Continue') return;
        const text=await ctx.ui.input('Native input','');
        pi.sendMessage({customType:'ui-test',content:'NATIVE_UI_RESULT '+text,display:true});
      }});
    }''')
    store = GatewayStore(state, vault=Vault())
    host = NativePiHost(runtime_path=ROOT / "apps/agent-runtime/native-bootstrap.mjs", workspace=workspace, state_dir=state, gateways=store, agent_dir=agent)
    if not host.available or not (DIST / "index.html").exists():
        pytest.skip("install Pi and build Pulse before GUI regression checks")
    kernel = build(db_url=f"sqlite:///{tmp_path / 'kernel.db'}", with_model=False)
    app = LocalPulseApp(kernel, native_host=host, gateways=store, state_dir=state, dist=DIST)
    http = make_server("127.0.0.1", 0, app, server_class=ThreadedServer, handler_class=QuietHandler)
    app.allowed_hosts.add(f"127.0.0.1:{http.server_port}")
    threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{http.server_port}")
            expect(page.get_by_text("从一句话开始。", exact=True)).to_be_visible()
            yield page, host, store, errors
            browser.close()
    finally:
        http.shutdown()
        http.server_close()
        host.close()
        kernel.ctx.store.close()


def test_gateway_gui_crud_hides_credentials_and_preserves_native_empty_state(native_ui, tmp_path):
    page, host, store, errors = native_ui
    page.get_by_role("button", name="连接设置", exact=True).click()
    page.get_by_role("button", name="添加 Gateway", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("名称", exact=True).fill("Local contract gateway")
    dialog.get_by_label("API 根地址", exact=True).fill("http://127.0.0.1:1")
    dialog.get_by_label("模型名称", exact=True).fill("contract-model")
    dialog.get_by_label("API Key / Bearer Token", exact=True).fill("fixture-ui-secret-not-a-real-key")
    dialog.get_by_role("button", name="安全保存", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_text("凭据已保存 · 尚未测试", exact=True)).to_be_visible()
    assert "fixture-ui-secret" not in page.content()
    page.get_by_role("button", name="测试连接", exact=True).click()
    expect(page.get_by_text("暂时无法完成 Gateway 请求，请检查网络和配置。", exact=True).first).to_be_visible(timeout=30000)
    page.get_by_role("button", name="编辑", exact=True).click()
    expect(dialog.get_by_label("API Key / Bearer Token", exact=True)).to_have_value("")
    dialog.get_by_label("名称", exact=True).fill("Renamed local gateway")
    dialog.get_by_role("button", name="安全保存", exact=True).click()
    expect(dialog).to_be_hidden()
    assert store.profile()["name"] == "Renamed local gateway"
    assert store.read_credential(store.profile()["provider_id"])["key"] == "fixture-ui-secret-not-a-real-key"
    assert_accessible(page)
    page.set_viewport_size({"width": 390, "height": 844})
    assert_accessible(page)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    page.screenshot(path=str(artifacts / "gateway-mobile.png"))
    page.get_by_role("button", name="编辑", exact=True).click()
    assert_accessible(page)
    dialog.get_by_role("button", name="删除凭据，保留配置", exact=True).click()
    dialog.get_by_role("button", name="保留", exact=True).click()
    assert store.public()["items"][0]["has_credentials"]
    dialog.get_by_role("button", name="删除凭据，保留配置", exact=True).click()
    dialog.get_by_role("button", name="确认移除", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_text("尚未配置凭据", exact=True)).to_be_visible()
    page.get_by_role("button", name="编辑", exact=True).click()
    dialog.get_by_role("button", name="移除 Gateway", exact=True).click()
    dialog.get_by_role("button", name="确认移除", exact=True).click()
    expect(dialog).to_be_hidden()
    assert store.public()["items"] == []
    page.reload()
    expect(page.get_by_role("heading", name="设置", exact=True)).to_be_visible()
    page.goto(page.url.split("#")[0])
    expect(page.get_by_text("从一句话开始。", exact=True)).to_be_visible()
    assert host.control("sessions") == []
    assert not errors


def test_gateway_running_check_survives_reload_and_stale_editor_preserves_input(native_ui):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    page, host, store, errors = native_ui
    release = threading.Event()
    entered = threading.Event()

    class SlowGateway(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            entered.set()
            release.wait(20)
            try:
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowGateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        item = store.save({"name": "Slow connection", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                           "model": "gui-contract", "api": "openai-responses", "api_key": "private-gui-fixture"})["items"][0]
        host.update_gateways()
        page.get_by_role("button", name="连接设置", exact=True).click()
        page.get_by_role("button", name="测试连接", exact=True).click()
        expect(page.get_by_role("button", name="取消测试", exact=True)).to_be_visible()
        assert entered.wait(5)
        page.reload()
        expect(page.get_by_role("button", name="取消测试", exact=True)).to_be_visible()
        page.get_by_role("button", name="取消测试", exact=True).click()
        expect(page.get_by_text("连接测试已取消。", exact=True).first).to_be_visible()
        release.set()
        page.get_by_role("button", name="编辑", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("名称", exact=True).fill("Unsaved input")
        store.save({"id": item["id"], "name": "Changed elsewhere"})
        dialog.get_by_role("button", name="安全保存", exact=True).click()
        expect(dialog.get_by_text("连接配置已在别处更新。请重新载入配置后再修改；你的输入仍保留。", exact=True)).to_be_visible()
        expect(dialog.get_by_label("名称", exact=True)).to_have_value("Unsaved input")
        assert store.profile()["name"] == "Changed elsewhere"
        expect(page.get_by_text("Changed elsewhere", exact=True)).to_be_visible()
        dialog.get_by_role("button", name="放弃本次输入，载入最新配置", exact=True).click()
        expect(dialog.get_by_label("名称", exact=True)).to_have_value("Changed elsewhere")
        assert_accessible(page)
        assert "private-gui-fixture" not in page.content()
        assert not errors
    finally:
        release.set()
        server.shutdown()
        server.server_close()


def test_native_commands_dialogs_models_and_context_are_accessible(native_ui, tmp_path):
    page, _, _, errors = native_ui
    page.get_by_role("button", name="模型与 Agent 设置", exact=True).click()
    page.get_by_role("tab", name="能力", exact=True).click()
    command = page.get_by_role("button", name="/ui-check Native dialog test", exact=True)
    expect(command).to_be_visible()
    assert_accessible(page)
    command.click()
    expect(page.get_by_role("textbox", name="给 Agent 的消息", exact=True)).to_have_value("/ui-check ")
    page.get_by_role("button", name="发送消息", exact=True).click()
    select = page.get_by_role("dialog", name="Native select", exact=True)
    expect(select).to_be_visible()
    assert_accessible(page)
    select.get_by_role("button", name="Continue", exact=True).click()
    entry = page.get_by_role("dialog", name="Native input", exact=True)
    expect(entry).to_be_visible()
    entry.get_by_role("textbox", name="你的回答", exact=True).fill("PI_GUI_VERIFIED")
    entry.get_by_role("button", name="继续", exact=True).click()
    expect(page.get_by_text("NATIVE_UI_RESULT PI_GUI_VERIFIED", exact=True)).to_be_visible()
    page.get_by_role("button", name="模型与 Agent 设置", exact=True).click()
    page.get_by_role("tab", name="模型与上下文", exact=True).click()
    expect(page.get_by_role("combobox", name="当前模型", exact=True)).to_be_visible()
    expect(page.get_by_text("当前上下文", exact=True)).to_be_visible()
    assert_accessible(page)
    page.set_viewport_size({"width": 390, "height": 844})
    assert_accessible(page)
    page.screenshot(path=str(tmp_path / "native-model-mobile.png"))
    assert not errors
