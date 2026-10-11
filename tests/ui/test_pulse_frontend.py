"""Built React UI against the real browser sidecar and kernel, no fabricated UI states."""
import threading
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import pytest
from playwright.sync_api import sync_playwright
from test_pulse import world as world

from wakecore.app.worker.loop import run_until_idle

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "apps" / "pulse" / "dist"
AXE = ROOT / "apps" / "pulse" / "node_modules" / "axe-core" / "axe.min.js"
pytestmark = pytest.mark.skipif(not (DIST / "index.html").exists(), reason="build apps/pulse before frontend QA")


class ThreadedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


class FixtureInterpreter:
    """A deterministic model double; real-model negation checks run separately, not in CI."""
    configured = True
    data_egress_target = "model:local-watch-plan"

    def __init__(self, url):
        self.url = url

    def interpret(self, messages, *, page=None, **kwargs):
        from wakecore.adapters.models.semantic import PROPOSAL_SCHEMA

        result = dict.fromkeys(PROPOSAL_SCHEMA["properties"])
        result.update(status="inspect", url=self.url, message="我先确认你关注的课程。", confidence=.99)
        if page:
            seats = next(c["id"] for c in page["candidates"] if c["selector"] == "#seats")
            selected = next(c["id"] for c in page["candidates"] if c["label"] == "已选")
            result.update(status="proposal", label="软件工程", message="我会关注软件工程是否出现名额，出现后准备选课，提交前询问你。",
                wait_summary="软件工程出现可选名额", observation_id=seats, mode="deterministic", value_type="number", op="gt", value=0,
                action_goal="选择软件工程并确认已选", verification_observation_id=selected,
                verification_type="text", verification_op="eq", verification_value="是", interval_seconds=3)
        return result


@pytest.fixture
def ui(world):
    app = world[3]
    app.api.agent.interpreter = FixtureInterpreter(world[0].url)
    app.dist = DIST
    http = make_server("127.0.0.1", 0, app, server_class=ThreadedServer, handler_class=QuietHandler)
    origin = f"http://127.0.0.1:{http.server_port}"
    app.allowed_hosts.add(f"127.0.0.1:{http.server_port}")
    threading.Thread(target=http.serve_forever, daemon=True).start()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(origin)
        page.get_by_text("从一句话开始。", exact=True).wait_for()
        yield page, errors, world
        browser.close()
    http.shutdown()
    http.server_close()


def no_axe_errors(page):
    if not AXE.exists():
        pytest.skip("npm ci in apps/pulse installs axe-core")
    if not page.evaluate("typeof axe !== 'undefined'"):
        page.add_script_tag(path=str(AXE))
    errors = page.evaluate("async () => (await axe.run(document, {runOnly:{type:'tag', values:['wcag2a','wcag2aa','wcag21a','wcag21aa']}})).violations")
    assert not errors, [(error["id"], [node["target"] for node in error["nodes"]]) for error in errors]


def make_demo_watch(page, world):
    assert page.get_by_role("combobox").count() == 0
    assert page.get_by_role("button", name="等出现空位").count() == 0
    page.get_by_role("textbox", name="给 Agent 的消息").fill("软件工程有人退课就帮我准备选课，提交前问我。" + world[0].url)
    page.get_by_role("button", name="发送消息").click()
    page.get_by_role("button", name="开始驻守", exact=True).wait_for()
    no_axe_errors(page)
    page.get_by_role("button", name="开始驻守", exact=True).click()
    page.get_by_text("已开始。我会持续关注相关内容，符合你确认的要求时再处理。", exact=True).wait_for()
    run_until_idle(world[2].ctx)
    page.locator('[data-testid="pulse"][data-state="watching"]').wait_for()


