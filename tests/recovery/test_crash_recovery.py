"""Crash, lease and in-flight recovery on SQLite (T05-T10, T14-T16).

These prove the *logic* of recovery (leases, epochs, UNKNOWN, reconciliation) on a store
that serialises writers. They do not replace the PostgreSQL concurrency gate (M2).
"""
import threading
from datetime import timedelta

import pytest

from harness import TENANT, USER, approve, base_spec, email_waiting_approval, model_spec, trigger_ambiguous_change
from wakecore.adapters.models.scripted import timeout
from wakecore.adapters.sqlite_dev.store import SqliteStore
from wakecore.kernel.budget.ledger import AccountSpec, reserve
from wakecore.kernel.commands import tasks as task_cmd
from wakecore.kernel.domain.enums import (
    ActionStatus,
    DeliveryStatus,
    ModelCallStatus,
    ReservationStatus,
    StepStatus,
    TaskLifecycle,
    WaitReason,
    WaitStatus,
)
from wakecore.kernel.domain.errors import BudgetDeferred, StaleLease
from wakecore.kernel.domain.model import (
    ActionRecord,
    BudgetAccount,
    BudgetReservation,
    Delivery,
    ObservationRecord,
    SourceCheckpoint,
    Step,
    WaitRecord,
)
from wakecore.kernel.execution.executor import execute
from wakecore.kernel.execution.leases import claim_next_step, verify_for_commit
from wakecore.kernel.locks import tx
from wakecore.kernel.ports.faults import SimulatedCrash
from wakecore.kernel.scheduling.waits import event_watermark, register_wait, resolve_waits
from wakecore.kernel.service import KernelService

SRC = "school_account_demo"


def ingest(h, eid, **kw):
    body = h.envelope(eid, **kw)
    return KernelService(h.ctx).ingest(SRC, body, h.signature(body)).body


def settle(h, rounds=4, step=60):
    for _ in range(rounds):
        h.cycle(step)


@pytest.fixture
def published(h):
    h.standard()
    h.run()
    h.k.grades.set_score("PHARM", 88)
    return h


# ------------------------------------------------------------------ T06

@pytest.mark.parametrize("point", ["after_fetch", "before_commit_T4", "after_commit_T4",
                                   "after_dispatch_permit", "after_tool_execute"])
def test_t06_crash_anywhere_on_the_path_gives_exactly_one_effect(published, point):
    h = published
    h.faults.arm(point)
    with pytest.raises(SimulatedCrash):
        h.cycle()
    h.restart()
    settle(h)
    assert len(h.inbox()) == 1, point
    assert h.k.inbox.executions <= 2          # a lost permit may re-dispatch; the row is still unique
    notes = [a for a in h.actions() if a.tool_id == "inbox.notify"]
    assert [a.status for a in notes].count(ActionStatus.CONFIRMED) == 1
    # A crash before the tool ran is proven no-effect by reconciliation, then retried as a linked action.
    assert all(a.status in (ActionStatus.CONFIRMED, ActionStatus.FAILED_NO_EFFECT) for a in notes)
    [cp] = h.all(SourceCheckpoint)
    assert cp.local_revision == 2            # baseline + one change: never a half or double commit


def test_t06_after_tool_execute_is_reconciled_not_resent(published):
    h = published
    h.faults.arm("after_tool_execute")
    with pytest.raises(SimulatedCrash):
        h.cycle()
    assert h.k.inbox.executions == 1
    h.restart()
    settle(h)
    assert h.k.inbox.executions == 1 and len(h.inbox()) == 1
    assert h.audit("action.unknown") and h.audit("action.reconciled")


# ------------------------------------------------------------------ T07

def test_t07_expired_owner_cannot_commit_after_takeover(published):
    h = published
    ingest(h, "e1")
    stale = claim_next_step(h.ctx, "worker-A")           # A claims, then stalls past its lease
    assert stale is not None and stale.lease_owner == "worker-A"
    h.advance(h.ctx.config.lease_seconds + 1)
    settle(h, rounds=2)                                    # recovery requeues; worker-B finishes it
    cur = h.all(Step, {"step_id": stale.step_id})[0]
    assert cur.status is StepStatus.SUCCEEDED and cur.lease_epoch > stale.lease_epoch
    before = (len(h.all(ObservationRecord)), len(h.inbox()))
    out = execute(h.ctx, stale)                            # A wakes up and tries to commit
    assert out["outcome"] == "stale_lease"
    assert (len(h.all(ObservationRecord)), len(h.inbox())) == before
    with tx(h.store) as repo, pytest.raises(StaleLease):
        verify_for_commit(repo, stale)


