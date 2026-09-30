"""Desktop Runtime protocol v1 against a fake Computer Use MCP bridge and a fake model.

The runtime runs in-process on a real HTTP socket and spawns the fake bridge as a real stdio
subprocess (testing/fake_desktop.py), so the MCP client, the guards, the journal and the
schemas are all exercised end to end. No real app, no network.
"""
import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

pytest.importorskip("wakecore_ui_runtime")

from wakecore.protocol.jsonschema_lite import registry  # noqa: E402
from wakecore_ui_runtime.desktop import server as dsrv  # noqa: E402
from wakecore_ui_runtime.desktop.mcp import McpBridge  # noqa: E402
from wakecore_ui_runtime.desktop.model import DesktopModelClient  # noqa: E402
from wakecore_ui_runtime.testing.fake_desktop import (  # noqa: E402
    CALC,
    SIGNIN,
    FakeDesktopModel,
    mcp_command,
    read_state_file,
    write_state,
)

TOKEN = "t0ken"
APPS = [CALC, SIGNIN]
CALC_X = {"ready": {"id": "StandardInputView"},
          "records": [{"id": "display", "columns": [
              {"field": "value", "node": {"under": {"id": "StandardInputView"}, "head_regex": "^text "},
               "pattern": r"(\S+)$", "group": 1, "type": "number"}]}]}
SIGNIN_X = {"ready": {"head": "button Sign In"}, "login": {"description": "password"},
            "records": [{"id": "form", "columns": [{"field": "user", "node": {"description": "user name"},
                                                     "attr": "value"}]}]}


def reg():
    return registry("desktop_runtime")


class Live:
    def __init__(self, tmp, policy="calc", *, token=TOKEN, faults=None, model=True):
        self.state = str(tmp / "app.json")
        write_state(self.state, faults=faults or {})
        self.model = FakeDesktopModel(policy).start() if model else None
        client = DesktopModelClient(base_url=self.model.base_url if model else "http://127.0.0.1:9/v1",
                                    api_key=self.model.api_key if model else "", model="fake-desktop")
        self.bridge = McpBridge(mcp_command(self.state), init_timeout=20)
        self.rt = dsrv.DesktopRuntime(str(tmp / "rt"), self.bridge, client=client, token=token)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), dsrv.make_desktop_handler(self.rt))
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def raw(self, method, path, body=None, *, token=TOKEN, timeout=60):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        c.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=h)
        r = c.getresponse()
        out = r.status, dict(r.getheaders()), json.loads(r.read() or b"{}")
        c.close()
        return out

    def ok(self, method, path, body=None, response=None):
        status, headers, data = self.raw(method, path, body)
        assert status == 200, data
        assert headers["WakeCore-Protocol"] == dsrv.PROTOCOL
        if response:
            reg().validate(data, response)
        return data

    def err(self, method, path, body=None, **kw):
        status, headers, data = self.raw(method, path, body, **kw)
        reg().validate(data, "error.v1.schema.json")
        return status, data["error"]

    def app(self):
        return read_state_file(self.state)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.rt.close()
        if self.model:
            self.model.stop()


@pytest.fixture
def make(tmp_path):
    lives = []

    def mk(policy="calc", **kw):
        lv = Live(tmp_path / f"l{len(lives)}", policy, **kw)
        lives.append(lv)
        return lv
    for d in range(4):
        (tmp_path / f"l{d}").mkdir()
    yield mk
    for lv in lives:
        lv.close()


def act(key="eff_1", app=CALC, goal="compute 12 x 3", **kw):
    return {"key": key, "attempt": "att_1", "app": app, "apps": APPS, "goal": goal, "timeout_s": 30,
            "max_steps": 5, **kw}


# ------------------------------------------------------------------ the eye

def test_health_capabilities_and_openapi(make):
    lv = make()
    h = lv.ok("GET", "/v1/health", response="health.v1.schema.json")
    assert h["protocol"] == "wakecore.desktop-runtime/1" and h["bridge"]["ok"] is True
    assert h["bridge"]["detail"] == {"brokerVerified": True, "permissionMode": "fake", "clientBuild": "1"}
    assert "path" not in json.dumps(h["bridge"]).lower()          # no local paths leak
    cap = lv.ok("GET", "/v1/capabilities", response="capabilities.v1.schema.json")
    assert cap["act"]["action_space"] == "wakecore.desktop-ax/1" and "credential_field_refusal" in cap["act"]["guards"]
    assert {f"{r.method} {r.template}" for r in dsrv.ROUTES} == set(cap["endpoints"])
    doc = dsrv.openapi()
    assert doc["info"]["x-wakecore-protocol"] == dsrv.PROTOCOL and "/v1/act" in doc["paths"]


def test_observe_reads_the_calculator(make):
    lv = make()
    r = lv.ok("POST", "/v1/observe", {"app": CALC, "apps": APPS, "extractor": CALC_X},
              response="observe.v1.schema.json#/$defs/response")
    assert r["outcome"] == "SUCCESS" and r["records"] == {"display": {"value": 0}} and r["window"] == "Calculator"
    assert len(r["content_digest"]) == 64
    v = lv.ok("POST", "/v1/verify", {"app": CALC, "apps": APPS, "extractor": CALC_X, "record_id": "display"},
              response="verify.v1.schema.json#/$defs/response")
    assert v["present"] and v["record"] == {"value": 0}
    assert lv.app()["calls"] == []                                  # reading never acts


