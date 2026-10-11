"""Durability and authorization boundaries for the optional platform modules."""
from datetime import datetime, timezone

import pytest

from wakecore.adapters.clock import FakeClock
from wakecore.adapters.sources.environment import EnvironmentSource, validate_source
from wakecore.app.bootstrap import build
from wakecore.app.pulse.agent import request
from wakecore.app.pulse.server import LocalPulseApp
from wakecore.app.worker.loop import run_until_idle
from wakecore_ui_runtime.desktop.node import ComputerError, ComputerNode

APP = "com.apple.calculator"


class Bridge:
    def __init__(self):
        self.value, self.writes, self.lost = "1", [], False

    def call(self, method, args, **kwargs):
        if method == "get_app_state":
            return {"text": f'<app_state>\nApp=/Calculator.app (bundleID {APP}, pid 42)\nWindow: "Calculator", App: Calculator.\n0 window Calculator\n\t1 text {self.value}\n\t2 button Equal\n</app_state>', "images": [], "is_error": False}
        self.writes.append((method, args))
        self.value = "2"
        if self.lost:
            raise TimeoutError()
        return {"text": "ok", "images": [], "is_error": False}

    def close(self):
        pass


@pytest.fixture
def node(tmp_path):
    bridge = Bridge()
    return ComputerNode(tmp_path / "node", bridge), bridge


def authorize(node):
    node.configure({"observe_apps": [APP], "operate_apps": [APP]})


def action(node, key="one-action"):
    return {"key": key, "app": APP, "method": "click", "arguments": {"element_index": "2"}, "observation_id": node.observe({"app": APP})["id"]}


def test_computer_scopes_freshness_and_takeover(node):
    computer, bridge = node
    with pytest.raises(ComputerError, match="授权"):
        computer.observe({"app": APP})
    computer.configure({"observe_apps": [APP]})
    body = action(computer)
    with pytest.raises(ComputerError):
        computer.act(body)
    authorize(computer)
    with pytest.raises(ComputerError, match="过期"):
        computer.act(body)
    body = action(computer)
    bridge.value = "changed"
    with pytest.raises(ComputerError, match="变化"):
        computer.act(body)
    body = action(computer)
    computer.configure({"paused": True})
    with pytest.raises(ComputerError, match="接管"):
        computer.act(body)
    assert not bridge.writes


def test_computer_send_once_survives_crash_and_conflicting_key(node, tmp_path):
    computer, bridge = node
    authorize(computer)
    body = action(computer)
    assert computer.act(body)["status"] == "observed"
    assert computer.act(body)["deduplicated"]
    with pytest.raises(ComputerError, match="不同操作"):
        computer.act({**body, "arguments": {"element_index": "1"}})
    restarted = ComputerNode(tmp_path / "node", bridge)
    assert restarted.act(body)["deduplicated"]
    assert len(bridge.writes) == 1


def test_uncertain_effect_is_never_retried(node):
    computer, bridge = node
    authorize(computer)
    body = action(computer)
    bridge.lost = True
    assert computer.act(body)["status"] == "unknown"
    assert computer.act(body)["status"] == "unknown"
    assert len(bridge.writes) == 1


def test_source_file_folder_log_and_secret_refusal(tmp_path):
    source = EnvironmentSource()
    file = tmp_path / "status.txt"
    cfg = {"kind": "file", "target": str(file)}
    assert not source.read(cfg)["exists"]
    file.write_text("等待通过")
    assert source.read(cfg)["value"] == "等待通过"
    assert source.read({"kind": "folder", "target": str(tmp_path)})["count"] == 1
    file.write_text("x" * 70000 + "DONE")
    with pytest.raises(ValueError, match="64 KB"):
        source.read(cfg)
    assert source.read({**cfg, "kind": "log"})["value"].endswith("DONE")
    with pytest.raises(ValueError, match="凭据"):
        validate_source({"kind": "file", "target": str(tmp_path / ".env")})
    with pytest.raises(ValueError, match="凭据"):
        validate_source({"kind": "http", "target": "https://example.com/?token=secret"})