def test_t07_lease_renewal_requires_current_epoch(published):
    from wakecore.kernel.execution.leases import renew

    h = published
    ingest(h, "e1")
    step = claim_next_step(h.ctx, "worker-A")
    h.advance(10)
    renewed = renew(h.ctx, step)
    assert renewed.lease_until > step.lease_until
    h.advance(h.ctx.config.lease_seconds + 1)
    with pytest.raises(StaleLease):
        renew(h.ctx, renewed)                              # an expired lease cannot be revived


# ------------------------------------------------------------------ T08

def test_t08_one_task_runs_one_step_at_a_time_other_tasks_proceed(published):
    h = published
    h.create(base_spec(task_id="grades_b", root_task_id="grades_b"))
    h.run()
    ingest(h, "e1")                                        # one delivery per task
    ingest(h, "e2")                                        # a second delivery for each task
    a = claim_next_step(h.ctx, "w1")
    b = claim_next_step(h.ctx, "w2")
    c = claim_next_step(h.ctx, "w3")
    assert a is not None and b is not None and a.task_id != b.task_id
    assert c is None                                       # both tasks busy: the second delivery waits
    execute(h.ctx, a)
    d = claim_next_step(h.ctx, "w3")
    assert d is not None and d.task_id == a.task_id
    execute(h.ctx, b)
    execute(h.ctx, d)
    h.run()
    assert {x.status for x in h.all(Delivery)} == {DeliveryStatus.PROCESSED}


# ------------------------------------------------------------------ T09

def test_t09_lost_email_response_is_reconciled_never_resent(h):
    action, approval = email_waiting_approval(h)
    approve(h, approval)
    h.k.email.lose_response_times = 1
    h.run()
    settle(h, rounds=2)
    [a] = h.all(ActionRecord, {"action_id": action.action_id})
    assert a.status is ActionStatus.CONFIRMED
    assert h.k.email.send_calls == 1 and len(h.k.email.sent) == 1
    assert [e.to_state for e in h.audit("action.reconciled")] == ["CONFIRMED"]


def test_t09_email_crash_after_send_is_reconciled_after_restart(h):
    action, approval = email_waiting_approval(h)
    approve(h, approval)
    h.faults.arm("after_tool_execute")
    with pytest.raises(SimulatedCrash):
        h.run()
    h.restart()
    h.advance(h.ctx.config.dispatch_lease_seconds + 1)
    settle(h, rounds=2)
    assert h.all(ActionRecord, {"action_id": action.action_id})[0].status is ActionStatus.CONFIRMED
    assert h.k.email.send_calls == 1


def test_t09_definite_failure_without_effect_is_not_unknown(h):
    action, approval = email_waiting_approval(h)
    approve(h, approval)
    h.k.email.fail_no_effect_times = 1
    h.run()
    settle(h, rounds=2)
    statuses = [a.status for a in h.actions() if a.tool_id == "email.send"]
    assert ActionStatus.UNKNOWN not in statuses
    assert len(h.k.email.sent) <= 1


# ------------------------------------------------------------------ T10

class CancelDuringSend:
    """Cancels the task while the provider call is in flight (no kernel transaction is open)."""

    def __init__(self, h, real):
        self.h, self.real, self.descriptor, self.cancel_result = h, real, real.descriptor, None

    def execute(self, request):
        with tx(self.h.store) as repo:
            self.cancel_result = task_cmd.cancel(repo, self.h.ctx, tenant_id=TENANT, principal=USER,
                                                 task_id="grades_demo")
        return self.real.execute(request)

    def reconcile(self, request):
        return self.real.reconcile(request)


def test_t10_cancel_during_dispatch_reports_in_flight_and_records_truth(h):
    action, approval = email_waiting_approval(h)
    approve(h, approval)
    tool = CancelDuringSend(h, h.ctx.tools["email.send"])
    h.ctx.tools["email.send"] = tool
    h.run()
    assert tool.cancel_result["in_flight"] == 1 and "在途" in tool.cancel_result["message"]
    assert h.runtime().lifecycle is TaskLifecycle.CANCELLED
    # The e-mail left before cancel could stop it: it is recorded as it happened, not hidden.
    assert h.all(ActionRecord, {"action_id": action.action_id})[0].status is ActionStatus.CONFIRMED
    assert h.k.email.send_calls == 1


