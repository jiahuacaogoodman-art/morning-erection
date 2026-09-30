"""UI Runtime protocol v1: every endpoint's real response is checked against the published
schemas, plus the error envelope, idempotency-key reuse, cancel and session queries.

The runtime runs in-process on a real HTTP socket. No browser is started: every call here
is answered from the journal, the profile directory or the validators.
"""
import http.client
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

pytest.importorskip("wakecore_ui_runtime")

from wakecore.adapters.ui_runtime.client import (  # noqa: E402
    PROTOCOL,
    ProtocolMismatch,
    RuntimeRejected,
    RuntimeUnreachable,
    UiRuntimeClient,
    compatible,
)
from wakecore.protocol.jsonschema_lite import registry  # noqa: E402
from wakecore_ui_runtime import server as srv  # noqa: E402
from wakecore_ui_runtime.browser import extractor as ex  # noqa: E402

TOKEN = "t0ken"
ACT = {"key": "eff_1", "attempt": "att_1", "session_ref": "jw_alice", "goal": "enroll in PHARM",
       "start_url": "https://jw.example.edu/courses", "origins": ["https://jw.example.edu"], "login": None,
       "timeout_s": 60, "max_steps": 5}
TABLE = {"table": {"label": "课程"}, "key_field": "code",
         "columns": [{"field": "code", "headers": ["课程号"]}, {"field": "seats", "headers": ["余量"], "type": "int"}]}


def schema(ref):
    return lambda body: registry().validate(body, ref)