def test_watch_lifecycle_deduplication_and_restart_without_pi(tmp_path):
    clock = FakeClock(datetime(2026, 10, 11, tzinfo=timezone.utc))
    target = tmp_path / "file.txt"
    target.write_text("pending")
    def open_app():
        kernel = build(db_url=f"sqlite:///{tmp_path}/kernel.db", clock=clock, with_model=False)
        app = LocalPulseApp(kernel, state_dir=tmp_path, platform_enabled=True)
        return kernel, app
    kernel, app = open_app()
    platform = app.platform
    observed = platform.command({"operation": "inspect_source", "source": {"kind": "file", "target": str(target)}})
    draft = platform.command({"operation": "prepare_watch", "inspection_id": observed["inspection_id"], "label": "发布状态", "condition": "状态变为 ready 时通知", "field": "value", "op": "eq", "value": "ready", "interval_seconds": 2})["draft"]
    assert app.api.native is None
    assert platform.snapshot()["watches"][0]["lifecycle"] == "DRAFT"
    app.api._start(request({"spec_digest": draft["spec_digest"]}, "start"), app.principal, {"id": draft["task_id"]})
    run_until_idle(kernel.ctx)
    target.write_text("ready")
    clock.advance(seconds=3)
    run_until_idle(kernel.ctx)
    platform.sync_notifications()
    assert len(platform.store.list("notification")) == 1
    app.api.service.pause_task(app.principal, draft["task_id"])
    platform.close()
    kernel.ctx.store.close()
    kernel, app = open_app()
    try:
        assert app.platform.snapshot()["watches"][0]["lifecycle"] == "PAUSED"
        app.platform.sync_notifications()
        assert len(app.platform.store.list("notification")) == 1
        app.api.service.resume_task(app.principal, draft["task_id"])
        clock.advance(seconds=3)
        run_until_idle(kernel.ctx)
        app.platform.sync_notifications()
        assert len(app.platform.store.list("notification")) == 1
        app.platform.command({"operation": "watch_archive", "task_id": draft["task_id"]})
        assert app.platform.snapshot()["watches"] == []
        assert app.api.service.get_task(app.principal, draft["task_id"])["lifecycle"] == "CANCELLED"
    finally:
        app.platform.close()
        kernel.ctx.store.close()


def test_untrusted_tools_cannot_approve_or_grant_device_access(tmp_path):
    from wakecore.app.pulse.native_watch import NativeWatchTools
    kernel = build(db_url=f"sqlite:///{tmp_path}/kernel.db", with_model=False)
    app = LocalPulseApp(kernel, state_dir=tmp_path, platform_enabled=True)
    handler = NativeWatchTools(app.api, app.principal)
    try:
        for op in ("permissions", "approve_action", "device_save"):
            with pytest.raises(Exception, match="不能"):
                handler("morning_computer", {"operation": op})
        app.platform.modules["computer"] = False
        with pytest.raises(ValueError, match="关闭"):
            handler("morning_computer", {"operation": "observe", "app": APP})
        assert app.platform.store.events() == []
    finally:
        app.platform.close()
        kernel.ctx.store.close()


def test_notification_and_wake_intent_are_deduplicated_together(tmp_path):
    from wakecore.app.pulse.platform_store import PlatformStore
    store = PlatformStore(tmp_path)
    note = {"state": "unread", "task_id": "watch-one", "title": "Changed", "body": "Value changed"}
    wake = {"state": "pending", "goal": "Observe only", "context": "untrusted data", "session_path": "/session.jsonl"}
    assert store.deliver_notification("notice-one", note, wake)
    assert not store.deliver_notification("notice-one", note, wake)
    assert len(store.list("notification")) == len(store.list("wakeup")) == len(store.events()) == 1
    store.save("wakeup", "watch:notice-one", {**store.get("wakeup", "watch:notice-one"), "state": "dispatching"})
    restarted = PlatformStore(tmp_path)
    assert restarted.get("wakeup", "watch:notice-one")["state"] == "unknown"


