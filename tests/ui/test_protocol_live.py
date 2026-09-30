"""Protocol v1 against the real sidecar process: real Chromium, real journal, fake model.

Every response is validated against the published schemas, and cooperative cancel is
exercised on a model that never finishes.
"""
import threading
import time

from conftest import SESSION

from wakecore.protocol.jsonschema_lite import registry


def valid(body, ref):
    registry().validate(body, ref)
    return body


def test_every_endpoint_answers_in_its_schema(world):
    w, c = world, world.client
    valid(c.health(), "health.v1.schema.json")
    caps = valid(c.capabilities(), "capabilities.v1.schema.json")
    assert caps["act"]["available"] is True and caps["act"]["variant"] == "ga"
    from conftest import EXTRACTOR
    obs = valid(c.observe(session_ref=SESSION, url=w.origin + "/courses", origins=[w.origin], extractor=EXTRACTOR,
                          timeouts={"navigation_s": 20, "ready_s": 5}),
                "observe.v1.schema.json#/$defs/response")
    assert obs["outcome"] == "SUCCESS" and "PHARM" in obs["records"]
    ver = valid(c.verify(session_ref=SESSION, url=w.origin + "/courses", origins=[w.origin], extractor=EXTRACTOR,
                         record_id="PHARM"), "verify.v1.schema.json#/$defs/response")
    assert ver["present"] is True
    [s] = valid({"sessions": c.sessions()}, "session.v1.schema.json#/$defs/list_response")["sessions"]
    assert s["session_ref"] == SESSION and s["state"] == "open" and s["profile_exists"] is True
    assert set(s) == {"session_ref", "state", "profile_exists", "last_used_at"}   # no cookies, no paths
    assert c.release_session(SESSION) is True
    assert c.session(SESSION)["state"] == "closed"


def test_an_act_can_be_canceled_between_steps_and_sends_nothing(make_world):
    w = make_world(policy="stall")
    c, key = w.client, "eff_cancel_1"
    out = {}
    t = threading.Thread(target=lambda: out.update(r=c.act(
        key=key, attempt="att_1", session_ref=SESSION, goal="enroll in PHARM", start_url=w.origin + "/courses",
        origins=[w.origin], login=w.login_markers if hasattr(w, "login_markers") else None, timeout_s=120,
        max_steps=200)))
    t.start()
    deadline = time.time() + 30
    while time.time() < deadline and not (c.act_record(key) or {}).get("status") == "in_progress":
        time.sleep(0.1)
    time.sleep(1.0)                                        # let it take a few steps
    r = valid(c.cancel_act(key), "act.v1.schema.json#/$defs/cancel_response")
    assert r["cancel_requested"] is True and r["status"] == "in_progress"
    t.join(60)
    assert not t.is_alive()
    res = valid(out["r"], "act.v1.schema.json#/$defs/response")
    assert res["status"] == "canceled" and res["mutating_requests"] == 0 and res["steps"] < 200
    rec = valid(c.act_record(key), "act_record.v1.schema.json")
    assert rec["status"] == "finished" and rec["cancel_requested_at"] is not None and rec["mutating"] == []
    assert rec["result"]["status"] == "canceled"
    again = c.act(key=key, attempt="att_1", session_ref=SESSION, goal="enroll in PHARM",
                  start_url=w.origin + "/courses", origins=[w.origin], login=None, timeout_s=60, max_steps=5)
    assert again["deduplicated"] is True and again["status"] == "canceled"
    assert w.confirm_posts() == 0
    assert c.cancel_act(key) == {"key": key, "cancel_requested": False, "status": "finished"}