def test_t10_cancel_before_dispatch_stops_the_effect(h):
    action, approval = email_waiting_approval(h)
    approve(h, approval)                                   # READY, not yet dispatched
    with tx(h.store) as repo:
        out = task_cmd.cancel(repo, h.ctx, tenant_id=TENANT, principal=USER, task_id="grades_demo")
    assert out["in_flight"] == 0
    h.run()
    assert h.all(ActionRecord, {"action_id": action.action_id})[0].status is ActionStatus.CANCELLED
    assert h.k.email.send_calls == 0


def test_t10_approval_after_cancel_is_refused(h):
    from wakecore.kernel.domain.errors import KernelError

    action, approval = email_waiting_approval(h)
    with tx(h.store) as repo:
        task_cmd.cancel(repo, h.ctx, tenant_id=TENANT, principal=USER, task_id="grades_demo")
    with pytest.raises(KernelError):
        approve(h, approval)
    h.run()
    assert h.k.email.send_calls == 0


# ------------------------------------------------------------------ T14

def _running_step(h):
    ingest(h, "base")
    step = claim_next_step(h.ctx, "w1")
    assert step is not None
    return step


def test_t14_event_between_read_and_wait_registration_is_not_lost(published):
    h = published
    step = _running_step(h)
    with tx(h.store) as repo:
        read_watermark = event_watermark(repo, TENANT)     # the step reads business state here
    ingest(h, "reply", type_="io.wakecore.grades.reply.v1")  # ...the reply lands before it parks
    with tx(h.store) as repo:
        cur = verify_for_commit(repo, step)
        register_wait(repo, h.ctx, cur, reason=WaitReason.SOURCE, match={"type": "io.wakecore.grades.reply.v1"},
                      due_at=h.clock.utc_now() + timedelta(hours=1), basis_watermark=read_watermark)
    with tx(h.store) as repo:
        assert resolve_waits(repo)["matched"] == 1
    [w] = h.all(WaitRecord)
    assert w.status is WaitStatus.MATCHED and w.matched_event_id
    assert h.all(Step, {"step_id": step.step_id})[0].status is StepStatus.READY


def test_t14_registration_time_watermark_would_have_missed_it(published):
    """Documents why the read-time watermark matters (the default is only safe with no match)."""
    h = published
    step = _running_step(h)
    ingest(h, "reply", type_="io.wakecore.grades.reply.v1")
    with tx(h.store) as repo:
        cur = verify_for_commit(repo, step)
        register_wait(repo, h.ctx, cur, reason=WaitReason.SOURCE, match={"type": "io.wakecore.grades.reply.v1"},
                      due_at=None)
    with tx(h.store) as repo:
        assert resolve_waits(repo)["matched"] == 0


def test_t14_timeout_resumes_to_recheck_instead_of_assuming_nothing_happened(published):
    h = published
    step = _running_step(h)
    with tx(h.store) as repo:
        cur = verify_for_commit(repo, step)
        register_wait(repo, h.ctx, cur, reason=WaitReason.SOURCE, match={"type": "never"},
                      due_at=h.clock.utc_now() + timedelta(minutes=5))
    h.advance(301)
    with tx(h.store) as repo:
        assert resolve_waits(repo)["timed_out"] == 1
    s = h.all(Step, {"step_id": step.step_id})[0]
    assert s.status is StepStatus.READY and s.input["wake"]["status"] == "TIMED_OUT"
    h.run()
    assert h.all(Step, {"step_id": step.step_id})[0].status is StepStatus.SUCCEEDED
    assert len(h.inbox()) == 1                             # the re-check found the real change


# ------------------------------------------------------------------ T15

def test_t15_last_budget_unit_is_spent_once_then_deferred_to_next_period(h):
    h.standard(model_spec(limits={"max_model_attempts_per_day": 1}))
    h.run()
    trigger_ambiguous_change(h, "考试延期")
    assert len(h.model_calls()) == 1
    trigger_ambiguous_change(h, "考试再次延期")
    assert len(h.model_calls()) == 1                       # no second call, no overdraft
    [waiting] = [s for s in h.steps() if s.status is StepStatus.WAITING]
    assert waiting.wait_reason is WaitReason.BUDGET
    assert any(a.logical_action == "notify.budget_deferred" for a in h.actions())
    accts = [a for a in h.all(BudgetAccount) if a.scope == "root_task"]
    assert all(a.limit_amount - a.settled - a.reserved >= 0 for a in accts)
    h.clock.set(h.clock.utc_now().replace(hour=16, minute=1))  # 00:01 Asia/Shanghai next day
    settle(h, rounds=2)
    assert len(h.model_calls()) == 2                       # the deferred judgement ran in the new period
    assert h.all(Step, {"step_id": waiting.step_id})[0].status is StepStatus.SUCCEEDED


