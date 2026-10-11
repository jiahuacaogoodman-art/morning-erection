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
def native_ui(tmp_path, request):
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
            page = browser.new_page(viewport={"width": 1440, "height": 900}, reduced_motion="reduce",
                **({"record_video_dir": str(tmp_path / "video"), "record_video_size": {"width": 1440, "height": 900}} if request.node.name in {"test_model_motion_frames_and_native_selection", "test_thinking_drag_slash_and_native_commands"} else {}))
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{http.server_port}")
            expect(page.get_by_text("从一句话开始。", exact=True)).to_be_visible()
            yield page, host, store, errors
            if page.video:
                video = page.video
                page.close()
                target = Path('/tmp/morning-design-depth') / ('thinking-motion.webm' if request.node.name == 'test_thinking_drag_slash_and_native_commands' else 'model-motion.webm')
                target.parent.mkdir(parents=True, exist_ok=True)
                video.save_as(str(target))
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
    expect(page.get_by_role("combobox", name="给 Agent 的消息", exact=True)).to_have_value("/ui-check ")
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


def test_native_settings_categories_search_save_scope_and_persistence(native_ui):
    page, host, _, errors = native_ui
    page.get_by_role("button", name="设置", exact=True).click()
    page.get_by_role("tab", name="Agent 行为", exact=True).click()
    field = page.get_by_label("补充指令的处理方式", exact=True)
    expect(field).to_be_visible()
    field.select_option("all")
    # Drafts remain available when navigating to another settings category.
    page.get_by_role("tab", name="外观", exact=True).click()
    page.get_by_role("button", name="保存更改", exact=True).click()
    expect(page.get_by_role("button", name="保存更改", exact=True)).to_be_hidden()
    import json
    stored = Path(host.agent_dir) / "settings.json"
    assert json.loads(stored.read_text())["steeringMode"] == "all"
    page.get_by_role("searchbox", name="搜索设置", exact=True).fill("预留上下文空间")
    page.get_by_role("button", name="预留上下文空间", exact=False).click()
    expect(page.get_by_label("预留上下文空间", exact=True)).to_be_visible()
    page.get_by_label("预留上下文空间", exact=True).fill("8192")
    page.get_by_role("button", name="保存更改", exact=True).click()
    expect(page.get_by_role("button", name="保存更改", exact=True)).to_be_hidden()
    assert json.loads(stored.read_text())["compaction"]["reserveTokens"] == 8192
    page.reload()
    page.get_by_role("button", name="上下文策略", exact=True).click()
    expect(page.get_by_label("预留上下文空间", exact=True)).to_have_value("8192")
    assert_accessible(page)
    page.get_by_role("button", name="预留上下文空间：恢复继承", exact=True).click()
    page.get_by_role("button", name="保存更改", exact=True).click()
    expect(page.get_by_role("button", name="保存更改", exact=True)).to_be_hidden()
    assert "reserveTokens" not in json.loads(stored.read_text())["compaction"]
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("combobox", name="设置分类", exact=True).select_option("agent")
    expect(page.get_by_label("补充指令的处理方式", exact=True)).to_have_value("all")
    assert_accessible(page)
    assert not errors