class Live:
    def __init__(self, tmp, token=TOKEN):
        self.rt = srv.Runtime(str(tmp / "state"), token=token, trace=False)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.make_handler(self.rt))
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.client = UiRuntimeClient(self.url, token=token)

    def raw(self, method, path, body=None, *, token=TOKEN, raw_body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json", **(headers or {})}
        if token:
            h["Authorization"] = f"Bearer {token}"
        data = raw_body if raw_body is not None else (None if body is None else json.dumps(body).encode())
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = r.status, dict(r.getheaders()), json.loads(r.read() or b"{}")
        c.close()
        return out

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.rt.sessions.close_all() if hasattr(self.rt.sessions, "close_all") else None


@pytest.fixture
def live(tmp_path):
    lv = Live(tmp_path)
    yield lv
    lv.close()


def err(live, method, path, body=None, **kw):
    status, headers, data = live.raw(method, path, body, **kw)
    registry().validate(data, "error.v1.schema.json")
    assert headers["WakeCore-Protocol"] == PROTOCOL
    return status, data["error"]


def journal_act(live, key="eff_1", *, digest=None, mutating=(), status="finished", result=None, attempt="att_1"):
    live.rt.journal.begin(key, attempt, "jw_alice", request_digest=digest)
    for m in mutating:
        live.rt.journal.append(key, "mutating", m)
    if status != "in_progress":
        live.rt.journal.update(key, status=status, finished_at=time.time(), result=result)


# ------------------------------------------------------------------ discovery

def test_health_and_capabilities_match_their_schemas(live):
    h = live.client.health()
    schema("health.v1.schema.json")(h)
    assert h["protocol"] == PROTOCOL and h["model_configured"] is False
    caps = live.client.capabilities()
    schema("capabilities.v1.schema.json")(caps)
    assert set(caps["extractor"]["types"]) == set(ex.TYPES)
    assert set(caps["extractor"]["transforms"]) == set(ex.TRANSFORMS)
    assert caps["act"]["cancel"] is True and caps["act"]["available"] is False
    assert {f"{r.method} {r.template}" for r in srv.ROUTES} == set(caps["endpoints"])


def test_every_response_carries_the_protocol_header(live):
    for path in ("/v1/health", "/v1/capabilities", "/v1/sessions", "/nope"):
        _, headers, _ = live.raw("GET", path)
        assert headers["WakeCore-Protocol"] == PROTOCOL


def test_every_route_with_a_body_names_a_resolvable_schema():
    for r in srv.ROUTES:
        if r.schema:
            registry().resolve(r.schema, "")
    registry().check()


# ------------------------------------------------------------------ error envelope

def test_missing_or_wrong_token_is_401(live):
    for token in ("", "wrong"):
        status, e = err(live, "GET", "/v1/health", token=token)
        assert (status, e["code"], e["retryable"]) == (401, "unauthorised", False)


def test_unknown_path_is_404_and_wrong_method_is_405(live):
    assert err(live, "GET", "/v2/health")[1]["code"] == "not_found"
    status, e = err(live, "GET", "/v1/observe")
    assert (status, e["code"]) == (405, "method_not_allowed") and "POST" in e["message"]
    assert err(live, "DELETE", "/v1/acts/x")[0] == 405


def test_bad_bodies_are_rejected_before_anything_runs(live):
    status, e = err(live, "POST", "/v1/observe", raw_body=b"{not json")
    assert (status, e["code"]) == (400, "bad_body")
    assert err(live, "POST", "/v1/observe", raw_body=b"[1, 2]")[1]["code"] == "bad_body"
    status, e = err(live, "POST", "/v1/act", {**ACT, "goal": 7})
    assert (status, e["code"], e["details"]["path"]) == (400, "invalid_request", "$.goal")
    assert err(live, "POST", "/v1/act", {**ACT, "surprise": 1})[1]["details"]["path"] == "$.surprise"
    assert err(live, "POST", "/v1/act", {**ACT, "max_steps": 500})[1]["code"] == "invalid_request"
    assert err(live, "POST", "/v1/act", {**ACT, "origins": ["https://jw.example.edu/path"]})[1]["code"] == \
        "invalid_request"
    assert live.rt.journal.get("eff_1") is None


def test_session_refs_are_checked_in_bodies_and_paths(live):
    status, e = err(live, "POST", "/v1/observe", {"session_ref": "../etc", "url": "https://a.example/",
                                                  "origins": ["https://a.example"], "extractor": TABLE})
    assert (status, e["code"]) == (400, "invalid_session_ref")
    assert err(live, "GET", "/v1/sessions/a%2F..%2Fb")[1]["code"] == "invalid_session_ref"
    assert err(live, "POST", "/v1/sessions/..%2Fx/release", {})[1]["code"] == "invalid_session_ref"


def test_oversized_bodies_are_413(live):
    c = http.client.HTTPConnection("127.0.0.1", live.port, timeout=10)
    c.putrequest("POST", "/v1/observe")
    c.putheader("Authorization", f"Bearer {TOKEN}")
    c.putheader("Content-Length", str(srv.MAX_BODY + 1))
    c.endheaders()
    r = c.getresponse()
    data = json.loads(r.read())
    c.close()
    assert r.status == 413 and data["error"]["code"] == "payload_too_large"
    assert data["error"]["details"]["max_body_bytes"] == srv.MAX_BODY


def test_error_codes_in_the_schema_are_the_ones_the_server_uses():
    import inspect
    codes = set(registry().resolve("error.v1.schema.json", "")[0]["properties"]["error"]["properties"]["code"]["enum"])
    src = inspect.getsource(srv)
    import re
    used = set(re.findall(r'ApiError\(\d{3}, "([a-z_]+)"', src))
    assert used <= codes, used - codes


# ------------------------------------------------------------------ extractors

@pytest.mark.parametrize("cfg", [
    TABLE,
    {"table": {"selector": "#t"}, "key_field": "id",
     "columns": [{"field": "id", "headers": ["ID"]}, {"field": "price", "headers": ["Price"], "type": "number",
                                                          "pattern": r"([\d.,]+)", "group": 1, "default": None}]},
    {"list": {"item": "li.todo"}, "ready": "ul.todo-list", "key_field": "title",
     "columns": [{"field": "title", "selector": "label", "transform": "lower"},
                 {"field": "done", "selector": "input:checked", "type": "exists"}]},
])
def test_valid_extractors_are_accepted_by_both_schema_and_runtime(live, cfg):
    registry().validate(cfg, "extractor.v1.schema.json")
    r = live.client.validate_extractor(cfg)
    schema("extractor.v1.schema.json#/$defs/validate_response")(r)
    assert r["ok"] is True and r["mode"] == ("list" if "list" in cfg else "table")


@pytest.mark.parametrize("cfg", [
    {},
    {**TABLE, "columns": []},
    {**TABLE, "columns": [{"field": "code", "headers": ["课程号"], "type": "date"}]},
    {**TABLE, "columns": [{"field": "code", "headers": ["课程号"]}, {"field": "x", "headers": ["X"], "shade": 1}]},
    {**TABLE, "list": {"item": "li"}},
    {**TABLE, "key_attr": "data-id"},                                                      # both keys
    {"table": {"label": "课程"}, "columns": TABLE["columns"]},                               # no key
    {"list": {"item": "li"}, "key_field": "t", "columns": [{"field": "t", "selector": "b"}]},   # list needs ready
    {**TABLE, "columns": [*TABLE["columns"], {"field": "n", "headers": ["N"], "transform": "title"}]},
])
def test_invalid_extractors_are_refused_by_both_schema_and_runtime(live, cfg):
    assert not registry().is_valid(cfg, "extractor.v1.schema.json")
    r = live.client.validate_extractor(cfg)
    schema("extractor.v1.schema.json#/$defs/validate_response")(r)
    assert r["ok"] is False and r["error"]["code"] == "extractor_invalid"


@pytest.mark.parametrize("cfg", [
    {**TABLE, "columns": [*TABLE["columns"], {"field": "n", "headers": ["N"], "pattern": "(unclosed"}]},
    {**TABLE, "columns": [*TABLE["columns"], {"field": "n", "headers": ["N"], "pattern": "a", "group": 1}]},
    {**TABLE, "columns": [*TABLE["columns"], {"field": "n", "headers": ["N"], "group": 1}]},
    {**TABLE, "columns": [*TABLE["columns"], {"field": "n", "headers": ["N"], "type": "int", "default": "many"}]},
    {**TABLE, "columns": [{"field": "code", "headers": ["课程号"], "default": "X"}, TABLE["columns"][1]]},
])
def test_semantic_mistakes_the_schema_cannot_see_are_caught_by_the_runtime(live, cfg):
    assert live.client.validate_extractor(cfg)["ok"] is False


# ------------------------------------------------------------------ acts

def test_unknown_act_is_404_and_legacy_status_says_never_received(live):
    assert live.client.act_record("nope") is None
    status, e = err(live, "GET", "/v1/acts/nope")
    assert (status, e["code"]) == (404, "act_not_found")
    assert live.client.act_status("nope") == {"status": "never_received", "mutating_requests": 0}


def test_act_record_matches_its_schema(live):
    result = {"status": "completed", "reason": "done", "steps": 3, "actions_executed": 4, "mutating_requests": 1,
              "blocked_requests": 0, "final_url": "https://jw.example.edu/courses"}
    journal_act(live, "k/with spaces", digest=srv.request_digest(ACT), result=result,
                mutating=[{"method": "POST", "url": "https://jw.example.edu/api/enroll", "at": time.time()}])
    rec = live.client.act_record("k/with spaces")
    schema("act_record.v1.schema.json")(rec)
    assert rec["status"] == "finished" and rec["request_digest"] == srv.request_digest(ACT)
    assert rec["artifacts"] == {"ref": None, "screenshots": [], "trace": None}
    assert live.client.act_status("k/with spaces")["mutating_requests"] == 1


def test_act_record_lists_evidence_files(live):
    journal_act(live, "eff_1", digest=srv.request_digest(ACT))
    adir = os.path.join(live.rt.state_dir, "artifacts", srv.hashlib.sha256(b"eff_1").hexdigest()[:24])
    os.makedirs(adir)
    for f in ("step-001.png", "step-002.png", "trace.zip", "notes.txt"):
        open(os.path.join(adir, f), "wb").close()
    arts = live.client.act_record("eff_1")["artifacts"]
    assert arts["screenshots"] == ["step-001.png", "step-002.png"] and arts["trace"] == "trace.zip"
    assert arts["ref"].startswith("artifacts/")


def test_reusing_an_act_key_for_a_different_request_is_409(live):
    journal_act(live, digest=srv.request_digest(ACT), mutating=[{"method": "POST", "url": "https://jw.example.edu/api/enroll"}],
                result={"status": "completed"})
    with pytest.raises(RuntimeRejected) as e:
        live.client.act(**{**ACT, "goal": "drop PHARM"})
    assert (e.value.status, e.value.code, e.value.retryable) == (409, "idempotency_key_reused", False)
    assert e.value.details == {"journal_status": "finished", "mutating_requests": 1}


def test_the_same_request_again_is_answered_from_the_journal_without_a_browser(live):
    journal_act(live, digest=srv.request_digest(ACT), mutating=[{"method": "POST", "url": "https://jw.example.edu/api/enroll"}],
                result={"status": "completed", "reason": "done"})
    r = live.client.act(**{**ACT, "attempt": "att_2", "timeout_s": 90, "max_steps": 9})   # digest ignores these
    schema("act.v1.schema.json#/$defs/response")(r)
    assert r["deduplicated"] is True and r["status"] == "completed" and r["mutating_requests"] == 1
    assert r["action_space"] == srv.ACTION_SPACE
    assert live.rt.sessions.active() == []                   # no browser was opened


def test_v03_records_without_a_digest_still_deduplicate(live):
    journal_act(live, digest=None, mutating=[{"method": "POST", "url": "https://jw.example.edu/api/enroll"}],
                result={"status": "completed"})
    assert live.client.act(**{**ACT, "goal": "anything"})["deduplicated"] is True


def test_request_digest_covers_the_effect_and_ignores_the_budget():
    base = srv.request_digest(ACT)
    assert srv.request_digest({**ACT, "attempt": "x", "timeout_s": 999, "max_steps": 1}) == base
    assert srv.request_digest({**ACT, "origins": list(reversed(ACT["origins"]))}) == base
    for change in ({"goal": "g"}, {"start_url": "https://jw.example.edu/"}, {"session_ref": "bob"},
                   {"origins": ["https://other.example"]}, {"login": {"url_contains": ["/login"]}}):
        assert srv.request_digest({**ACT, **change}) != base


def test_cancel_unknown_is_404_and_cancel_of_a_finished_act_is_a_no_op(live):
    status, e = err(live, "POST", "/v1/acts/nope/cancel", {})
    assert (status, e["code"]) == (404, "act_not_found")
    journal_act(live, result={"status": "completed"})
    r = live.client.cancel_act("eff_1")
    schema("act.v1.schema.json#/$defs/cancel_response")(r)
    assert r == {"key": "eff_1", "cancel_requested": False, "status": "finished"}


def test_cancel_of_a_running_act_sets_its_event_and_stamps_the_journal(live):
    journal_act(live, status="in_progress")
    ev = threading.Event()
    live.rt._cancels["eff_1"], live.rt._running = ev, {"eff_1"}
    r = live.client.cancel_act("eff_1")
    schema("act.v1.schema.json#/$defs/cancel_response")(r)
    assert r["cancel_requested"] is True and r["status"] == "in_progress" and ev.is_set()
    assert live.client.act_record("eff_1")["cancel_requested_at"] is not None


def test_cancel_of_a_queued_act(live):
    ev = threading.Event()
    live.rt._cancels["eff_q"] = ev
    assert live.client.cancel_act("eff_q")["status"] == "queued" and ev.is_set()


# ------------------------------------------------------------------ sessions

def test_sessions_are_listed_without_secrets(live):
    assert live.client.sessions() == []
    assert live.client.session("jw_alice") is None
    status, e = err(live, "GET", "/v1/sessions/jw_alice")
    assert (status, e["code"]) == (404, "session_not_found")
    os.makedirs(os.path.join(live.rt.state_dir, "profiles", "jw_alice"))
    os.makedirs(os.path.join(live.rt.state_dir, "profiles", "not valid!"))
    [s] = live.client.sessions()
    schema("session.v1.schema.json#/$defs/list_response")({"sessions": [s]})
    assert s == {"session_ref": "jw_alice", "state": "closed", "profile_exists": True, "last_used_at": None}
    assert live.client.session("jw_alice") == s


def test_release_new_and_legacy_endpoints(live):
    r = live.client.release_session("jw_alice")
    assert r is False
    status, _, body = live.raw("POST", "/v1/sessions/release", {"session_ref": "jw_alice"})
    schema("session.v1.schema.json#/$defs/release_response")(body)
    assert status == 200 and body["released"] is False


# ------------------------------------------------------------------ client behaviour

def test_the_client_parses_the_envelope(live):
    with pytest.raises(RuntimeRejected) as e:
        UiRuntimeClient(live.url, token="bad").health()
    assert (e.value.status, e.value.code, e.value.retryable) == (401, "unauthorised", False)
    assert e.value.message == "missing or wrong bearer token"


def test_the_client_reads_v03_style_errors():
    e = RuntimeRejected(400, json.dumps({"error": "invalid session_ref"}))
    assert e.code == "invalid_session_ref" and e.retryable is False
    e = RuntimeRejected(502, "<html>bad gateway</html>")
    assert e.code == "http_502" and e.retryable is True


@pytest.mark.parametrize("proto,ok", [(PROTOCOL, True), ("wakecore.ui-runtime/1.3", True),
                                      ("wakecore.ui-runtime/2", False), ("other/1", False), (None, False)])
def test_protocol_compatibility(proto, ok):
    assert compatible(proto) is ok


def test_a_runtime_speaking_another_major_version_is_refused_before_any_request(live, monkeypatch):
    monkeypatch.setattr(live.rt, "health", lambda: {"ok": True, "protocol": "wakecore.ui-runtime/2"})
    seen = []
    orig = srv.dispatch
    monkeypatch.setattr(srv, "dispatch", lambda rt, m, p, b: seen.append(p) or orig(rt, m, p, b))
    with pytest.raises(ProtocolMismatch):
        live.client.act(**ACT)
    assert seen == ["/v1/health"] and live.rt.journal.get("eff_1") is None


def test_negotiation_happens_once_and_again_after_a_reconnect(live, monkeypatch):
    seen = []
    orig = srv.dispatch
    monkeypatch.setattr(srv, "dispatch", lambda rt, m, p, b: seen.append(p) or orig(rt, m, p, b))
    live.client.sessions()
    live.client.sessions()
    assert seen.count("/v1/health") == 1
    live.client._negotiated = None      # what a connection error does
    live.client.sessions()
    assert seen.count("/v1/health") == 2


def test_an_unreachable_runtime_is_distinguished(tmp_path):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(RuntimeUnreachable):
        UiRuntimeClient(f"http://127.0.0.1:{port}").act(**ACT)
