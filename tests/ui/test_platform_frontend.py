"""Real HTTP, SQLite, filesystem events and browser routes; no mocked console API."""
import threading
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import pytest
from playwright.sync_api import expect, sync_playwright

from wakecore.app.bootstrap import build
from wakecore.app.pulse.server import LocalPulseApp

ROOT = Path(__file__).resolve().parents[2]


class Server(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def platform_page(tmp_path, request):
    kernel = build(db_url=f"sqlite:///{tmp_path}/kernel.db", with_model=False)
    app = LocalPulseApp(kernel, state_dir=tmp_path, platform_enabled=True, dist=ROOT / "apps/pulse/dist")
    server = make_server("127.0.0.1", 0, app, server_class=Server, handler_class=Quiet)
    base = f"http://127.0.0.1:{server.server_port}"
    app.allowed_hosts.add(f"127.0.0.1:{server.server_port}")
    app.platform.start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900}, reduced_motion="reduce",
            **({"record_video_dir": str(tmp_path / "video"), "record_video_size": {"width": 1440, "height": 900}} if request.node.name == "test_pulse_morph_keyboard_close_resize_and_real_pause" else {}))
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base)
        expect(page.get_by_role("button", name="Watch", exact=True)).to_be_visible()
        yield page, app, tmp_path, base, errors
        if page.video:
            video = page.video
            page.close()
            target = Path('/tmp/morning-design-depth/pulse-motion.webm')
            target.parent.mkdir(parents=True, exist_ok=True)
            video.save_as(str(target))
        browser.close()
    app.platform.close()
    server.shutdown()
    server.server_close()
    kernel.ctx.store.close()


