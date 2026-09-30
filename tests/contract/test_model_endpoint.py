"""OpenAI-compatible model endpoints (relays, proxies, gateways): URL checks, the egress the
runtime declares, the act-time egress check, and the connectivity self-check.

No browser and no network: the model is the stdlib FakeOpenAI on 127.0.0.1.
"""
import json
import os
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest

pytest.importorskip("wakecore_ui_runtime")

from wakecore.adapters.tools.browser_cua import MODEL_EGRESS  # noqa: E402
from wakecore.adapters.ui_runtime.client import RuntimeRejected, UiRuntimeClient  # noqa: E402
from wakecore.protocol.jsonschema_lite import registry  # noqa: E402
from wakecore_ui_runtime import server as srv  # noqa: E402
from wakecore_ui_runtime.cua.openai import (  # noqa: E402
    FUNCTION_NAME,
    KEEP_IMAGES,
    ModelError,
    ResponsesClient,
    _replayable,
    _trim_images,
    check_base_url,
    computer_calls,
    egress_for,
)
from wakecore_ui_runtime.cua.selfcheck import check_model, render, solid_png  # noqa: E402
from wakecore_ui_runtime.testing.fake_openai import FakeOpenAI, colour_of  # noqa: E402

KEY = "sk-test-relay-0123456789"
RELAY = "https://relay.example.com/v1"
ACT = {"key": "eff_1", "attempt": "att_1", "session_ref": "jw_alice", "goal": "enroll in PHARM",
       "start_url": "https://jw.example.edu/courses", "origins": ["https://jw.example.edu"], "login": None,
       "timeout_s": 60, "max_steps": 5}


# ------------------------------------------------------------------ base URL and egress

@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "https://relay.example.com/openai/v1/",
                                 "http://127.0.0.1:8080/v1", "http://localhost:3000/v1", "http://[::1]:9/v1"])
def test_acceptable_base_urls(url):
    assert check_base_url(url) == url.rstrip("/")


@pytest.mark.parametrize("url,why", [
    ("http://relay.example.com/v1", "https"),
    ("https://user:pw@relay.example.com/v1", "credentials"),
    ("https://relay.example.com/v1?key=abc", "query"),
    ("https://relay.example.com/v1#x", "query"),
    ("ftp://relay.example.com/v1", "https://host/v1"),
    ("relay.example.com/v1", "https://host/v1"),
    ("https://relay.example.com:99999/v1", "valid URL"),
])
def test_unacceptable_base_urls(url, why):
    with pytest.raises(ValueError, match=why):
        check_base_url(url)


def test_the_egress_names_who_receives_the_screenshots():
    assert egress_for("https://api.openai.com/v1") == MODEL_EGRESS
    assert egress_for("https://API.OpenAI.com/v1") == MODEL_EGRESS
    assert egress_for("https://Relay.Example.com:8443/openai/v1") == "model:openai-compatible:relay.example.com"
    assert egress_for("http://127.0.0.1:5/v1") == "model:openai-compatible:127.0.0.1"


def test_the_client_refuses_a_bad_base_url_from_the_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://relay.example.com/v1")
    with pytest.raises(ValueError):
        ResponsesClient(api_key="x")


def test_the_server_refuses_a_bad_base_url_at_start(tmp_path):
    p = subprocess.run([sys.executable, "-m", "wakecore_ui_runtime.server", "--state", str(tmp_path / "s"),
                        "--openai-base-url", "http://relay.example.com/v1"], capture_output=True, text=True, timeout=60)
    assert p.returncode == 2 and "https" in p.stderr and "LISTENING" not in p.stdout


# ------------------------------------------------------------------ the runtime declares and enforces it

