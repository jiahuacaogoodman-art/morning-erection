"""Observation → change detection → routing, end to end on SQLite (T01, T03, T04, T17, T18, T19)."""
import pytest

from harness import TENANT, USER, base_spec, model_spec
from wakecore.kernel.domain.enums import ActionStatus, SourceHealth, StepKind, TaskLifecycle
from wakecore.kernel.domain.model import (
    DecisionRecord,
    ObservationRecord,
    SourceBinding,
    SourceCheckpoint,
    TriggerOccurrence,
)
from wakecore.kernel.service import KernelService, Principal


def _checkpoint(h):
    return h.all(SourceCheckpoint)[0]


def _binding(h):
    return h.all(SourceBinding)[0]


def _transitions(h):
    return [e for e in h.events() if e.internal]


# ------------------------------------------------------------------ T01

def test_t01_valid_snapshot_without_change_never_calls_a_model(h):
    h.standard(model_spec())
    h.k.grades.set_score("PHARM", 88)
    h.run()                                  # baseline, includes one published grade
    calls_after_baseline = h.k.model.call_count
    for _ in range(10):
        h.cycle()                            # page_rendered_at changes on every render
    assert h.k.grades.fetch_count == 11
    assert h.k.model.call_count == calls_after_baseline == 0
    assert h.model_calls() == []
    probes = [s for s in h.steps() if s.kind is StepKind.PROBE]
    assert [s.result["reason_code"] for s in probes[1:]] == ["no_change"] * 10
    assert len(_transitions(h)) == 1         # only the baseline moved the revision


# ------------------------------------------------------------------ T03

def test_t03_a_b_a_keeps_both_transitions(h):
    h.standard()
    h.run()
    for score in (88, 90, 88):
        h.k.grades.set_score("PHARM", score)
        h.cycle()
    revs = [e.data["revision"] for e in _transitions(h)]
    assert revs == [1, 2, 3, 4]
    kinds = [[c["kind"] for c in e.data["changes"]] for e in _transitions(h)[1:]]
    assert kinds == [["grade_published"], ["grade_changed"], ["grade_changed"]]
    bodies = [m.body for m in h.inbox()]
    assert len(bodies) == 3 and "88" in bodies[0] and "90" in bodies[1] and "88" in bodies[2]
    assert len({m.effect_key for m in h.inbox()}) == 3


# ------------------------------------------------------------------ T04

@pytest.mark.parametrize("mode,health", [
    ("login_page", SourceHealth.AUTH_REQUIRED),
    ("captcha", SourceHealth.AUTH_REQUIRED),
    ("empty", SourceHealth.DEGRADED),
    ("unavailable", SourceHealth.UNAVAILABLE),
    ("schema_invalid", SourceHealth.SCHEMA_INVALID),
    ("raise", SourceHealth.UNAVAILABLE),
    ("partial", SourceHealth.DEGRADED),
])
def test_t04_fault_pages_never_overwrite_the_trusted_snapshot(h, mode, health):
    h.standard(model_spec())
    h.k.grades.set_score("PHARM", 88)
    h.run()
    before = _checkpoint(h)
    inbox_before = len(h.inbox())

    h.k.grades.set_mode(mode, partial_ids=("PHARM",))
    h.cycle()
    after = _checkpoint(h)
    assert after.trusted_snapshot == before.trusted_snapshot
    assert after.local_revision == before.local_revision
    assert after.trusted_observation_id == before.trusted_observation_id
    assert _binding(h).health is health
    assert len(h.inbox()) == inbox_before                 # no "grades withdrawn" message
    assert h.k.model.call_count == 0                      # a broken page is not a semantic question
    last = h.all(ObservationRecord, order_by=("observed_at", "observation_id"))[-1]
    assert last.evidence_ref is not None                  # the fault itself is kept as evidence
    view = KernelService(h.ctx).get_task(Principal(TENANT, USER), "grades_demo")
    assert view["source"]["health"] == str(health)
    assert "可信快照未被覆盖" in view["explanation"]

    h.k.grades.set_mode("normal")
    h.cycle()
    assert _binding(h).health is SourceHealth.HEALTHY
    assert _checkpoint(h).local_revision == before.local_revision   # recovery is not a change
    assert len(h.inbox()) == inbox_before


def test_t04_fault_does_not_complete_or_break_the_task(h):
    h.standard()
    h.run()
    h.k.grades.set_mode("login_page")
    for _ in range(3):
        h.cycle()
    assert h.runtime().lifecycle is TaskLifecycle.ACTIVE
    h.k.grades.set_mode("normal")
    h.k.grades.set_score("PHARM", 1)
    h.k.grades.set_score("PATHOPHYS", 2)
    h.cycle()
    assert h.runtime().lifecycle is TaskLifecycle.COMPLETED


# ------------------------------------------------------------------ T17

def test_t17_one_hour_outage_is_one_coalesced_read(h):
    h.standard()
    h.run()
    fetches = h.k.grades.fetch_count
    h.advance(3600)                       # the worker was down for an hour: 12 slots missed
    h.k.grades.set_score("PHARM", 70)
    h.run()
    assert h.k.grades.fetch_count == fetches + 1
    occ = h.all(TriggerOccurrence, order_by=("scheduled_for",))
    assert len(occ) == 2 and occ[-1].coalesced_count == 12
    assert len(h.inbox()) == 1
    # The schedule stays on its grid instead of drifting.
    h.cycle()
    assert h.k.grades.fetch_count == fetches + 2