def test_observe_login_screen_and_outcomes(make):
    lv = make()
    r = lv.ok("POST", "/v1/observe", {"app": SIGNIN, "apps": APPS, "extractor": SIGNIN_X},
              response="observe.v1.schema.json#/$defs/response")
    assert r["outcome"] == "AUTH_REQUIRED" and r["error_code"] == "login_screen" and r["records"] == {}
    bad = lv.ok("POST", "/v1/observe", {"app": CALC, "apps": APPS, "extractor": {"records": []}})
    assert bad["outcome"] == "SCHEMA_INVALID" and bad["error_code"].startswith("extractor_invalid")
    not_ready = lv.ok("POST", "/v1/observe", {"app": CALC, "apps": APPS, "extractor": SIGNIN_X})
    assert not_ready["outcome"] == "SCHEMA_INVALID" and not_ready["error_code"] == "not_ready"
    gone = lv.ok("POST", "/v1/observe", {"app": "com.example.absent", "apps": ["com.example.absent"],
                                         "extractor": CALC_X}, response="observe.v1.schema.json#/$defs/response")
    assert gone["outcome"] == "UNAVAILABLE" and gone["error_code"] == "app_state_failed"


def test_scope_and_request_validation(make):
    lv = make()
    status, e = lv.err("POST", "/v1/observe", {"app": SIGNIN, "apps": [CALC], "extractor": CALC_X})
    assert (status, e["code"]) == (403, "app_outside_scope")
    status, e = lv.err("POST", "/v1/observe", {"app": "Calculator", "apps": APPS, "extractor": CALC_X})
    assert (status, e["code"]) == (400, "invalid_request")
    status, e = lv.err("POST", "/v1/act", act(app=SIGNIN, apps=[CALC]))
    assert (status, e["code"]) == (403, "app_outside_scope")
    status, e = lv.err("GET", "/v1/health", token="wrong")
    assert (status, e["code"]) == (401, "unauthorised")
    assert lv.ok("POST", "/v1/extractors/validate", {"extractor": CALC_X},
                 response="extractor.v1.schema.json#/$defs/validate_response")["mode"] == "records"
    assert lv.app()["calls"] == []


def test_wrong_bundle_is_never_read(make):
    lv = make(faults={"wrong_bundle": True})
    r = lv.ok("POST", "/v1/observe", {"app": CALC, "apps": APPS, "extractor": CALC_X})
    assert r["outcome"] == "UNAVAILABLE" and r["error_code"] == "app_mismatch"
    a = lv.ok("POST", "/v1/act", act(), response="act.v1.schema.json#/$defs/response")
    assert (a["status"], a["reason"], a["mutating_actions"]) == ("needs_human", "app_mismatch", 0)
    assert lv.model.requests == [] and lv.app()["calls"] == []


def test_diffs_are_never_parsed(make):
    lv = make(faults={"diff": True})
    for _ in range(2):   # the second read would be a diff without disableDiff
        r = lv.ok("POST", "/v1/observe", {"app": CALC, "apps": APPS, "extractor": CALC_X})
        assert r["outcome"] == "SUCCESS", r


# ------------------------------------------------------------------ the hand

def test_act_computes_and_is_sent_once(make):
    lv = make("calc")
    r = lv.ok("POST", "/v1/act", act(), response="act.v1.schema.json#/$defs/response")
    assert (r["status"], r["reason"]) == ("completed", "model_finished"), r
    assert r["mutating_actions"] == 6 and r["actions_executed"] == 6 and "36" in r["summary"]
    assert lv.app()["display"] == "36"
    obs = lv.ok("POST", "/v1/observe", {"app": CALC, "apps": APPS, "extractor": CALC_X})
    assert obs["records"]["display"]["value"] == 36                 # verified by a deterministic read
    again = lv.ok("POST", "/v1/act", act(), response="act.v1.schema.json#/$defs/response")
    assert again["deduplicated"] and again["mutating_actions"] == 6
    assert len(lv.app()["calls"]) == 6                                # nothing was sent twice
    new_attempt = lv.ok("POST", "/v1/act", act(attempt="att_2"))
    assert new_attempt["deduplicated"]                               # it did send: never re-run
    status, e = lv.err("POST", "/v1/act", act(goal="compute 12 + 3"))
    assert (status, e["code"]) == (409, "idempotency_key_reused")
    rec = lv.ok("GET", "/v1/acts/eff_1", response="act_record.v1.schema.json")
    assert [m["type"] for m in rec["mutating"]] == ["click"] * 6
    assert rec["artifacts"]["screenshots"][0] == "step-000.png"
    st = lv.ok("GET", "/v1/act/status?key=eff_1", response="act.v1.schema.json#/$defs/status_response")
    assert st == {"status": "finished", "mutating_actions": 6, "attempt": "att_1", "result_status": "completed"}


