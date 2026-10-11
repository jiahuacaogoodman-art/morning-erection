"""Real Chromium -> local HTTP -> durable kernel -> Pulse, approval and reconnect."""
import io
import json
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

import pytest

from wakecore.adapters.clock import FakeClock
from wakecore.adapters.models.watch_plan import WatchPlan
from wakecore.adapters.ui_runtime.client import UiRuntimeClient
from wakecore.app.bootstrap import build
from wakecore.app.pulse.server import LocalPulseApp
from wakecore.app.worker.loop import run_until_idle
from wakecore.kernel.domain.errors import NotFound
from wakecore.kernel.domain.model import ModelCall
from wakecore.kernel.locks import tx
from wakecore.kernel.service import Principal
from wakecore_ui_runtime.cua.openai import ResponsesClient
from wakecore_ui_runtime.server import Runtime, make_handler
from wakecore_ui_runtime.testing.pulse_demo import CourseDemo, CourseDemoTool


class LocalClient:
    def __init__(self, app):
        self.app, self.cookie = app, ""

    def call(self, method, path, body=None, *, key=None, host="127.0.0.1:8788", origin=None, cookie=True):
        data = json.dumps(body).encode() if body is not None else b""
        env = {"REQUEST_METHOD": method, "PATH_INFO": "/api" + path, "CONTENT_LENGTH": str(len(data)),
               "wsgi.input": io.BytesIO(data), "HTTP_HOST": host, "HTTP_COOKIE": self.cookie if cookie else "",
               "HTTP_ORIGIN": origin or f"http://{host}", "HTTP_X_PULSE_CLIENT": "1"}
        if key:
            env["HTTP_IDEMPOTENCY_KEY"] = key
        got = {}

        def start(status, headers):
            got["status"] = int(status.split()[0])
            got["headers"] = dict(headers)
        result = json.loads(b"".join(self.app(env, start)))
        if "Set-Cookie" in got["headers"]:
            self.cookie = got["headers"]["Set-Cookie"].split(";", 1)[0]
        return got["status"], result


@pytest.fixture
def world(tmp_path):
    demo = CourseDemo(tmp_path / "course.db", port=0)
    rt = Runtime(str(tmp_path / "browser"), client=ResponsesClient(api_key=""), trace=False)
    http = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(rt))
    threading.Thread(target=http.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{http.server_address[1]}"
    clock = FakeClock(datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc))
    kernel = build(db_url=f"sqlite:///{tmp_path / 'kernel.db'}", clock=clock, with_model=False, ui_runtime_url=url)
    kernel.ctx.reasoning = WatchPlan(kernel.ctx.store)
    policy = kernel.ctx.system_policy
    kernel.ctx.system_policy = replace(policy, allowed_capabilities=policy.allowed_capabilities | {"demo.course.enroll"},
                                       allowed_data_egress=policy.allowed_data_egress | {"model:local-watch-plan"})
    tool = CourseDemoTool(demo, rt)
    kernel.ctx.tools[tool.descriptor.tool_id] = tool
    app = LocalPulseApp(kernel, client=UiRuntimeClient(url), demo=demo, state_dir=tmp_path)
    client = LocalClient(app)
    client.call("GET", "/bootstrap")
    yield demo, rt, kernel, app, client, clock
    rt.human_login.close_all()
    rt.sessions.close_all()
    http.shutdown()
    http.server_close()
    demo.close()
    kernel.ctx.store.close()


def start_watch(world, action=True):
    demo, _, kernel, app, client, _ = world
    request = {"url": demo.url, "prompt": "等软件工程有空位", "label": "软件工程", "demo_course": True,
               "demo_action": action, "rule": "gt", "threshold": 0, "value_type": "number", "min_seconds": 3, "max_seconds": 12}
    status, draft = client.call("POST", "/pulse/draft", request, key="draft")
    assert status == 201, draft
    assert client.call("POST", "/pulse/draft", request, key="draft") == (201, draft)
    assert len(app.api.service.list_tasks(app.principal)) == 1
    assert draft["current"]["seats"] == 0
    status, result = client.call("POST", f"/pulse/watches/{draft['task_id']}/start",
                                {"spec_digest": draft["spec_digest"]}, key="start")
    assert status == 200, result
    run_until_idle(kernel.ctx)
    return draft["task_id"]