def test_composer_model_picker_switches_real_pi_model_and_thinking(native_ui):
    import re
    page, host, store, errors = native_ui
    for name, model in [("First connection", "first-model"), ("Second connection", "second-model")]:
        store.save({"name": name, "base_url": "http://127.0.0.1:1/v1", "api": "openai-responses", "model": model,
                    "model_options": {"reasoning": True, "input": ["text", "image"]}, "api_key": "model-menu-fixture-key"})
    host.update_gateways()
    host.control("start_agent")
    page.reload()
    picker = page.get_by_role("button", name=re.compile(r"^选择模型，当前 first-model"))
    expect(picker).to_be_visible()
    picker.click()
    dialog = page.get_by_role("dialog", name="选择模型", exact=True)
    expect(dialog).to_be_visible()
    dialog.get_by_role("combobox", name="搜索模型", exact=True).fill("second")
    option = dialog.get_by_role("option", name=re.compile("second-model"))
    expect(option).to_be_visible()
    assert_accessible(page)
    option.click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_role("button", name=re.compile(r"^选择模型，当前 second-model"))).to_be_visible()
    assert host.rpc("get_state")["model"]["id"] == "second-model"
    composer = page.get_by_role("combobox", name="给 Agent 的消息", exact=True)
    composer.fill('/think medium')
    expect(page.get_by_role("option", name="均衡 中等推理强度", exact=True)).to_have_attribute("aria-disabled", "false")
    composer.press('Enter')
    expect(composer).to_have_value('')
    expect(page.get_by_role("slider", name="思考深度", exact=True)).to_have_attribute("aria-valuetext", "均衡")
    assert host.rpc("get_state")["thinkingLevel"] == "medium"
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("button", name=re.compile(r"^选择模型，当前")).click()
    expect(dialog).to_be_visible()
    assert_accessible(page)
    dialog.get_by_role("combobox", name="搜索模型", exact=True).press("Escape")
    expect(dialog).to_be_hidden()
    assert not errors


def test_sidebar_collapse_is_persistent_keyboard_accessible_and_interruptible(native_ui):
    page, _, _, errors = native_ui
    sidebar = page.locator("aside.sidebar")
    expect(sidebar).to_have_attribute("data-collapsed", "true")
    assert 60 <= sidebar.bounding_box()["width"] <= 68
    page.get_by_role("button", name="展开侧栏", exact=True).click()
    expect(sidebar).to_have_attribute("data-collapsed", "false")
    assert 208 <= sidebar.bounding_box()["width"] <= 216
    page.reload()
    expect(page.get_by_role("button", name="收起侧栏", exact=True)).to_be_visible()
    page.get_by_role("button", name="收起侧栏", exact=True).focus()
    page.keyboard.press("Control+b")
    expect(sidebar).to_have_attribute("data-collapsed", "true")
    page.emulate_media(reduced_motion="no-preference")
    for _ in range(3):
        page.get_by_role("button", name="展开侧栏", exact=True).click()
        page.get_by_role("button", name="收起侧栏", exact=True).click()
    page.wait_for_function("Math.abs(document.querySelector('aside.sidebar').getBoundingClientRect().width-64)<1")
    assert_accessible(page)
    assert not errors