def test_t15_concurrent_reservations_cannot_both_take_the_last_unit(h, tmp_path):
    """Two connections race for one unit. SQLite serialises writers; PG is covered by the M2 gate."""
    h.standard()
    own = isinstance(h.store, SqliteStore)       # PostgresStore already uses one connection per thread
    stores = [SqliteStore(h.path, h.clock) for _ in range(2)] if own else [h.store, h.store]
    results, barrier = [], threading.Barrier(2)
    acct = [AccountSpec("root_task", "grades_demo", "model_attempts", "attempt", 1)]

    def go(i):
        barrier.wait()
        try:
            with tx(stores[i]) as repo:
                results.append(reserve(repo, h.ctx.ids, tenant_id=TENANT, accounts=acct, amount=1,
                                       root_task_id="grades_demo", attempt_id=f"a{i}", period="2026-09-29"))
        except BudgetDeferred:
            results.append("deferred")

    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    if own:
        [s.close() for s in stores]
    assert sorted(r == "deferred" for r in results) == [False, True]
    [a] = h.all(BudgetAccount, {"account_key": "root_task:grades_demo:model_attempts:2026-09-29"})
    assert a.reserved == 1 and a.limit_amount - a.settled - a.reserved == 0


# ------------------------------------------------------------------ T16

def test_t16_model_timeout_keeps_reservation_instead_of_zero(h):
    h.standard(model_spec())
    h.run()
    h.k.model.script_classify(timeout())
    trigger_ambiguous_change(h)
    [mc] = h.model_calls()
    assert mc.status is ModelCallStatus.UNKNOWN
    rows = h.all(BudgetReservation, {"reservation_id": mc.reservation_id})
    assert rows and {r.status for r in rows} == {ReservationStatus.AWAITING_RECONCILIATION}
    for r in rows:
        [acct] = h.all(BudgetAccount, {"account_key": r.account_key})
        assert acct.reserved == 1 and acct.settled == 0    # not silently released to zero
    judge = [s for s in h.steps() if s.kind == "judge"][0]
    assert judge.result["fallback"] == "model_timeout"
    assert any(a.logical_action == "notify.review" for a in h.actions())


def test_t16_crash_after_model_call_is_unknown_after_restart(h):
    h.standard(model_spec())
    h.run()
    h.faults.arm("after_model_call")
    with pytest.raises(SimulatedCrash):
        trigger_ambiguous_change(h)
    h.restart()
    settle(h, rounds=3)
    first = h.model_calls()[0]
    assert first.status is ModelCallStatus.UNKNOWN
    assert {r.status for r in h.all(BudgetReservation, {"reservation_id": first.reservation_id})} == \
        {ReservationStatus.AWAITING_RECONCILIATION}


def test_t06_no_effect_proven_while_paused_is_still_retried_after_resume(h):
    """Regression (found by the randomized fault test): a notification proven no-effect while the
    task was paused must get its retry successor, or the task can never complete."""
    h.standard()
    h.run()
    h.k.grades.set_score("PHARM", 88)
    h.faults.arm("after_dispatch_permit")
    with pytest.raises(SimulatedCrash):
        h.cycle()
    h.restart()
    rt = h.runtime()
    with tx(h.store) as repo:
        task_cmd.pause(repo, h.ctx, tenant_id=TENANT, principal=USER, task_id=rt.task_id)
    settle(h, rounds=6, step=120)                # lease expires -> UNKNOWN -> reconciled no_effect, while paused
    assert h.runtime().lifecycle is TaskLifecycle.PAUSED
    assert h.audit("action.reconciled") and len(h.inbox()) == 0
    notes = [a for a in h.actions() if a.tool_id == "inbox.notify"]
    assert any(a.status is ActionStatus.FAILED_NO_EFFECT for a in notes)
    assert any("#retry" in a.effect_key and a.status is ActionStatus.READY for a in notes)
    with tx(h.store) as repo:
        task_cmd.resume(repo, h.ctx, tenant_id=TENANT, principal=USER, task_id=rt.task_id)
    settle(h, rounds=6, step=120)
    assert len(h.inbox()) == 1 and h.k.inbox.executions == 1   # the PHARM notice, sent once, after resume
    h.k.grades.set_score("PATHOPHYS", 91)
    settle(h, rounds=6, step=300)
    assert h.runtime().lifecycle is TaskLifecycle.COMPLETED
    assert len(h.inbox()) == 2 and h.k.inbox.executions == 2