def test_t17_skip_missed_policy_does_not_fire_missed_slots(h):
    spec = base_spec(trigger={"catchup_policy": "skip_missed"})
    h.standard(spec)
    h.run()
    fetches = h.k.grades.fetch_count
    h.advance(3600 + 10)
    h.run()
    assert h.k.grades.fetch_count == fetches
    assert h.audit("trigger.skipped")


# ------------------------------------------------------------------ T18

def test_t18_source_checkpoint_is_separate_from_task_consumption(h):
    h.standard()
    h.run()
    h.k.grades.set_score("PHARM", 88)
    h.cycle()
    cp = _checkpoint(h)
    decisions = h.all(DecisionRecord)
    # Source fact: the checkpoint moved. Task consumption: a decision + an action per transition.
    assert cp.local_revision == 2
    trans = _transitions(h)
    assert {d.event_id for d in decisions} >= {e.event_id for e in trans}
    assert [a.causal_event_id for a in h.actions()] == [trans[-1].event_id]
    # A failed read moves neither the checkpoint nor consumes anything.
    h.k.grades.set_mode("unavailable")
    h.cycle()
    assert _checkpoint(h).version == cp.version
    assert len(h.actions()) == 1


def test_t18_consumption_failure_does_not_move_the_source_back(h):
    h.standard()
    h.run()
    h.k.grades.set_score("PHARM", 88)
    h.faults.arm("before_commit_T4")
    with pytest.raises(Exception):
        h.cycle()
    cp_mid = _checkpoint(h)
    assert cp_mid.local_revision == 1          # whole T4 rolled back together
    h.advance(60)   # lease expires -> recovery requeues with backoff
    h.run()
    h.advance(60)
    h.run()
    assert _checkpoint(h).local_revision == 2 and len(h.inbox()) == 1


# ------------------------------------------------------------------ T19

class BrokenInbox:
    """Inbox whose response is always lost and which cannot be reconciled."""

    def __init__(self, real):
        import dataclasses

        self.real = real
        self.descriptor = dataclasses.replace(real.descriptor, supports_reconciliation=False)

    def execute(self, request):
        raise ConnectionError("response lost")

    def reconcile(self, request):  # pragma: no cover - never called: reconciliation unsupported
        raise AssertionError("must not be called")


def _finish_all_grades(h):
    h.k.grades.set_score("PHARM", 88)
    h.k.grades.set_score("PATHOPHYS", 91)


def test_t19_draining_until_final_notification_is_confirmed(h):
    from wakecore.kernel.actions.coordinator import dispatch_once
    from wakecore.kernel.execution.executor import run_next
    from wakecore.kernel.scheduling.scheduler import tick

    h.standard()
    h.run()
    _finish_all_grades(h)
    h.advance(300)
    tick(h.ctx)
    while run_next(h.ctx, "w1"):
        pass
    assert h.runtime().lifecycle is TaskLifecycle.DRAINING   # goal met, notification not delivered yet
    assert [a.status for a in h.actions()] == [ActionStatus.READY]
    while dispatch_once(h.ctx, "w1"):
        pass
    assert h.runtime().lifecycle is TaskLifecycle.COMPLETED
    assert len(h.inbox()) == 1


def test_t19_unknown_final_notification_blocks_completion(h):
    from wakecore.kernel.commands import approvals
    from wakecore.kernel.locks import tx

    h.standard()
    h.run()
    real = h.ctx.tools["inbox.notify"]
    h.ctx.tools["inbox.notify"] = BrokenInbox(real)
    _finish_all_grades(h)
    h.cycle()
    for _ in range(5):
        h.cycle(3600)
    rt = h.runtime()
    assert rt.lifecycle is TaskLifecycle.DRAINING
    [action] = h.actions()
    assert action.status is ActionStatus.UNKNOWN            # never assumed delivered, never re-sent
    assert h.k.grades.fetch_count == 2                       # draining stops new observation
    view = KernelService(h.ctx).get_task(Principal(TENANT, USER), "grades_demo")
    assert view["in_flight"] == 1 and "待确认" in view["explanation"]
    with tx(h.store) as repo:
        approvals.resolve_manually(repo, h.ctx, tenant_id=TENANT, actor=USER, action_id=action.action_id,
                                   note="checked the inbox by hand")
    assert h.runtime().lifecycle is TaskLifecycle.COMPLETED


def test_t19_lost_inbox_response_is_reconciled_not_resent(h):
    h.standard()
    h.run()
    _finish_all_grades(h)
    h.faults.arm("after_tool_execute")          # message committed, result never recorded
    with pytest.raises(Exception):
        h.cycle()
    assert h.runtime().lifecycle is TaskLifecycle.DRAINING
    h.cycle(120)                                # dispatch lease expires -> UNKNOWN -> reconcile
    h.run()
    assert h.runtime().lifecycle is TaskLifecycle.COMPLETED
    assert h.k.inbox.executions == 1 and len(h.inbox()) == 1
    assert h.audit("action.reconciled")