def test_model_motion_frames_and_native_selection(native_ui):
    import json
    import re
    page, host, store, errors = native_ui
    for model, name in [("gpt-5.6-sol", "模型网关"), ("gpt-5.6-luna", "快速模型")]:
        store.save({"name": name, "base_url": "http://127.0.0.1:1/v1", "api": "openai-responses", "model": model, "api_key": "motion-contract-key"})
    host.update_gateways()
    host.control("start_agent")
    page.reload()
    expect(page.get_by_role("button", name=re.compile("^选择模型，当前 gpt-5.6-sol"))).to_be_visible()
    page.emulate_media(reduced_motion="no-preference")
    page.get_by_role("button", name=re.compile("^选择模型，当前")).click()
    frames = page.evaluate("""async () => {
      const values=[]; for(let i=0;i<28;i++){await new Promise(requestAnimationFrame);const el=document.querySelector('.model-popover');if(el){const rect=el.getBoundingClientRect();values.push({width:rect.width,top:rect.top,opacity:getComputedStyle(el).opacity})}}return values;
    }""")
    assert len({round(float(frame['opacity']), 2) for frame in frames}) > 2, frames
    expect(page.get_by_role("dialog", name="选择模型", exact=True)).to_be_visible()
    page.get_by_role("combobox", name="搜索模型", exact=True).fill("gpt-5.6")
    page.get_by_role("option", name=re.compile("gpt-5.6-luna")).hover()
    page.get_by_role("option", name=re.compile("gpt-5.6-sol")).hover()
    page.get_by_role("combobox", name="搜索模型", exact=True).press("Escape")
    expect(page.get_by_role("dialog", name="选择模型", exact=True)).to_be_hidden()
    # Close and reopen during contraction: the old timeline must not hide the new menu.
    button = page.get_by_role("button", name=re.compile("^选择模型，当前"))
    button.click()
    page.get_by_role("combobox", name="搜索模型", exact=True).press("Escape")
    button.click()
    expect(page.get_by_role("dialog", name="选择模型", exact=True)).to_be_visible()
    expect(page.get_by_role("combobox", name="搜索模型", exact=True)).to_be_focused()
    page.get_by_role("combobox", name="搜索模型", exact=True).press("Escape")
    expect(page.get_by_role("dialog", name="选择模型", exact=True)).to_be_hidden()
    slider = page.get_by_role("slider", name="思考深度", exact=True)
    expect(slider).to_have_attribute("aria-disabled", "false")
    slider.press('ArrowRight')
    expect(slider).to_have_attribute("aria-valuetext", "深入")
    expect(slider).to_have_attribute("aria-disabled", "false")
    assert host.rpc("get_state")["thinkingLevel"] == "high"
    page.get_by_role("button", name="展开侧栏", exact=True).click()
    page.wait_for_function("Math.abs(document.querySelector('aside.sidebar').getBoundingClientRect().width-212)<1")
    page.get_by_role("button", name="收起侧栏", exact=True).click()
    page.wait_for_function("Math.abs(document.querySelector('aside.sidebar').getBoundingClientRect().width-64)<1")
    Path('/tmp/morning-design-depth/model-animation-frames.json').write_text(json.dumps(frames))
    assert not errors