def pulse(world, task_id):
    return world[3].api.service.pulse(world[3].principal, task_id)


def test_real_browser_condition_approval_exact_content_and_one_submit(world):
    task_id = start_watch(world)
    demo, _, kernel, _, client, clock = world
    assert pulse(world, task_id)["state"] == "watching"
    with tx(kernel.ctx.store) as repo:
        assert repo.find(ModelCall, {}) == []
    demo.control({"seats": 1})
    clock.advance(3)
    run_until_idle(kernel.ctx)
    state = pulse(world, task_id)
    assert state["state"] == "approval" and state["condition_met"]
    assert demo.state()[-1] == 0
    approval = state["pending_approvals"][0]
    path = f"/approvals/{approval['approval_id']}/approve"
    assert client.call("POST", path, {"payload_digest": "sha256:wrong"})[0] == 409
    assert demo.state()[-1] == 0
    assert client.call("POST", path, {"payload_digest": approval["payload_digest"]}, key="approve")[0] == 200
    run_until_idle(kernel.ctx)
    assert demo.state()[-1] == 1
    assert any(m["status"] == "CONFIRMED" for m in pulse(world, task_id)["history"] if m["type"] == "action")
    clock.advance(3)
    run_until_idle(kernel.ctx)
    assert pulse(world, task_id)["state"] == "completed"
    client.call("POST", path, {"payload_digest": approval["payload_digest"]}, key="approve")
    run_until_idle(kernel.ctx)
    assert demo.state()[-1] == 1


def test_local_instances_keep_independent_cookies_on_shared_host(world):
    client = world[4]
    first = client.cookie
    second_app = LocalPulseApp(world[2], client=world[3].api.client, allowed_hosts=["127.0.0.1:8789"])
    other = LocalClient(second_app)
    assert other.call("GET", "/bootstrap", host="127.0.0.1:8789")[0] == 200
    second = other.cookie
    assert first.split("=", 1)[0] != second.split("=", 1)[0]
    client.cookie = other.cookie = first + "; " + second
    assert client.call("GET", "/pulse/state")[0] == 200
    assert other.call("GET", "/pulse/state", host="127.0.0.1:8789")[0] == 200
    client.cookie = second
    assert client.call("GET", "/pulse/state")[0] == 401


def test_login_wall_holds_old_value_stops_checks_and_recovers(world):
    task_id = start_watch(world, action=False)
    demo, _, kernel, _, client, clock = world
    demo.control({"auth": False})
    clock.advance(3)
    run_until_idle(kernel.ctx)
    blocked = pulse(world, task_id)
    assert blocked["state"] == "attention" and blocked["polling_suspended"]
    assert blocked["records"]["SE"]["seats"] == 0 and blocked["stale"]
    count = kernel.web.fetch_count
    clock.advance(100)
    run_until_idle(kernel.ctx)
    assert kernel.web.fetch_count == count
    demo.control({"auth": True})
    assert client.call("POST", f"/tasks/{task_id}/check", {})[0] == 200
    run_until_idle(kernel.ctx)
    assert pulse(world, task_id)["state"] == "watching" and not pulse(world, task_id)["stale"]


def test_http_429_honours_retry_after_and_cannot_be_bypassed(world):
    task_id = start_watch(world, action=False)
    demo, _, kernel, _, client, clock = world
    demo.control({"mode": "429"})
    clock.advance(3)
    run_until_idle(kernel.ctx)
    state = pulse(world, task_id)
    assert state["state"] == "reconnecting" and state["interval_seconds"] >= 15
    before = kernel.web.fetch_count
    clock.advance(4)
    status, response = client.call("POST", f"/tasks/{task_id}/check", {})
    assert status == 200 and response["reason"] == "server_backoff"
    run_until_idle(kernel.ctx)
    assert kernel.web.fetch_count == before