@pytest.fixture
def relay_live(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    rt = srv.Runtime(str(tmp_path / "state"), trace=False, client=ResponsesClient(base_url=RELAY, api_key="k"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.make_handler(rt))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield rt, UiRuntimeClient(f"http://127.0.0.1:{httpd.server_address[1]}")
    httpd.shutdown()
    httpd.server_close()


def test_health_and_capabilities_declare_the_endpoint_and_egress(relay_live):
    _, client = relay_live
    h = client.health()
    registry().validate(h, "health.v1.schema.json")
    assert (h["model_endpoint"], h["model_egress"]) == ("relay.example.com", "model:openai-compatible:relay.example.com")
    caps = client.capabilities()
    registry().validate(caps, "capabilities.v1.schema.json")
    assert caps["act"]["model_egress"] == h["model_egress"]


def test_an_act_approved_for_another_egress_is_refused_before_anything_is_sent(relay_live):
    rt, client = relay_live
    with pytest.raises(RuntimeRejected) as e:
        client.act(**ACT, model_egress=MODEL_EGRESS)
    assert (e.value.status, e.value.code) == (409, "model_egress_mismatch")
    assert e.value.details == {"runtime_model_egress": "model:openai-compatible:relay.example.com"}
    assert rt.journal.get("eff_1") is None and rt.sessions.active() == []


def test_a_journalled_act_is_still_answered_whatever_the_egress(relay_live):
    """A repeat never calls the model, and refusing it would hide writes that already happened."""
    rt, client = relay_live
    rt.journal.begin("eff_1", "att_1", "jw_alice", request_digest=srv.request_digest(ACT))
    rt.journal.append("eff_1", "mutating", {"method": "POST", "url": "https://jw.example.edu/api/enroll"})
    rt.journal.update("eff_1", status="finished", finished_at=0, result={"status": "completed"})
    r = client.act(**ACT, model_egress=MODEL_EGRESS)
    assert r["deduplicated"] is True and r["mutating_requests"] == 1


def test_the_egress_field_is_validated(relay_live):
    _, client = relay_live
    with pytest.raises(RuntimeRejected) as e:
        client.act(**ACT, model_egress="openai")
    assert e.value.status == 400


# ------------------------------------------------------------------ connectivity self-check

@pytest.fixture
def fake():
    f = FakeOpenAI(api_key=KEY).start()
    yield f
    f.stop()


def test_the_check_screenshot_is_a_red_png():
    png = solid_png()
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and colour_of(png) == "red"
    assert colour_of(solid_png((255, 255, 255))) == "white"


def test_check_passes_against_a_compatible_endpoint(fake):
    r = check_model(ResponsesClient(base_url=fake.base_url, api_key=KEY, model="m"))
    assert r["ok"] and r["steps"]["start"]["ok"] and r["steps"]["chain"]["ok"]
    assert r["steps"]["start"]["output_types"] == ["computer_call"] and r["steps"]["chain"]["image"] == "seen"
    assert len(fake.requests) == 2
    assert r["model_egress"] == "model:openai-compatible:127.0.0.1" and not fake.violations
    assert "MODEL CHECK OK" in render(r)


def test_check_preview_variant(fake):
    fake.variant = "preview"
    assert check_model(ResponsesClient(base_url=fake.base_url, api_key=KEY, model="m", variant="preview"))["ok"]


def test_check_reports_a_stateless_relay_when_history_is_pinned_to_the_server(fake):
    fake.stateless = True
    r = check_model(ResponsesClient(base_url=fake.base_url, api_key=KEY, model="m", history="server"))
    assert not r["ok"] and (r["failed_step"], r["error_code"]) == ("chain", "http_400")
    assert "previous_response_id" in r["hint"] and r["steps"]["start"]["ok"]


def test_a_stateless_relay_falls_back_to_client_side_history(fake):
    fake.stateless = True
    client = ResponsesClient(base_url=fake.base_url, api_key=KEY, model="m")
    r = check_model(client)
    assert r["ok"] and r["history"] == "client" and r["steps"]["chain"]["image"] == "seen"
    assert fake.history_turns == 1 and client.history_in_use == "client"
    assert "history      client" in render(r)
    fake.requests.clear()
    assert check_model(client)["ok"]          # sticky: no second refused attempt
    assert all("previous_response_id" not in json.loads(b) for b in fake.requests)


def test_a_relay_that_refuses_chaining_with_a_bare_400_falls_back_too(fake):
    fake.stateless, fake.refusal = True, "The request could not be processed. Please check the request parameters."
    client = ResponsesClient(base_url=fake.base_url, api_key=KEY, model="m")
    r = check_model(client)
    assert r["ok"] and r["history"] == "client" and client.history_in_use == "client"
    assert fake.violations == [fake.refusal]                 # refused once, then the history is sent


def test_client_history_from_the_start_never_chains(fake):
    r = check_model(ResponsesClient(base_url=fake.base_url, api_key=KEY, model="m", history="client"))
    assert r["ok"] and fake.violations == []
    assert all("previous_response_id" not in json.loads(b) for b in fake.requests)


def test_the_function_variant_passes_where_the_computer_tool_is_refused(fake):
    fake.variant, fake.stateless = "function", True     # the relays seen for real
    r = check_model(ResponsesClient(base_url=fake.base_url, api_key=KEY, model="m", variant="ga"))
    assert not r["ok"] and "Unsupported tool type: computer" in r["steps"]["start"]["detail"]
    fake.violations.clear()
    r = check_model(ResponsesClient(base_url=fake.base_url, api_key=KEY, model="m", variant="function"))
    assert r["ok"] and r["model_ref"] == "openai:m:function" and r["history"] == "client"
    assert r["steps"]["start"]["output_types"] == ["function_call"]
    assert fake.violations == ["previous_response_id is not available for this user"]


def test_a_function_call_is_read_as_a_computer_call():
    def resp(args):
        return {"output": [{"type": "reasoning"}, {"type": "function_call", "call_id": "c1",
                                                   "name": FUNCTION_NAME, "arguments": args},
                           {"type": "function_call", "call_id": "c2", "name": "other", "arguments": "{}"}]}
    calls = computer_calls(resp(json.dumps({"actions": [{"type": "click", "x": 1, "y": 2}]})))
    assert [(c["call_id"], c["actions"]) for c in calls] == [("c1", [{"type": "click", "x": 1, "y": 2}])]
    for bad in ("not json", "{}", '{"actions": []}', '{"actions": "click"}', None):
        assert computer_calls(resp(bad))[0]["actions"] == [None]     # the loop refuses: bad_action


def test_client_history_keeps_only_the_last_screenshots():
    shot = {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
    items = [{"role": "user", "content": [{"type": "input_text", "text": "task"}, shot]}]
    for n in range(5):
        items += [{"type": "function_call", "call_id": f"c{n}", "name": FUNCTION_NAME, "arguments": "{}"},
                  {"type": "function_call_output", "call_id": f"c{n}", "output": "ok"},
                  {"role": "user", "content": [{"type": "input_text", "text": "after"}, shot]}]
    trimmed = _trim_images(items)
    images = [p for i in trimmed if isinstance(i.get("content"), list) for p in i["content"]
              if p["type"] == "input_image"]
    assert len(images) == KEEP_IMAGES and trimmed[0]["content"][0]["text"] == "task"
    assert trimmed[0]["content"][1] == {"type": "input_text", "text": "[earlier screenshot omitted]"}
    assert trimmed[-1]["content"][1] == shot and len(trimmed) == len(items)
    assert items[0]["content"][1] == shot                                  # the input is not modified


def test_replayed_items_carry_no_ids_and_no_reasoning():
    out = [{"type": "reasoning", "id": "rs_1", "summary": []},
           {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": FUNCTION_NAME, "arguments": "{}"},
           {"type": "message", "id": "msg_1", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}]
    assert [_replayable(o) for o in out] == [
        None, {"type": "function_call", "call_id": "c1", "name": FUNCTION_NAME, "arguments": "{}"},
        {"role": "assistant", "content": "hi"}]


def test_check_catches_a_relay_that_silently_drops_the_computer_tool(fake):
    # seen for real: the relay answered 200, the model said "no browser tool available"
    fake.drop_tools = True
    r = check_model(ResponsesClient(base_url=fake.base_url, api_key=KEY, model="m"))
    assert not r["ok"] and (r["failed_step"], r["error_code"]) == ("start", "computer_tool_unused")
    assert "no browser tool" in r["hint"] and "`computer` tool" in r["hint"]
    assert "MODEL CHECK FAILED: computer_tool_unused" in render(r) and len(fake.requests) == 1


def test_check_catches_a_relay_that_drops_images(fake):
    fake.blind = True
    r = check_model(ResponsesClient(base_url=fake.base_url, api_key=KEY, model="m"))
    assert not r["ok"] and (r["failed_step"], r["error_code"]) == ("chain", "image_not_seen")
    assert "image input" in r["hint"] and r["steps"]["start"]["ok"]


def test_check_reports_a_relay_without_the_computer_tool(fake):
    fake.variant = "preview"          # the endpoint only knows computer_use_preview
    r = check_model(ResponsesClient(base_url=fake.base_url, api_key=KEY, model="m", variant="ga"))
    assert (r["failed_step"], r["error_code"]) == ("start", "http_400") and "`computer` tool" in r["hint"]


def test_check_reports_a_wrong_key_and_a_wrong_path_without_printing_the_key(fake):
    r = check_model(ResponsesClient(base_url=fake.base_url, api_key="sk-wrong-key-123456", model="m"))
    assert r["error_code"] == "http_401" and "sk-wrong" not in render(r)
    r = check_model(ResponsesClient(base_url=fake.base_url[:-3], api_key=KEY, model="m"))
    assert r["error_code"] == "http_404" and "/v1" in r["hint"]


def test_check_names_an_endpoint_that_only_offers_chat_completions(monkeypatch):
    client = ResponsesClient(base_url=RELAY, api_key=KEY, model="m")

    def start(goal, png):
        raise ModelError("http_503", '{"error":{"code":"no_upstream_account","message":"/v1/responses '
                                     'unavailable, use POST /v1/chat/completions instead"}}')
    monkeypatch.setattr(client, "start", start)
    r = check_model(client)
    assert (r["failed_step"], r["error_code"]) == ("start", "http_503")
    assert "only Chat Completions" in r["hint"] and "try again later" not in r["hint"]


def test_check_names_a_missing_model_instead_of_blaming_the_path(monkeypatch):
    client = ResponsesClient(base_url=RELAY, api_key=KEY, model="computer-use-preview")

    def start(goal, png):
        raise ModelError("http_404", '{"error":{"message":"Model \\"computer-use-preview\\" is not supported by '
                                     'any configured account in this group","type":"model_not_found"}}')
    monkeypatch.setattr(client, "start", start)
    r = check_model(client)
    assert "`computer-use-preview`" in r["hint"] and "/v1" not in r["hint"]


def test_check_without_a_key():
    r = check_model(ResponsesClient(base_url=RELAY, api_key="", model="m"))
    assert r["error_code"] == "model_not_configured"


def test_check_model_command(fake, tmp_path):
    key = tmp_path / "openai.key"
    key.write_text(KEY + "\n")
    key.chmod(0o600)
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENAI_")}
    args = [sys.executable, "-m", "wakecore_ui_runtime.server", "--check-model", "--openai-key-file", str(key),
            "--openai-base-url", fake.base_url, "--model", "m"]
    p = subprocess.run(args, capture_output=True, text=True, timeout=60, env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "MODEL CHECK OK" in p.stdout and "model:openai-compatible:127.0.0.1" in p.stdout
    assert KEY not in p.stdout + p.stderr
    fake.stateless = True
    p = subprocess.run(args + ["--history", "server"], capture_output=True, text=True, timeout=60, env=env)
    assert p.returncode == 1 and "MODEL CHECK FAILED: http_400" in p.stdout
    fake.variant = "function"
    p = subprocess.run(args + ["--variant", "function"], capture_output=True, text=True, timeout=60, env=env)
    assert p.returncode == 0 and "openai:m:function" in p.stdout and "history      client" in p.stdout, p.stdout