def test_thinking_drag_slash_and_native_commands(native_ui):
    """Real native session: preview without writes, one release write, slash without inference."""
    import json
    import re
    page, host, store, errors = native_ui
    store.save({"name": "模型网关", "base_url": "http://127.0.0.1:1/v1", "api": "openai-responses", "model": "gpt-5.6-sol", "api_key": "thinking-fixture-key"})
    host.update_gateways()
    host.control("start_agent")
    host.rpc("set_thinking_level", {"level": "medium"})
    levels = host.rpc("get_available_thinking_levels")["levels"]
    page.reload()
    trigger = page.get_by_role("slider", name="思考深度", exact=True)
    expect(trigger).to_have_attribute("aria-disabled", "false")
    page.emulate_media(reduced_motion="no-preference")
    writes = []
    chats = []
    page.on("request", lambda request: writes.append(request.post_data_json) if request.url.endswith('/api/pulse/pi') and request.method == 'POST' and request.post_data_json.get('command') == 'set_thinking_level' else None)
    page.on("request", lambda request: chats.append(request.post_data) if request.url.endswith('/api/pulse/chat') and request.method == 'POST' else None)
    box = trigger.bounding_box()
    page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    page.mouse.down()
    page.mouse.move(box['x'] + box['width'] / 2 + 200, box['y'] + box['height'] / 2, steps=15)
    assert page.get_by_role("dialog", name="思考深度", exact=True).count() == 0
    assert trigger.bounding_box()["width"] > box["width"] + 35
    assert host.rpc("get_state")["thinkingLevel"] == "medium"
    assert not writes
    page.screenshot(path='/tmp/morning-design-depth/thinking-stretched-1440.png')
    page.mouse.up()
    slider = trigger
    expect(slider).to_have_attribute("aria-valuenow", str(len(levels) - 1))
    expect(slider).to_have_attribute("aria-disabled", "false")
    assert host.rpc("get_state")["thinkingLevel"] == levels[-1]
    assert len(writes) == 1
    frames = page.evaluate("""async () => {const values=[]; for(let i=0;i<42;i++){await new Promise(requestAnimationFrame);values.push(document.querySelector('.thinking-elastic').getBoundingClientRect().width)}return values}""")
    assert len({round(frame, 1) for frame in frames}) > 5
    page.wait_for_function("Math.abs(document.querySelector('.thinking-elastic').getBoundingClientRect().width-84)<1")
    Path('/tmp/morning-design-depth/thinking-animation-frames.json').write_text(json.dumps(frames))
    page.screenshot(path='/tmp/morning-design-depth/thinking-resting-1440.png')
    assert min(frames) >= 75
    slider.press("Home")
    expect(slider).to_have_attribute("aria-valuenow", "0")
    expect(slider).to_have_attribute("aria-disabled", "false")
    assert host.rpc("get_state")["thinkingLevel"] == levels[0]
    # Adjust inside an existing draft and retain the rest of that draft.
    composer = page.get_by_role("combobox", name="给 Agent 的消息", exact=True)
    composer.fill("请分析 /")
    menu = page.get_by_role("listbox", name="对话快捷选择", exact=True)
    expect(menu.get_by_role("option", name=re.compile("思考深度"))).to_have_attribute("aria-disabled", "false")
    page.get_by_role("button", name="发送消息", exact=True).click()
    expect(composer).to_have_value("请分析 /think ")
    choices = page.get_by_role("listbox", name="选择思考深度", exact=True)
    assert choices.bounding_box()['height'] < 55
    page.wait_for_function("Number(getComputedStyle(document.querySelector('.composer-command-menu')).opacity) === 1")
    assert_accessible(page)
    choices.get_by_role("option", name="深入 较高推理强度", exact=True).click()
    expect(composer).to_have_value("请分析 ")
    expect(slider).to_have_attribute("aria-valuetext", "深入")
    assert host.rpc("get_state")["thinkingLevel"] == 'high'
    assert not chats
    # Real extension discovery remains available and is only inserted, never auto-run.
    composer.fill('/ui-check')
    expect(page.get_by_role("option", name="/ui-check Native dialog test", exact=True)).to_be_visible()
    composer.press("Enter")
    expect(composer).to_have_value('/ui-check ')
    assert not chats
    composer.press("Enter")
    expect(page.get_by_role("dialog", name="Native select", exact=True)).to_be_visible()
    page.get_by_role("dialog", name="Native select", exact=True).get_by_role("button", name="Cancel", exact=True).click()
    expect(page.get_by_role("dialog", name="Native select", exact=True)).to_be_hidden()
    # Mobile and keyboard access; reduced motion still lands exactly on the native value.
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(reduced_motion="reduce")
    expect(slider).to_be_visible()
    assert_accessible(page)
    page.screenshot(path='/tmp/morning-design-depth/thinking-390.png')
    composer.fill('/think med')
    expect(choices.get_by_role("option", name="均衡 中等推理强度", exact=True)).to_be_visible()
    composer.press("Enter")
    expect(composer).to_have_value('')
    assert host.rpc("get_state")["thinkingLevel"] == 'medium'
    assert len(chats) == 1
    page.reload()
    expect(page.get_by_role("slider", name="思考深度", exact=True)).to_have_attribute("aria-valuetext", "均衡")
    assert not errors

    Path('/tmp/morning-design-depth/thinking-interactions.json').write_text(json.dumps({'writes':len(writes),'native_extension_calls':len(chats),'levels':levels,'no_model_generation':True}))


