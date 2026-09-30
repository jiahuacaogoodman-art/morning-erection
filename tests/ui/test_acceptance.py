"""V0.3 acceptance: "在这个网页插个眼，有位置以后帮我操作。"

manual login -> persistent session -> periodic Playwright observation -> change -> wake ->
planner -> authority -> approval -> Computer Use -> Playwright re-observation -> CONFIRMED
-> goal observed -> COMPLETED.
"""
from harness import TENANT
from wakecore.kernel.domain.enums import ActionStatus, TaskLifecycle
from wakecore.kernel.domain.model import ActionAttempt, Approval, ObservationRecord
from wakecore.kernel.replay import replay_task


def test_watch_then_operate_then_verify_then_complete(world):
    w, h = world, world.h
    w.setup()
    assert w.web.fetch_count == 1 and w.fake.requests == []      # baseline read, no model
    for _ in range(3):                                            # nothing changes: nothing happens
        h.cycle()
    assert w.web.fetch_count == 4 and w.fake.requests == [] and h.k.model.calls == []
    assert [a for a in h.actions() if a.tool_id == "browser.cua"] == []

    action = w.open_seat()
    # the planner only saw what this task may use, with the origins it is bound to
    [bundle] = [b for kind, b in h.k.model.calls if kind == "plan"]
    tools = {t["tool_id"]: t for t in bundle.allowed_tools}
    assert tools["browser.cua"]["allowed_origins"] == [w.origin]
    assert "email.send" not in tools and "jw_alice" not in str(bundle)
    assert bundle.event["changes"][0]["kind"] == "slot_opened"
    # it is an external write: nothing happens before the user approves this exact digest
    assert action.status is ActionStatus.WAITING_APPROVAL
    assert w.fake.requests == [] and w.confirm_posts() == 0
    approval = h.all(Approval, {"action_id": action.action_id})[-1]
    assert approval.payload_digest == action.payload_digest
    assert "model:openai-compatible:127.0.0.1" in action.data_egress    # the fake model's host
    assert action.resource_scope["origins"] == [w.origin]

    w.approve(action)
    h.run()
    done = w.current(action)
    assert done.status is ActionStatus.CONFIRMED, done.resolution
    assert w.portal.enrolled() == ["PHARM"] and w.confirm_posts() == 1
    [att] = h.all(ActionAttempt, {"action_id": action.action_id})
    receipt = att.receipt["receipt"]
    assert receipt["verified"] is True and receipt["record"]["enrolled"] is True   # re-observed, not claimed
    assert receipt["act"]["mutating_requests"] == 1 and receipt["act"]["status"] == "completed"
    assert w.fake.screenshots >= 2 and w.fake.violations == []

    h.cycle()                                                     # the eye sees the goal reached
    assert h.runtime("enroll_watch").lifecycle is TaskLifecycle.COMPLETED
    h.cycle()
    assert w.confirm_posts() == 1                                 # and nothing is ever re-done

    # secrets never reached the model, and are not in WakeCore's database
    assert w.model_saw_secret() == []
    db = open(h.path, "rb").read()
    for token in w.session_tokens():
        assert token.encode() not in db
    assert b"correct-horse-battery" not in db

    # replay recomputes from the record; with the browser runtime gone it touches nothing
    w.side.stop()
    hits = w.portal.hit_count()
    s = replay_task(h.store, TENANT, "enroll_watch").summary()
    assert s["mismatches"] == [] and w.portal.hit_count() == hits
    assert len(h.all(ObservationRecord)) >= 6