def test_quiet_hover_details_approval_postverify_mobile_and_accessibility(ui, tmp_path):
    page, errors, world = ui
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    page.emulate_media(reduced_motion="no-preference")
    page.get_by_role("button", name="重播分层日出 Logo", exact=True).click()
    page.wait_for_function("!document.querySelector('.welcome-art .morning-logo').classList.contains('is-intro')", timeout=6000)
    page.emulate_media(reduced_motion="reduce")
    no_axe_errors(page)
    page.screenshot(path=str(artifacts / "empty-desktop.png"))
    make_demo_watch(page, world)
    island = page.locator('[data-testid="pulse"]')
    assert island.bounding_box()["height"] < 60
    page.screenshot(path=str(artifacts / "watch-desktop.png"))
    page.get_by_role("button", name="Pulse：").hover()
    assert "软件工程" in island.inner_text()
    page.get_by_role("button", name="Pulse：").click()
    page.get_by_role("button", name="暂停", exact=True).wait_for()
    no_axe_errors(page)
    page.screenshot(path=str(artifacts / "task-desktop.png"))
    page.get_by_role("button", name="Pulse：").click()
    world[0].control({"seats": 1})
    world[5].advance(3)
    run_until_idle(world[2].ctx)
    page.locator('[data-testid="pulse"][data-state="approval"]').wait_for()
    no_axe_errors(page)
    page.screenshot(path=str(artifacts / "approval-desktop.png"))
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    no_axe_errors(page)
    page.screenshot(path=str(artifacts / "approval-mobile.png"))
    page.get_by_role("button", name="批准这次操作").click()
    page.wait_for_function("!document.querySelector('.approve-button')")
    run_until_idle(world[2].ctx)
    world[5].advance(3)
    run_until_idle(world[2].ctx)
    page.locator('[data-testid="pulse"][data-state="completed"]').wait_for()
    assert world[0].state()[-1] == 1
    assert island.bounding_box()["height"] < 60
    no_axe_errors(page)
    page.screenshot(path=str(artifacts / "complete-mobile.png"))
    assert not errors


def test_keyboard_modal_focus_and_pause_resume_controls(ui):
    page, errors, world = ui
    make_demo_watch(page, world)
    pulse = page.get_by_role("button", name="Pulse：")
    pulse.focus()
    page.keyboard.press("Enter")
    page.get_by_role("button", name="暂停", exact=True).click()
    page.locator('[data-testid="pulse"][data-state="paused"]').wait_for()
    page.get_by_role("button", name="继续", exact=True).click()
    run_until_idle(world[2].ctx)
    page.locator('[data-testid="pulse"][data-state="watching"]').wait_for()
    page.get_by_role("button", name="驻守设置", exact=True).click()
    page.get_by_role("dialog").wait_for()
    for _ in range(15):
        page.keyboard.press("Tab")
        assert page.evaluate("document.querySelector('[role=dialog]').contains(document.activeElement)")
    no_axe_errors(page)
    page.keyboard.press("Escape")
    page.get_by_role("dialog").wait_for(state="hidden")
    assert not errors