def test_slash_preserves_native_command_name_and_model_capabilities(native_ui, tmp_path):
    import re
    page, host, store, errors = native_ui
    (tmp_path / 'agent/extensions/collision.js').write_text("export default function(pi){pi.registerCommand('think',{description:'An existing native command',handler:async()=>{}})}")
    for model, reasoning in [('reasoning-model', True), ('plain-model', False)]:
        store.save({"name": model, "base_url": "http://127.0.0.1:1/v1", "api": "openai-responses", "model": model,
                    "model_options": {"reasoning": reasoning}, "api_key": "capability-fixture-key"})
    host.update_gateways()
    host.control("start_agent")
    page.reload()
    composer = page.get_by_role('combobox', name='给 Agent 的消息', exact=True)
    composer.fill('/think')
    native = page.get_by_role('option', name='/think An existing native command', exact=True)
    expect(native).to_have_attribute('aria-disabled', 'false')
    assert page.get_by_role('option', name=re.compile('^思考深度 ')).count() == 0
    composer.press('Enter')
    expect(composer).to_have_value('/think ')
    assert page.get_by_role('listbox', name='选择思考深度').count() == 0
    composer.fill('/')
    page.get_by_role('option', name=re.compile('^思考深度 ')).click()
    expect(composer).to_have_value('/morning:think ')
    page.get_by_role('option', name='深入 较高推理强度', exact=True).click()
    expect(composer).to_have_value('')
    assert host.rpc('get_state')['thinkingLevel'] == 'high'
    plain = next(model for model in host.control('models') if model['id'] == 'plain-model')
    host.rpc('set_model', {'provider': plain['provider'], 'modelId': plain['id']})
    page.reload()
    expect(page.get_by_role('button', name=re.compile('^选择模型，当前 plain-model'))).to_be_visible()
    assert page.get_by_role('slider', name='思考深度').count() == 0
    composer.fill('/')
    expect(page.get_by_role('option', name='/think An existing native command', exact=True)).to_be_visible()
    assert page.get_by_role('option', name=re.compile('^思考深度 ')).count() == 0
    assert_accessible(page)
    assert not errors


def test_thinking_failure_and_pointer_cancel_preserve_native_state(native_ui):
    page, host, store, errors = native_ui
    store.save({"name": "Thinking", "base_url": "http://127.0.0.1:1/v1", "api": "openai-responses", "model": "gpt-5.6-sol", "api_key": "thinking-fixture-key"})
    host.update_gateways()
    host.control("start_agent")
    host.rpc("set_thinking_level", {"level": "medium"})
    page.reload()
    trigger = page.get_by_role("slider", name="思考深度", exact=True)
    expect(trigger).to_have_attribute("aria-disabled", "false")
    box = trigger.bounding_box()
    page.mouse.move(box['x'] + 30, box['y'] + 15)
    page.mouse.down()
    page.mouse.move(box['x'] + 110, box['y'] + 15, steps=4)
    page.keyboard.press("Escape")
    page.mouse.up()
    assert host.rpc("get_state")["thinkingLevel"] == 'medium'
    assert page.get_by_role("dialog", name="思考深度", exact=True).count() == 0
    # Fault injection tests only the error branch; success paths use the real Pi above.
    def unavailable(route):
        if route.request.post_data_json.get('command') == 'set_thinking_level':
            route.fulfill(status=503, content_type='application/json', body='{"error":{"message":"Pi 暂时无法保存思考深度"}}')
        else:
            route.continue_()
    page.route('**/api/pulse/pi', unavailable)
    composer = page.get_by_role("combobox", name="给 Agent 的消息", exact=True)
    composer.fill('/think high')
    choices = page.get_by_role("listbox", name="选择思考深度", exact=True)
    expect(choices.get_by_role("option", name="深入 较高推理强度", exact=True)).to_have_attribute("aria-disabled", "false")
    composer.press('Enter')
    expect(page.locator('.composer-command-menu').get_by_role('alert')).to_contain_text('Pi 暂时无法保存')
    expect(composer).to_have_value('/think high')
    assert host.rpc('get_state')['thinkingLevel'] == 'medium'
    page.unroute('**/api/pulse/pi', unavailable)
    composer.press('Enter')
    expect(composer).to_have_value('')
    assert host.rpc('get_state')['thinkingLevel'] == 'high'
    assert not errors