def test_watch_start_pause_edit_resume_event_and_archive(platform_page):
    page, app, root, base, errors = platform_page
    target = root / "status.txt"
    target.write_text("pending")
    inspection = app.platform.command({"operation": "inspect_source", "source": {"kind": "file", "target": str(target)}})
    proposed = app.platform.command({"operation": "prepare_watch", "inspection_id": inspection["inspection_id"], "label": "交付文件", "condition": "文件变为 READY", "field": "value", "op": "eq", "value": "READY", "interval_seconds": 2})
    key = proposed["draft"]["task_id"]
    page.goto(base + "#watch/" + key)
    page.get_by_role("button", name="开始驻守", exact=True).click()
    expect(page.get_by_text("驻守中", exact=True)).to_be_visible(timeout=20000)
    page.get_by_role("button", name="暂停", exact=True).click()
    expect(page.get_by_role("button", name="恢复", exact=True)).to_be_visible()
    page.get_by_role("button", name="修改目标", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("检查间隔 · 秒", exact=True).fill("3")
    dialog.get_by_role("button", name="确认并保存", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_role("button", name="恢复", exact=True)).to_be_visible()
    page.get_by_role("button", name="恢复", exact=True).click()
    expect(page.get_by_role("button", name="暂停", exact=True)).to_be_visible()
    # The UI route is now unrelated to Watch; the worker and OS events still run.
    page.get_by_role("button", name="Computer", exact=True).click()
    target.write_text("READY")
    page.get_by_role("button", name="Tasks", exact=True).click()
    page.get_by_role("button", name="通知", exact=False).click()
    expect(page.get_by_role("heading", name="交付文件有变化", exact=True)).to_be_visible(timeout=30000)
    page.get_by_role("link", name="查看驻守", exact=True).click()
    expect(page.get_by_role("heading", name="事件时间线", exact=True)).to_be_visible()
    page.get_by_role("button", name="结束并移除", exact=True).click()
    expect(page.get_by_role("heading", name="驻守任务", exact=True)).to_be_visible()
    assert app.api.service.get_task(app.principal, key)["lifecycle"] == "CANCELLED"
    assert not errors


def test_platform_routes_responsive_accessible_and_module_independence(platform_page):
    page, app, root, base, errors = platform_page
    for width, height in [(1440, 900), (390, 844)]:
        page.set_viewport_size({"width": width, "height": height})
        for route in ("computer", "watch", "tasks", "devices", "settings"):
            page.goto(base + "#" + route)
            heading = {"computer":"电脑控制", "watch":"驻守任务", "tasks":"任务与记录", "devices":"设备与连接", "settings":"设置"}[route]
            expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
            page.wait_for_function("document.getAnimations().every(a=>a.constructor.name!=='CSSTransition'||a.playState!=='running')")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.add_script_tag(path=str(ROOT / "apps/pulse/node_modules/axe-core/axe.min.js"))
            violations = page.evaluate("async()=> (await axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa']}})).violations.map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)}))")
            assert not violations, (route, width, violations)
            page.screenshot(path=str(root / f"platform-{route}-{width}.png"))
    page.get_by_role("combobox", name="设置分类", exact=True).select_option("appearance")
    with page.expect_response(lambda response: response.url.endswith("/pulse/settings") and response.request.method == "POST"):
        page.get_by_role("radio", name="深色", exact=True).click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    for width, height in [(1440, 900), (390, 844)]:
        page.set_viewport_size({"width": width, "height": height})
        for route in ("computer", "watch", "tasks", "devices", "settings"):
            page.goto(base + "#" + route)
            heading = {"computer":"电脑控制", "watch":"驻守任务", "tasks":"任务与记录", "devices":"设备与连接", "settings":"设置"}[route]
            expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
            page.wait_for_function("document.getAnimations().every(a=>a.constructor.name!=='CSSTransition'||a.playState!=='running')")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.add_script_tag(path=str(ROOT / "apps/pulse/node_modules/axe-core/axe.min.js"))
            violations = page.evaluate("async()=> (await axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa']}})).violations.map(v=>({id:v.id,nodes:v.nodes.map(n=>({target:n.target,summary:n.failureSummary}))}))")
            page.screenshot(path=str(root / f"platform-{route}-{width}-dark.png"))
            if violations:
                import json
                diagnostics = page.evaluate("() => ({theme:document.documentElement.dataset.theme,root:getComputedStyle(document.documentElement).color,brand:getComputedStyle(document.querySelector('.brand')).color,wordmark:getComputedStyle(document.querySelector('.brand>span:nth-child(2)')).color,animations:document.getAnimations().map(a=>({state:a.playState,type:a.constructor.name}))})")
                (root / "contrast-diagnostics.json").write_text(json.dumps({"violations":violations,"computed":diagnostics},ensure_ascii=False,indent=2))
            assert not violations, violations
            page.screenshot(path=str(root / f"platform-{route}-{width}-dark.png"))
    page.get_by_role("combobox", name="设置分类", exact=True).select_option("background")
    page.get_by_role("button", name="后台能力与系统提醒", exact=False).click()
    with page.expect_response(lambda response: response.url.endswith("/pulse/platform") and response.request.method == "POST"):
        page.get_by_label("启用 Computer Use", exact=True).uncheck()
    with page.expect_response(lambda response: response.url.endswith("/pulse/platform") and response.request.method == "POST"):
        page.get_by_label("启用 Watch", exact=True).uncheck()
    expect(page.get_by_label("启用 Watch", exact=True)).not_to_be_checked()
    assert not app.platform.modules["computer"] and not app.platform.modules["watch"]
    page.goto(base + "#chat")
    expect(page.get_by_role("textbox", name="给 Agent 的消息", exact=True)).to_be_visible()
    assert not errors


def test_pulse_morph_keyboard_close_resize_and_real_pause(platform_page):
    import json
    import re
    page, app, root, base, errors = platform_page
    target = root / "delivery.txt"
    target.write_text("等待新版本")
    inspection = app.platform.command({"operation": "inspect_source", "source": {"kind": "file", "target": str(target)}})
    proposal = app.platform.command({"operation": "prepare_watch", "inspection_id": inspection["inspection_id"], "label": "交付文件", "condition": "交付内容有变化时通知我", "field": "value", "op": "change", "interval_seconds": 5})
    key = proposal['draft']['task_id']
    page.goto(base + '#watch/' + key)
    page.get_by_role('button', name='开始驻守', exact=True).click()
    expect(page.get_by_text('驻守中', exact=True)).to_be_visible(timeout=20000)
    page.get_by_role('button', name='暂停', exact=True).click()
    expect(page.get_by_role('button', name='恢复', exact=True)).to_be_visible()
    before = app.api.service.get_task(app.principal, key)
    page.goto(base + '#chat')
    page.emulate_media(reduced_motion='no-preference')
    page.get_by_role('button', name=re.compile('^Pulse：')).click()
    frames = page.evaluate("""async () => {
      const values=[]; for(let i=0;i<34;i++){await new Promise(requestAnimationFrame);const el=document.querySelector('.pulse-panel');if(el){const r=el.getBoundingClientRect();values.push({width:r.width,height:r.height,top:r.top})}}return values;
    }""")
    assert len({round(frame['width']) for frame in frames}) > 4, frames
    expect(page.get_by_role('region', name='驻守详情', exact=True)).to_be_visible()
    page.get_by_role('button', name='收起后台活动', exact=True).click()
    expect(page.locator('.pulse-panel')).to_be_hidden()
    page.get_by_role('button', name=re.compile('^Pulse：')).click()
    expect(page.get_by_role('region', name='驻守详情', exact=True)).to_be_visible()
    page.set_viewport_size({'width':390,'height':844})
    page.wait_for_function("document.querySelector('.pulse-panel').getBoundingClientRect().width <= innerWidth-26")
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.keyboard.press('Escape')
    expect(page.locator('.pulse-panel')).to_be_hidden()
    after = app.api.service.get_task(app.principal,key)
    assert after['lifecycle'] == 'PAUSED' and after['version'] == before['version']
    Path('/tmp/morning-design-depth/pulse-animation-frames.json').write_text(json.dumps(frames))
    assert not errors