def test_credentials_never_reach_the_model_or_the_field(make):
    lv = make("password")
    r = lv.ok("POST", "/v1/act", act(app=SIGNIN, goal="sign in"), response="act.v1.schema.json#/$defs/response")
    assert (r["status"], r["reason"], r["mutating_actions"]) == ("needs_human", "credential_field", 0)
    assert lv.app()["password"] == "hunter2" and lv.app()["calls"] == []
    assert lv.model.requests and all("hunter2" not in body for body in lv.model.requests)
    assert "[hidden]" in lv.model.requests[0]


def test_typing_while_a_password_field_is_showing_is_refused(make):
    lv = make("type_login")
    r = lv.ok("POST", "/v1/act", act(app=SIGNIN, goal="sign in"))
    assert (r["status"], r["reason"], r["mutating_actions"]) == ("needs_human", "credential_field", 0)


@pytest.mark.parametrize("policy,reason", [("system_key", "system_key"), ("bad_index", "unknown_element"),
                                           ("disabled", "element_disabled")])
def test_refused_actions_are_not_sent(make, policy, reason):
    lv = make(policy)
    r = lv.ok("POST", "/v1/act", act(), response="act.v1.schema.json#/$defs/response")
    assert r["status"] == "completed" and r["mutating_actions"] == 0 and r["blocked"] == [reason]
    assert lv.app()["calls"] == []
    assert "REFUSED" in lv.model.requests[-1]


@pytest.mark.parametrize("policy,status,reason", [("forever", "incomplete", "max_steps"),
                                                  ("other_tool", "needs_human", "unsupported_action"),
                                                  ("needs_login", "needs_human", "needs_login")])
def test_loop_endings(make, policy, status, reason):
    lv = make(policy)
    r = lv.ok("POST", "/v1/act", act(max_steps=3), response="act.v1.schema.json#/$defs/response")
    assert (r["status"], r["reason"]) == (status, reason)
    assert r["mutating_actions"] == (3 if policy == "forever" else 0)


def test_bridge_timeout_is_unknown_not_retried(make):
    lv = make("one", faults={"hang_on": "click"})
    r = lv.ok("POST", "/v1/act", act(timeout_s=4), response="act.v1.schema.json#/$defs/response")
    assert (r["status"], r["reason"], r["mutating_actions"]) == ("incomplete", "bridge_timeout", 1)
    again = lv.ok("POST", "/v1/act", act(timeout_s=4, attempt="att_2"))
    assert again["deduplicated"] and len(lv.app()["calls"]) == 1     # may have happened: never repeated
    h = lv.ok("GET", "/v1/health", response="health.v1.schema.json")    # a fresh bridge process
    assert h["bridge"]["ok"] and lv.bridge.starts == 2


def test_app_error_counts_as_sent(make):
    lv = make("one", faults={"error_on": "click"})
    r = lv.ok("POST", "/v1/act", act())
    assert r["status"] == "completed" and r["mutating_actions"] == 1 and r["blocked"] == []
    assert "FAILED" in lv.model.requests[-1]


def test_egress_and_model_configuration(make):
    lv = make()
    status, e = lv.err("POST", "/v1/act", act(model_egress="model:openai-computer-use"))
    assert (status, e["code"]) == (409, "model_egress_mismatch")
    ok = lv.ok("POST", "/v1/act", act(key="eff_2", model_egress=lv.rt.client.model_egress))
    assert ok["status"] == "completed"
    nomodel = make(model=False)
    r = nomodel.ok("POST", "/v1/act", act(key="eff_3"), response="act.v1.schema.json#/$defs/response")
    assert (r["status"], r["reason"], r["mutating_actions"]) == ("model_error", "model_not_configured", 0)


def test_cancel_and_unknown_keys(make):
    lv = make()
    status, e = lv.err("POST", "/v1/acts/nope/cancel")
    assert (status, e["code"]) == (404, "act_not_found")
    status, e = lv.err("GET", "/v1/acts/nope")
    assert (status, e["code"]) == (404, "act_not_found")
    assert lv.ok("GET", "/v1/act/status?key=nope")["status"] == "never_received"
    lv.ok("POST", "/v1/act", act())
    c = lv.ok("POST", "/v1/acts/eff_1/cancel", response="act.v1.schema.json#/$defs/cancel_response")
    assert c == {"key": "eff_1", "cancel_requested": False, "status": "finished"}


def test_restart_marks_interrupted(make, tmp_path):
    lv = make("one", faults={"hang_on": "click"})
    lv.rt.journal.begin("eff_9", "a", CALC, request_digest="x")
    lv.rt.journal.append("eff_9", "mutating", {"type": "click", "element": "4", "at": 0})
    rt2 = dsrv.DesktopRuntime(lv.rt.state_dir, McpBridge(mcp_command(lv.state)), client=lv.rt.client)
    try:
        assert rt2.interrupted_on_start == 1
        assert rt2.act_status("eff_9")["status"] == "interrupted"
    finally:
        rt2.close()