def test_generic_picker_and_scalar_changes(world):
    demo, _, kernel, _, client, clock = world
    status, picked = client.call("POST", "/pulse/inspect", {"url": demo.url, "prompt": "等网页内容更新"})
    assert status == 200 and any(c["selector"] == "#seats" for c in picked["candidates"])
    status, draft = client.call("POST", "/pulse/draft", {
        "url": demo.url, "prompt": "等余量变化", "selector": "#seats", "rule": "change",
        "value_type": "number", "min_seconds": 3, "max_seconds": 12, "label": "通用数值驻守"})
    assert status == 201, draft
    client.call("POST", f"/pulse/watches/{draft['task_id']}/start", {"spec_digest": draft["spec_digest"]})
    run_until_idle(kernel.ctx)
    demo.control({"seats": 2})
    clock.advance(3)
    run_until_idle(kernel.ctx)
    state = pulse(world, draft["task_id"])
    assert state["changes"][-1]["changes"][0]["new"]["value"] == 2
    assert demo.state()[-1] == 0


def test_whole_page_text_can_be_watched_without_a_site_specific_selector(world):
    demo, _, kernel, _, client, clock = world
    status, draft = client.call("POST", "/pulse/draft", {"url": demo.url, "prompt": "等页面正文变化", "selector": "body",
        "rule": "change", "value_type": "text", "min_seconds": 3, "max_seconds": 12, "label": "整页正文"})
    assert status == 201, draft
    assert "软件工程" in draft["current"]["value"]
    client.call("POST", f"/pulse/watches/{draft['task_id']}/start", {"spec_digest": draft["spec_digest"]})
    run_until_idle(kernel.ctx)
    demo.control({"seats": 3})
    clock.advance(3)
    run_until_idle(kernel.ctx)
    state = pulse(world, draft["task_id"])
    assert "3" in state["changes"][-1]["changes"][0]["new"]["value"]


def test_local_session_origin_owner_and_restart_boundaries(world, tmp_path):
    task_id = start_watch(world, action=False)
    demo, _, kernel, app, client, _ = world
    assert client.call("GET", "/pulse/state", cookie=False)[0] == 401
    assert client.call("GET", "/bootstrap", host="evil.example")[0] == 403
    assert client.call("POST", f"/tasks/{task_id}/pause", {}, origin="https://evil.example")[0] == 403
    with pytest.raises(NotFound):
        app.api.service.pulse(Principal("pulse_local", "user:other"), task_id)
    restarted = LocalPulseApp(kernel, client=app.api.client, demo=demo, state_dir=tmp_path)
    assert restarted.api.service.pulse(restarted.principal, task_id)["records"]["SE"]["seats"] == 0
    assert kernel.secrets.resolve("pulse_local", task_id)


@pytest.mark.parametrize("decision", ["reject", "expire"])
def test_rejected_and_expired_approvals_never_submit(world, decision):
    task_id = start_watch(world)
    demo, _, kernel, _, client, clock = world
    demo.control({"seats": 1})
    clock.advance(3)
    run_until_idle(kernel.ctx)
    approval = pulse(world, task_id)["pending_approvals"][0]
    if decision == "reject":
        assert client.call("POST", f"/approvals/{approval['approval_id']}/reject", {"reason": "no"})[0] == 200
    else:
        clock.advance(86401)
    run_until_idle(kernel.ctx)
    assert demo.state()[-1] == 0
    assert not pulse(world, task_id)["pending_approvals"]
    if decision == "expire":
        assert pulse(world, task_id)["approval_expired"] and pulse(world, task_id)["state"] == "attention"
    assert client.call("POST", f"/approvals/{approval['approval_id']}/approve", {"payload_digest": approval["payload_digest"]})[0] == 409
    run_until_idle(kernel.ctx)
    assert demo.state()[-1] == 0