def test_device_metadata_failure_rolls_back_secure_token(tmp_path, monkeypatch):
    from test_gateway_credentials import VaultFixture

    from wakecore.app.pulse.devices import DeviceError, DeviceManager
    from wakecore.app.pulse.platform_store import PlatformStore
    store, vault = PlatformStore(tmp_path), VaultFixture()
    devices = DeviceManager(store, tmp_path, vault=vault)
    item = devices.save({"name": "Device", "endpoint": "https://device.example", "token": "fixture-device-token-original"})
    assert "fixture-device-token" not in str(devices.public())
    with pytest.raises(DeviceError, match="认证令牌"):
        devices.save({"id": item["id"], "endpoint": "https://different.example"})
    def fail(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(OSError):
        devices.save({"id": item["id"], "endpoint": item["endpoint"], "token": "fixture-device-token-replacement"})
    assert vault.read(item["id"])["token"] == "fixture-device-token-original"


def test_http_failures_preserve_content_and_status_mode_keeps_server_backoff(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from wakecore.kernel.ports.observation import ObservationRequest
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(503)
            self.send_header("Retry-After", "90")
            self.end_headers()
            self.wfile.write(b"upstream error")
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    source = EnvironmentSource()
    cfg = {"kind": "http", "target": f"http://127.0.0.1:{server.server_port}"}
    def fetch(config):
        return source.fetch(ObservationRequest("t", "s", {"environment": config}, None, None, datetime.now(timezone.utc)))
    try:
        failed = fetch(cfg)
        assert failed.outcome.value == "unavailable" and failed.records == {} and failed.retry_after_seconds == 90
        status = fetch({**cfg, "response_mode": "status"})
        assert status.outcome.value == "success" and status.records["target"]["value"] == 503 and status.retry_after_seconds == 90
    finally:
        server.shutdown()
        server.server_close()


def test_real_device_startup_accepts_high_numbered_descriptor(tmp_path, monkeypatch):
    """Exercise the real readiness pipe above select()'s macOS FD_SETSIZE."""
    import fcntl
    import os
    import resource

    from test_gateway_credentials import VaultFixture

    from wakecore.app.pulse import devices
    from wakecore.app.pulse.platform_store import PlatformStore

    if resource.getrlimit(resource.RLIMIT_NOFILE)[0] <= 1200:
        pytest.skip('requires room for a descriptor above FD_SETSIZE')
    original = devices.subprocess.Popen
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        high = fcntl.fcntl(process.stdout.fileno(), fcntl.F_DUPFD, 1100)
        process.stdout.close()
        process.stdout = os.fdopen(high, 'rb', buffering=0)
        return process
    monkeypatch.setattr(devices.subprocess, 'Popen', launch)
    manager = devices.DeviceManager(PlatformStore(tmp_path), tmp_path, vault=VaultFixture())
    try:
        manager._start()
        assert manager.process.stdout.fileno() >= 1100
        assert manager.local_endpoint.startswith('http://127.0.0.1:')
        assert manager.process.poll() is None
    finally:
        manager.close()
    assert manager.local_endpoint is None


@pytest.mark.parametrize('summary', [
    'There has been no change in the accessibility tree for Window: "Fixture".',
    'The following is a diff from the previous accessibility tree with ~ and + representing changed and added elements.\n+ 5 button Increase',
])
def test_partial_bridge_observation_cannot_authorize_or_execute(node, summary):
    computer, bridge = node
    authorize(computer)
    body = action(computer)
    original = bridge.call
    def partial(method, args, **kwargs):
        if method == 'get_app_state':
            return {'text': f'<app_state>\nApp=/Calculator.app (bundleID {APP}, pid 42)\n{summary}\n</app_state>', 'images': [], 'is_error': False}
        return original(method, args, **kwargs)
    bridge.call = partial
    with pytest.raises(ComputerError) as caught:
        computer.observe({'app': APP})
    assert caught.value.code == 'observation_incomplete'
    with pytest.raises(ComputerError) as caught:
        computer.act(body)
    assert caught.value.code == 'observation_incomplete'
    assert not bridge.writes
