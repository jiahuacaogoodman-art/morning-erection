"""Native Pi contract checks. They use the installed Pi package, never a model stub."""
import base64
import io
import re
import threading
import time
from pathlib import Path

import pytest
from test_gateway_credentials import VaultFixture
from test_pulse_settings import call

from wakecore.adapters.agent_runtime.native import NativePiHost
from wakecore.app.pulse.gateways import GatewayStore
from wakecore.app.pulse.native_agent import NativeConversation, project_messages
from wakecore.app.pulse.server import LocalPulseApp

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "apps/agent-runtime/native-bootstrap.mjs"


@pytest.fixture
def native(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    vault = VaultFixture()
    gateways = GatewayStore(tmp_path / "state", vault=vault)
    host = NativePiHost(runtime_path=RUNTIME, workspace=workspace, state_dir=tmp_path / "state", gateways=gateways, agent_dir=tmp_path / "agent")
    if not host.available:
        pytest.skip("install apps/agent-runtime to check the native Pi contract")
    yield host, NativeConversation(host)
    host.close()


def test_empty_native_collections_are_arrays_and_gateway_does_not_start_agent(native):
    host, _ = native
    assert host.control("sessions") == []
    assert isinstance(host.control("models"), list)
    assert host.agent_ready is False
    assert host.status_snapshot()["status"] == "ready"


def test_empty_native_selection_restores_without_fabricating_messages(native):
    host, conversation = native
    host.legacy_messages = [{"role": "user", "text": "Older conversation", "at": "2026-01-01T00:00:00Z"}]
    host.rpc("get_state")
    conversation.command("new_session", {})
    conversation.command("set_session_name", {"name": "Empty work session"})
    profile = host.gateways.save({'name': 'Empty session gateway', 'base_url': 'http://127.0.0.1:1/v1', 'model': 'restore-model', 'api': 'openai-responses', 'api_key': 'restore-fixture-key', 'model_options': {'reasoning': True}})
    host.update_gateways()
    profile = host.gateways.profile()
    conversation.command('set_model', {'provider': profile['provider_id'], 'modelId': profile['model']})
    conversation.command('set_thinking_level', {'level': 'high'})
    before = conversation.snapshot()["state"]
    assert not Path(before["sessionFile"]).exists(), "Pi deliberately does not persist setup-only sessions"
    assert host.rpc("get_messages")["messages"] == []
    host.close()
    after = host.rpc("get_state")
    assert after["sessionId"] == before["sessionId"]
    assert after["sessionName"] == "Empty work session"
    assert after['model']['provider'] == before['model']['provider']
    assert after['model']['id'] == 'restore-model'
    assert after['thinkingLevel'] == 'high'
    assert host.rpc("get_messages")["messages"] == []
    assert not Path(after["sessionFile"]).exists(), "GUI selection metadata must not fake a native conversation"


def test_native_session_history_new_resume_fork_and_restart(native):
    host, conversation = native
    host.legacy_messages = [{"role": "user", "text": "Offline persistence contract fixture.", "at": "2026-01-01T00:00:00Z"}]
    state = host.rpc("get_state")
    assert state["autoCompactionEnabled"]
    first = state["sessionFile"]
    result = host.rpc("bash", {"command": "printf PI_NATIVE_HISTORY"})
    assert result["output"] == "PI_NATIVE_HISTORY"
    conversation.snapshot()
    assert Path(first).exists()
    assert host.rpc("new_session")["cancelled"] is False
    assert host.rpc("get_messages")["messages"] == []
    assert host.rpc("switch_session", {"sessionPath": first})["cancelled"] is False
    assert host.rpc("get_messages")["messages"][-1]["output"] == "PI_NATIVE_HISTORY"
    exported = conversation.command("export_html", {})
    assert Path(exported["path"]).is_file()
    embedded = re.search(r'<script id="session-data" type="application/json">(.*?)</script>', Path(exported["path"]).read_text(), re.S)
    assert embedded and "PI_NATIVE_HISTORY" in base64.b64decode(embedded[1]).decode()
    assert conversation.exports[exported["download_url"].rsplit("/", 1)[-1]] == exported["path"]
    assert host.rpc("clone")["cancelled"] is False
    clone = host.rpc("get_state")["sessionFile"]
    assert clone != first
    conversation.snapshot()
    host.close()
    restored = host.rpc("get_state")
    assert restored["sessionFile"] == clone
    assert host.rpc("get_messages")["messages"][-1]["output"] == "PI_NATIVE_HISTORY"


def test_native_bash_abort_is_concurrent_with_running_command(native):
    host, _ = native
    host.rpc("get_state")
    result = {}
    done = threading.Event()

    def execute():
        result.update(host.rpc("bash", {"command": "printf PI_STARTED; sleep 15; printf PI_TOO_LATE"}))
        done.set()

    worker = threading.Thread(target=execute, daemon=True)
    worker.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any(event.get("type") == "bash_execution_update" and "PI_STARTED" in event.get("delta", "") for event in host.events):
            break
        time.sleep(.05)
    started = time.monotonic()
    host.rpc("abort_bash")
    assert done.wait(3)
    assert time.monotonic() - started < 3
    assert result["cancelled"]
    assert "PI_TOO_LATE" not in result["output"]


def test_native_resources_and_extension_ui_use_official_discovery(native):
    host, conversation = native
    skills = Path(host.agent_dir) / "skills" / "contract-check"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: contract-check\ndescription: Native skill contract check.\n---\nRead the task carefully.\n")
    extensions = Path(host.agent_dir) / "extensions"
    extensions.mkdir()
    (extensions / "dialog.js").write_text('''export default function(pi) {
      pi.registerCommand('contract-dialog', {description:'Contract dialog', handler:async(_args,ctx) => {
        const value = await ctx.ui.input('PI_NATIVE_INPUT','value');
        pi.sendMessage({customType:'contract-result',content:value || 'cancelled',display:true});
      }});
    }''')
    resources = host.control("resources")
    assert any(skill["name"] == "contract-check" for skill in resources["skills"])
    commands = host.rpc("get_commands")["commands"]
    assert {"contract-dialog", "skill:contract-check"} <= {command["name"] for command in commands}
    assert {"read", "write", "edit", "bash"} <= {tool["name"] for tool in resources["tools"]}
    accepted = []
    thread = threading.Thread(target=lambda: accepted.append(host.rpc("prompt", {"message": "/contract-dialog"})), daemon=True)
    thread.start()
    deadline = time.monotonic() + 8
    while not host.pending_ui and time.monotonic() < deadline:
        time.sleep(.05)
    interaction = host.status_snapshot()["pending_ui"][0]
    assert interaction["method"] == "input"
    host.answer_ui(interaction["id"], {"value": "PI_GUI_ANSWER"})
    thread.join(3)
    assert accepted == [{"disposition": "handled"}]
    assert any(message["text"] == "PI_GUI_ANSWER" for message in conversation.snapshot()["dialogue"])


def test_projection_preserves_rich_messages_and_distinguishes_stop():
    messages = [{"role": "system", "content": "internal", "timestamp": 1},
                {"role": "user", "content": [{"type": "text", "text": "hello"}, {"type": "image", "mimeType": "image/png", "data": "sample"}], "timestamp": 2},
                {"role": "assistant", "content": [], "stopReason": "error", "errorMessage": "This operation was aborted", "timestamp": 3}]
    display = project_messages(messages)
    assert len(display) == 2
    assert display[0]["images"][0]["data"] == "sample"
    assert display[1]["type"] == "stopped"


def test_native_package_manager_and_tree_navigation(native, tmp_path):
    host, _ = native
    host.legacy_messages = [{"role": "user", "text": "Tree navigation fixture.", "at": "2026-01-01T00:00:00Z"}]
    host.rpc("get_state")
    bundle = tmp_path / "local-pi-package"
    prompts = bundle / "prompts"
    prompts.mkdir(parents=True)
    (bundle / "package.json").write_text('{"name":"morning-native-contract-package","version":"1.0.0","pi":{"prompts":["prompts/*.md"]}}')
    (prompts / "package-check.md").write_text("---\ndescription: Native package fixture.\n---\nCheck the current task.\n")
    resources = host.control("package", {"action": "install", "source": str(bundle), "local": True})
    assert any(prompt["name"] == "package-check" for prompt in resources["prompts"])
    resources = host.control("package", {"action": "remove", "source": str(bundle), "local": True})
    assert all(prompt["name"] != "package-check" for prompt in resources["prompts"])
    host.rpc("bash", {"command": "printf BRANCH_RECORD"})
    entries = host.rpc("get_entries")["entries"]
    user = next(entry for entry in entries if entry["type"] == "message" and entry["message"]["role"] == "user")
    result = host.control("navigate_tree", {"entryId": user["id"], "summarize": False})
    assert not result["cancelled"]
    assert result["editorText"] == "Tree navigation fixture."
    assert any(entry["type"] == "message" and entry["message"].get("output") == "BRANCH_RECORD" for entry in host.rpc("get_entries")["entries"])


def test_live_agent_drops_deleted_credential_and_does_not_override_native_model_selection(native):
    host, _ = native
    store = host.gateways
    store.save({"name": "Fixture Gateway", "base_url": "http://127.0.0.1:1", "api": "openai-responses", "model": "contract-model", "api_key": "fixture-valid-storage-only"})
    first = store.profile()
    host.rpc("get_state")
    store.save({"name": "Second Fixture", "base_url": "http://127.0.0.1:2", "api": "openai-responses", "model": "second-model", "api_key": "second-fixture-storage-only"})
    second = store.public()["items"][1]
    host.update_gateways()
    host.rpc("set_model", {"provider": second["provider_id"], "modelId": second["model"]})
    store.save({"id": first["id"], "name": "Renamed default"})
    host.update_gateways()
    assert host.rpc("get_state")["model"]["provider"] == second["provider_id"]
    store.save({"id": second["id"], "model": "updated-second-model"})
    host.update_gateways()
    assert host.rpc("get_state")["model"]["id"] == "updated-second-model"
    store.activate(second["id"])
    host.update_gateways()
    store.delete(second["id"], credentials_only=True)
    host.update_gateways()
    assert not store.public()["items"][1]["has_credentials"]
    assert all(model["provider"] != second["provider_id"] for model in host.control("models"))


def test_session_download_requires_the_local_session_and_an_issued_export_id(tmp_path):
    from wakecore.app.bootstrap import build

    kernel = build(db_url=f"sqlite:///{tmp_path / 'kernel.db'}", with_model=False)
    host = NativePiHost(runtime_path=RUNTIME, workspace=tmp_path, state_dir=tmp_path,
                        gateways=GatewayStore(tmp_path, vault=VaultFixture()))
    try:
        app = LocalPulseApp(kernel, native_host=host, gateways=host.gateways, state_dir=tmp_path)
        exported = tmp_path / "private-session.html"
        exported.write_text("<!doctype html><title>PRIVATE_ARCHIVE_CONTENT</title>")
        app.api.native.exports["issued-export-id"] = str(exported)
        cookie = call(app, "GET", "/bootstrap")[2]["Set-Cookie"].split(";", 1)[0]

        def download(export_id, auth=""):
            response = {}

            def start(status, headers):
                response.update(status=int(status.split()[0]), headers=dict(headers))

            response["body"] = b"".join(app({"REQUEST_METHOD": "GET", "PATH_INFO": "/api/pulse/pi/export/" + export_id,
                "HTTP_HOST": "127.0.0.1:8788", "HTTP_COOKIE": auth, "wsgi.input": io.BytesIO()}, start))
            return response

        denied = download("issued-export-id")
        assert denied["status"] == 401 and b"PRIVATE_ARCHIVE_CONTENT" not in denied["body"]
        assert download("unknown-export-id", cookie)["status"] == 404
        allowed = download("issued-export-id", cookie)
        assert allowed["status"] == 200 and b"PRIVATE_ARCHIVE_CONTENT" in allowed["body"]
        assert allowed["headers"]["Content-Disposition"].startswith("attachment;")
        assert allowed["headers"]["Cache-Control"] == "no-store"
    finally:
        kernel.ctx.store.close()