def test_human_login_owns_profile_and_only_a_new_read_proves_recovery(world):
    task_id = start_watch(world, action=False)
    demo, rt, kernel, _, client, clock = world
    demo.control({"auth": False})
    clock.advance(3)
    run_until_idle(kernel.ctx)
    rt.human_login.headless = True  # a real browser, without opening a window in CI
    status, response = client.call("POST", "/pulse/login", {"task_id": task_id, "url": demo.url})
    assert status == 200 and response["state"] == "opening"
    ref = kernel.secrets.resolve("pulse_local", task_id)
    deadline = time.monotonic() + 10
    while rt.session(ref)["state"] == "opening" and time.monotonic() < deadline:
        time.sleep(.05)
    assert rt.session(ref)["state"] == "human_login"
    observed = rt.observe({"session_ref": ref, "url": demo.url, "origins": [demo.origin], "extractor": demo.extractor})
    assert observed["outcome"] == "AUTH_REQUIRED" and observed["error_code"] == "human_login_in_progress"
    assert client.call("POST", "/pulse/login", {"task_id": task_id, "url": demo.url, "finish": True})[1]["state"] == "closed"
    # Closing a login window is not proof of authentication.
    assert pulse(world, task_id)["source"]["health"] == "AUTH_REQUIRED"
    demo.control({"auth": True})
    clock.advance(3)
    client.call("POST", f"/tasks/{task_id}/check", {})
    run_until_idle(kernel.ctx)
    assert pulse(world, task_id)["state"] == "watching"


def test_settings_are_atomic_idempotent_and_preserve_pause(world):
    task_id = start_watch(world, action=False)
    _, _, _, app, client, _ = world
    before = pulse(world, task_id)
    client.call("POST", f"/tasks/{task_id}/pause", {"expected_version": before["version"]})
    paused = pulse(world, task_id)
    body = {"spec_digest": paused["spec_digest"], "polling": {"min_seconds": 4, "max_seconds": 30,
            "error_max_seconds": 900, "jitter_percent": 10}}
    response = client.call("POST", f"/pulse/watches/{task_id}/settings", body, key="settings")
    assert response[0] == 200, response
    assert client.call("POST", f"/pulse/watches/{task_id}/settings", body, key="settings") == response
    state = app.api.service.pulse(app.principal, task_id)
    assert state["lifecycle"] == "PAUSED" and state["configuration"]["polling"]["min_seconds"] == 4
    assert client.call("POST", f"/pulse/watches/{task_id}/settings", body, key="different")[0] == 409


def test_cadence_change_keeps_baseline_and_server_backoff(world):
    task_id = start_watch(world, action=False)
    demo, _, kernel, _, client, clock = world
    demo.control({"seats": 1})
    clock.advance(3)
    run_until_idle(kernel.ctx)
    state = pulse(world, task_id)
    before = state["changes"]
    settings = {"spec_digest": state["spec_digest"], "polling": {"min_seconds": 4, "max_seconds": 30,
                "error_max_seconds": 900, "jitter_percent": 10}}
    assert client.call("POST", f"/pulse/watches/{task_id}/settings", settings)[0] == 200
    assert pulse(world, task_id)["records"] == state["records"]
    count = kernel.web.fetch_count
    run_until_idle(kernel.ctx)
    assert kernel.web.fetch_count == count
    clock.advance(4)
    run_until_idle(kernel.ctx)
    assert pulse(world, task_id)["changes"] == before
    demo.control({"mode": "429"})
    clock.advance(10)
    run_until_idle(kernel.ctx)
    blocked = pulse(world, task_id)
    due = blocked["next_check_at"]
    settings["spec_digest"] = blocked["spec_digest"]
    settings["polling"]["min_seconds"] = 3
    assert client.call("POST", f"/pulse/watches/{task_id}/settings", settings)[0] == 200
    assert pulse(world, task_id)["next_check_at"] >= due
    assert client.call("POST", f"/tasks/{task_id}/check", {})[1]["reason"] == "server_backoff"
    client.call("POST", f"/tasks/{task_id}/pause", {})
    client.call("POST", f"/tasks/{task_id}/resume", {})
    assert client.call("POST", f"/tasks/{task_id}/check", {})[1]["reason"] == "server_backoff"


def test_new_watch_uses_app_polling_defaults(world):
    demo, _, _, app, client, _ = world
    assert client.call("POST", "/pulse/settings", {"polling_min_seconds": 25, "polling_max_seconds": 400})[0] == 200
    request = {"url": demo.url, "prompt": "等软件工程有空位", "label": "软件工程", "demo_course": True,
               "rule": "gt", "threshold": 0, "value_type": "number"}
    status, draft = client.call("POST", "/pulse/draft", request, key="defaults")
    assert status == 201, draft
    polling = app.api.service.pulse(app.principal, draft["task_id"])["configuration"]["polling"]
    assert (polling["min_seconds"], polling["max_seconds"]) == (25, 400)
