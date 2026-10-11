"""Connection failure/cancellation use an actual Pi process and a real local HTTP server.

The local server only returns HTTP failures: it does not pretend to generate model output.
Live-provider acceptance is a separate, explicitly invoked verification script.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_native_pi import native as real_native

from wakecore.adapters.agent_runtime.native import NativePiError
from wakecore.app.pulse.gateway_service import GatewayService
from wakecore.app.pulse.gateways import GatewayError, GatewayStore

native = real_native


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.025)
    raise AssertionError("state did not settle")


@pytest.fixture
def failure_endpoint():
    entered, release = threading.Event(), threading.Event()
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            seen.append(self.headers.get("Authorization"))
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            entered.set()
            release.wait(8)
            data = json.dumps({"error": {"message": "Invalid API key", "type": "authentication_error"}}).encode()
            try:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1", entered, release, seen
    release.set()
    server.shutdown()
    server.server_close()


def configured(native, endpoint):
    host, conversation = native
    service = GatewayService(host.gateways, conversation, lambda: None)
    result = service.action({"action": "save", "name": "Failure endpoint", "base_url": endpoint,
                             "model": "contract-model", "api": "openai-responses", "api_key": "private-transport-fixture"})
    return host, service, result["items"][0]


def test_connection_check_is_cancellable_durable_and_does_not_lock_conversation(native, failure_endpoint):
    address, entered, release, seen = failure_endpoint
    host, service, item = configured(native, address)
    # A native extension command is a real conversation operation without model inference.
    # Load from the test's private agent directory, as the other native SDK tests do.
    from pathlib import Path
    extensions = Path(host.agent_dir) / "extensions"
    extensions.mkdir(parents=True, exist_ok=True)
    (extensions / "check.js").write_text("export default pi => pi.registerCommand('local-check', {handler:async()=>{pi.sendMessage({customType:'check',content:'NATIVE_CONVERSATION_AVAILABLE',display:true})}})")
    host.control("start_agent")
    before = host.rpc("get_state")["sessionId"]
    start = time.monotonic()
    first = service.action({"action": "test", "id": item["id"]}, "same-connection-check")
    assert time.monotonic() - start < 2
    assert first["test"]["state"] == "running"
    assert entered.wait(5), service.public()
    duplicate = service.action({"action": "test", "id": item["id"]}, "same-connection-check")
    assert duplicate["test"]["id"] == first["test"]["id"]
    start = time.monotonic()
    service.assert_prompt_ready()
    result = service.native.prompt({"message": "/local-check"}, "native-dialogue-during-test")
    assert result["disposition"] == "handled"
    assert time.monotonic() - start < 2
    assert host.rpc("get_state")["sessionId"] == before
    assert len(seen) == 1
    response = service.action({"action": "cancel_test", "test_id": first["test"]["id"]})
    assert response["items"][0]["connection_test"]["state"] == "cancelled"
    release.set()
    wait_for(lambda: not host.pending_control)
    assert service.public()["items"][0]["connection_test"]["state"] == "cancelled"
    restored = GatewayStore(host.state_dir, vault=host.gateways.vault)
    assert restored.public()["items"][0]["connection_test"]["state"] == "cancelled"
    assert "private-transport-fixture" not in host.gateways.path.read_text()
    assert "private-transport-fixture" not in json.dumps(service.public())


def test_auth_failure_then_rotation_invalidates_test_and_restart_keeps_receipt(native, failure_endpoint):
    address, entered, release, seen = failure_endpoint
    release.set()
    host, service, item = configured(native, address)
    service.action({"action": "test", "id": item["id"]}, "auth-failure")
    wait_for(lambda: service.public()["items"][0].get("connection_test", {}).get("state") == "failed")
    result = service.public()["items"][0]
    assert result["last_test"]["category"] == "authentication"
    assert result["connection_test"]["current"]
    public = service.action({"action": "save", "id": item["id"], "api_key": "rotated-transport-fixture", "expected_revision": service.public()["revision"]})
    assert public["items"][0].get("last_test") is None
    assert not public["items"][0]["connection_test"]["current"]
    service.action({"action": "test", "id": item["id"]}, "auth-after-rotation")
    wait_for(lambda: service.public()["items"][0]["connection_test"]["state"] == "failed")
    assert seen == ["Bearer private-transport-fixture", "Bearer rotated-transport-fixture"]
    host.close()
    host.control("start_agent")
    assert service.runtime()["state"] == "applied"
    assert service.public()["items"][0]["connection_test"]["id"] == "auth-after-rotation"


def test_saved_configuration_can_reconcile_after_apply_failure(native, monkeypatch):
    host, service, item = configured(native, "http://127.0.0.1:1/v1")
    host.control("start_agent")
    real_update = host.update_gateways
    monkeypatch.setattr(host, "update_gateways", lambda **kw: (_ for _ in ()).throw(NativePiError("IPC failure")))
    saved = service.action({"action": "save", "id": item["id"], "model": "changed-model"})
    assert saved["items"][0]["model"] == "changed-model"
    assert saved["runtime"]["state"] == "pending"
    assert "已安全保存" in saved["message"]
    assert host.rpc("get_state")["model"]["id"] == "contract-model"
    monkeypatch.setattr(host, "update_gateways", real_update)
    applied = service.action({"action": "apply"})
    assert applied["runtime"]["state"] == "applied"
    assert host.rpc("get_state")["model"]["id"] == "changed-model"
    # Removing credentials or an externally missing vault entry blocks GUI prompts.
    host.gateways.vault.delete(item["provider_id"])
    with pytest.raises(GatewayError, match="凭据"):
        service.assert_prompt_ready()


def test_test_receipts_fence_late_results_and_restart(native):
    host, service, item = configured(native, "http://127.0.0.1:1/v1")
    store = host.gateways
    first, created = store.begin_test(item["id"], "late-result")
    assert created
    store.save({"id": item["id"], "model": "new-model"})
    finished = store.finish_test(first["id"], {"ok": True, "category": "connected"})
    assert finished["state"] == "superseded"
    assert "last_test" not in store.profile()
    store.begin_test(item["id"], "interrupted-check")
    restarted = GatewayStore(host.state_dir, vault=store.vault)
    assert restarted.public()["items"][0]["connection_test"]["state"] == "interrupted"
    # A repeated request key after restart returns the original receipt, without inference.
    receipt, created = restarted.begin_test(item["id"], "interrupted-check")
    assert not created and receipt["state"] == "interrupted"


def test_cancel_before_native_dispatch_prevents_model_request(native, failure_endpoint):
    address, _, _, seen = failure_endpoint
    host, service, item = configured(native, address)
    host.control("cancel_gateway_test", {"requestId": "cancel-before-start"})
    result = host.control("test_gateway", {"id": item["id"], "requestId": "cancel-before-start"})
    assert result["category"] == "cancelled"
    assert seen == []


def test_startup_failure_retains_config_and_exposes_recovery(native, monkeypatch):
    host, conversation = native
    service = GatewayService(host.gateways, conversation, lambda: None)
    real_wait = host.wait_ready
    monkeypatch.setattr(host, "wait_ready", lambda: (_ for _ in ()).throw(NativePiError("startup failed")))
    saved = service.action({"action": "save", "name": "Retained connection", "base_url": "http://127.0.0.1:1/v1",
                            "model": "retained-model", "api_key": "retained-fixture-key"})
    assert saved["runtime"]["state"] == "unavailable"
    assert "已安全保存" in saved["message"]
    assert host.gateways.profile()["name"] == "Retained connection"
    monkeypatch.setattr(host, "wait_ready", real_wait)
    assert service.action({"action": "apply"})["runtime"]["state"] == "applied"
