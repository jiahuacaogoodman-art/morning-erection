"""V0.3 P2 recovery: a crashed or ambiguous browser operation becomes UNKNOWN and is resolved
by re-observation (reconcile), never by operating the page again (constraints 9, 10)."""
import threading
import time
from datetime import timedelta

import pytest

from harness import TENANT
from wakecore.kernel.domain.enums import ActionStatus, TaskLifecycle
from wakecore.kernel.domain.model import ActionAttempt, AuditEntry
from wakecore.kernel.ports.action import AuthorizedAction
from wakecore.kernel.ports.faults import SimulatedCrash
from wakecore.kernel.replay import replay_task


def waiting(w, **spec_kw):
    w.setup(**spec_kw)
    action = w.open_seat()
    assert action.status is ActionStatus.WAITING_APPROVAL
    w.approve(action)
    return action


def reconciled(w, action):
    return [a for a in w.h.all(AuditEntry, {"kind": "action.reconciled"}) if a.subject_id == action.action_id]


def test_runtime_crash_after_the_submit_is_resolved_by_re_observation(world):
    w, h = world, world.h
    action = waiting(w)
    w.side.faults(exit_after_mutating_response=True)
    h.run()
    assert w.side.wait_dead() == 18
    assert w.current(action).status is ActionStatus.UNKNOWN          # it cannot know, so it does not guess
    assert w.confirm_posts() == 1 and w.portal.enrolled() == ["PHARM"]

    w.side.restart()                                                 # same port, profile and journal
    h.cycle(60)
    assert w.current(action).status is ActionStatus.CONFIRMED
    [rec] = reconciled(w, action)
    assert rec.to_state == "CONFIRMED"
    assert w.confirm_posts() == 1                                    # nothing was re-done
    assert len(h.all(ActionAttempt, {"action_id": action.action_id})) == 1
    h.cycle()
    assert h.runtime("enroll_watch").lifecycle is TaskLifecycle.COMPLETED


def test_runtime_crash_before_any_submit_is_a_proven_no_effect(world):
    w, h = world, world.h
    action = waiting(w)
    w.side.faults(exit_before_mutating=True)
    h.run()
    assert w.side.wait_dead() == 17
    assert w.current(action).status is ActionStatus.UNKNOWN
    assert w.confirm_posts() == 0

    w.side.restart()                                                 # the journal marks the act interrupted
    h.cycle(60)
    cur = w.current(action)
    assert cur.status is ActionStatus.FAILED_NO_EFFECT
    assert reconciled(w, action)[0].to_state == "FAILED_NO_EFFECT"
    assert w.confirm_posts() == 0 and w.portal.enrolled() == []
    assert w.tool.act_calls == 1                                     # reconcile never operates the page again


def test_kernel_crash_during_the_act_is_recovered_after_restart(world):
    """The kernel process dies mid-dispatch (lease expires); the runtime finished the job."""
    w, h = world, world.h
    action = waiting(w)
    h.faults.arm("after_tool_execute")
    with pytest.raises(SimulatedCrash):
        h.run()
    assert w.confirm_posts() == 1                      # the page was operated; the kernel never heard
    w.restart_kernel()
    h = w.h
    h.advance(180 + 30 + 1)                            # past the attempt lease (deadline + grace)
    h.run()
    assert w.current(action).status is ActionStatus.CONFIRMED   # UNKNOWN -> reconcile -> re-observed
    assert w.confirm_posts() == 1 and w.tool.act_calls == 0     # the new process never re-operated


def test_portal_drops_the_connection_after_committing(world):
    w, h = world, world.h
    action = waiting(w)
    w.portal.set(crash_after_commit=True)
    h.run()
    assert w.current(action).status is ActionStatus.CONFIRMED        # the page tells the truth afterwards
    assert w.confirm_posts() == 1 and w.portal.enrolled() == ["PHARM"]


def test_competitor_takes_the_seat_unknown_then_no_effect_after_settling(world):
    w, h = world, world.h
    action = waiting(w)
    w.portal.set(competitor_on_confirm="PHARM")
    h.run()
    assert w.current(action).status is ActionStatus.UNKNOWN          # a submit went out; goal not reached
    assert w.confirm_posts() == 1 and w.portal.enrolled() == []
    h.cycle(60)
    assert w.current(action).status is ActionStatus.UNKNOWN          # still inside the settle window
    h.cycle(120)
    assert w.current(action).status is ActionStatus.FAILED_NO_EFFECT
    assert w.confirm_posts() == 1                                    # and it is not retried behind the user's back
    assert w.tool.act_calls == 1


def test_slow_backend_unknown_then_confirmed_without_resubmitting(world):
    w, h = world, world.h
    action = waiting(w, max_run_seconds=20)                          # act budget ~10 s after the verify reserve
    w.portal.set(confirm_delay=18.0)                                 # longer than the runtime waits
    h.run()
    assert w.current(action).status is ActionStatus.UNKNOWN
    deadline = time.time() + 30
    while not w.portal.enrolled() and time.time() < deadline:
        time.sleep(0.3)
    h.cycle(60)
    assert w.current(action).status is ActionStatus.CONFIRMED
    assert w.confirm_posts() == 1 and w.tool.act_calls == 1


def test_duplicate_dispatch_of_one_effect_reaches_the_page_once(world):
    """Two workers (or a retried attempt) executing the same effect_key concurrently."""
    w, h = world, world.h
    action = waiting(w)
    rt = h.runtime("enroll_watch")
    now = h.clock.utc_now()

    def req(attempt):
        return AuthorizedAction(
            tenant_id=TENANT, action_id=action.action_id, attempt_id=attempt, effect_key=action.effect_key,
            task_id=rt.task_id, tool_id="browser.cua", tool_version="0.3.0", payload=action.canonical_payload,
            payload_digest=action.payload_digest, revocation_epoch=0, permit="test", secret="jw_alice",
            resource_scope=action.resource_scope, data_egress=tuple(action.data_egress),
            deadline_at=now + timedelta(seconds=120))

    results = {}
    threads = [threading.Thread(target=lambda a=a: results.__setitem__(a, w.tool.execute(req(a))))
               for a in ("att_A", "att_B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert w.confirm_posts() == 1 and w.portal.enrolled() == ["PHARM"]
    assert {r.status for r in results.values()} <= {"confirmed", "unknown"}
    assert "confirmed" in {r.status for r in results.values()}
    again = w.tool.execute(req("att_C"))                             # a later redelivery
    assert again.status == "confirmed" and w.confirm_posts() == 1


def test_replay_after_recovery_recomputes_and_never_touches_the_page(world):
    w, h = world, world.h
    action = waiting(w)
    w.side.faults(exit_after_mutating_response=True)
    h.run()
    w.side.wait_dead()
    w.side.restart()
    h.cycle(60)
    h.cycle()
    assert h.runtime("enroll_watch").lifecycle is TaskLifecycle.COMPLETED
    w.side.stop()
    hits, posts, calls = w.portal.hit_count(), w.confirm_posts(), len(w.fake.requests)
    s = replay_task(h.store, TENANT, "enroll_watch").summary()
    assert s["mismatches"] == []
    assert (w.portal.hit_count(), w.confirm_posts(), len(w.fake.requests)) == (hits, posts, calls)
    assert w.current(action).status is ActionStatus.CONFIRMED