def test_agent_tool_stream_results_and_responsive_conversation(ui, tmp_path):
    """Exercise live UI event rendering with a runtime double; model execution is tested separately."""
    page, errors, world = ui

    class AgentInterpreter(FixtureInterpreter):
        def interpret(self, messages, **kwargs):
            result = super().interpret(messages, **kwargs)
            result.update(status="agent")
            return result

    release = threading.Event()

    class RuntimeDouble:
        configured = True
        workspace = tmp_path

        def run(self, message, on_event):
            on_event({"type": "tool_execution_start", "tool": "morning_bash", "call_id": "verify", "args": {"command": "python3 -m unittest"}})
            assert release.wait(20)
            on_event({"type": "tool_execution_end", "tool": "morning_bash", "call_id": "verify", "is_error": False, "output": "Ran 2 tests\nOK"})
            on_event({"type": "text_delta", "text": "已完成，2 项测试通过。"})
            on_event({"type": "assistant_message", "text": "已完成，2 项测试通过。"})
            return {"type": "complete", "error": None}

    world[3].api.agent.interpreter = AgentInterpreter(world[0].url)
    world[3].api.agent.coding_runtime = RuntimeDouble()
    page.emulate_media(reduced_motion="no-preference")
    page.context.clear_cookies()
    page.get_by_role("textbox", name="给 Agent 的消息").fill("修改项目并运行测试")
    page.get_by_role("button", name="发送消息").click()
    step = page.get_by_test_id("tool-row")
    try:
        step.get_by_text("进行中", exact=True).wait_for()
        assert page.get_by_role("button", name="发送消息").is_disabled()
        assert page.get_by_text("Agent 正在处理你的任务…", exact=True).is_visible()
        logo = page.locator(".composer-identity .morning-logo svg").element_handle()
        page.wait_for_function("""() => {
            const sun = document.querySelector('.composer-identity [data-live="sun"]');
            return sun?.getAnimations().some(animation => animation.currentTime > 1400);
        }""", timeout=8000)
        assert logo.evaluate("node => node.isConnected"), "Polling must not replace the SVG and restart its layers"
    finally:
        release.set()
    page.get_by_text("已完成，2 项测试通过。", exact=True).wait_for()
    assert page.get_by_text("已完成，2 项测试通过。", exact=True).count() == 1
    assert step.get_by_text("已完成", exact=True).is_visible()
    assert not step.locator("pre").is_visible()
    step.get_by_role("button").click()
    assert "Ran 2 tests" in step.locator("pre").inner_text()
    no_axe_errors(page)
    page.screenshot(path=str(tmp_path / "agent-desktop.png"))
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    no_axe_errors(page)
    page.screenshot(path=str(tmp_path / "agent-mobile.png"))
    assert not errors


def test_markdown_retry_navigation_and_theme_persistence(ui, tmp_path):
    page, errors, world = ui
    agent = world[3].api.agent
    markdown = "## 验证结果\n\n已保留 **中文列名**。\n\n```python\ncolumns = ['日期', '结果']\nprint(columns)\n```\n\n| 检查 | 结果 |\n| --- | --- |\n| 中文 | 通过 |\n\n<script>window.injected = true</script>"
    messages = [agent._message("user", "整理检查结果"), agent._message("agent", markdown, type="reply"),
                agent._message("user", "继续检查日期格式"), agent._message("agent", "连接暂时中断。", type="error")]
    agent.store.update(lambda data: data.update(messages=messages))
    page.reload()
    page.get_by_role("heading", name="验证结果", exact=True).wait_for()
    assert "columns = ['日期', '结果']" in page.locator(".code-block pre").inner_text()
    assert page.get_by_role("table").get_by_role("cell", name="通过", exact=True).is_visible()
    assert page.evaluate("typeof window.injected") == "undefined"
    page.get_by_role("button", name="编辑后重试", exact=True).click()
    assert page.get_by_role("textbox", name="给 Agent 的消息").input_value() == "继续检查日期格式"
    assert len(agent.store.snapshot()["messages"]) == 4  # Editing is not an automatic retry.
    no_axe_errors(page)
    page.screenshot(path=str(tmp_path / "markdown-desktop.png"))
    page.get_by_role("button", name="设置", exact=True).click()
    page.get_by_role("radio", name="深色", exact=True).click()
    page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
    no_axe_errors(page)
    page.reload()
    page.get_by_role("button", name="设置", exact=True).click()
    page.get_by_role("radio", name="深色", exact=True).wait_for()
    assert page.get_by_role("radio", name="深色", exact=True).get_attribute("aria-checked") == "true"
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    no_axe_errors(page)
    page.screenshot(path=str(tmp_path / "settings-dark-mobile.png"))
    page.get_by_role("button", name="打开导航", exact=True).click()
    for _ in range(10):
        page.keyboard.press("Tab")
        assert page.evaluate("document.querySelector('aside').contains(document.activeElement)")
    page.keyboard.press("Escape")
    assert page.get_by_role("button", name="打开导航", exact=True).get_attribute("aria-expanded") == "false"
    assert not errors
