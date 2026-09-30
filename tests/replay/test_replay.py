"""T22: replay rebuilds decisions from recorded evidence, re-admits recorded model output, and
cannot perform side effects or writes of any kind."""

import pytest

from harness import TENANT, email_waiting_approval, model_spec, trigger_ambiguous_change
from wakecore.kernel.domain.enums import Route
from wakecore.kernel.domain.errors import NotFound, PolicyDenied
from wakecore.kernel.domain.model import Evidence, Step
from wakecore.kernel.locks import tx
from wakecore.kernel.ports.reasoning import DecisionProposal, Usage
from wakecore.kernel.ports.tables import TABLES
from wakecore.kernel.replay import ReadOnlyRepo, replay_task


def row_counts(h):
    with tx(h.store) as repo:
        return {name: repo.count(t.record, {}) for name, t in TABLES.items()}


def grades_flow(h):
    h.standard()
    h.run()
    h.k.grades.set_score("PHARM", 88)
    h.cycle()
    h.cycle()
    h.k.grades.set_score("PATHOPHYS", 91)
    h.cycle()


def test_t22_replay_matches_and_writes_nothing(h):
    grades_flow(h)
    before, inbox_exec = row_counts(h), h.k.inbox.executions
    report = replay_task(h.store, TENANT, "grades_demo")
    s = report.summary()
    assert s["replayed"] >= 4 and s["matches"] == s["replayed"] and s["mismatches"] == []
    assert s["external_writes"] == 0
    assert row_counts(h) == before and h.k.inbox.executions == inbox_exec
    notices = [w for i in report.items for w in i.would_propose if w["logical_action"] == "notify.grades_published"]
    assert len(notices) == 2          # the two notifications are re-derived, not re-sent


def test_t22_replay_is_repeatable(h):
    grades_flow(h)
    a = replay_task(h.store, TENANT, "grades_demo")
    b = replay_task(h.store, TENANT, "grades_demo")
    assert [(i.step_id, i.replayed) for i in a.items] == [(i.step_id, i.replayed) for i in b.items]


def test_t22_judge_is_replayed_from_recorded_output_without_calling_the_model(h):
    h.standard(model_spec())
    h.run()
    h.k.model.script_classify(DecisionProposal(route=Route.NOOP, reason_code="remark_noted",
                                               usage=Usage(3, 2, 1), model_ref="offline-scripted"))
    trigger_ambiguous_change(h)
    calls, before = len(h.k.model.calls), row_counts(h)
    report = replay_task(h.store, TENANT, "grades_demo")
    judged = [i for i in report.items if i.kind == "judge"]
    assert judged and all(i.match for i in judged)
    assert judged[0].replayed["route"] == str(Route.NOOP)
    assert len(h.k.model.calls) == calls and row_counts(h) == before


def test_t22_replay_never_touches_external_tools(h):
    action, approval = email_waiting_approval(h)
    from harness import approve
    approve(h, approval)
    h.run()
    assert h.k.email.send_calls == 1
    report = replay_task(h.store, TENANT, "grades_demo")
    assert report.external_writes == 0 and h.k.email.send_calls == 1
    assert not report.mismatches


def test_t22_tampered_evidence_is_detected(h):
    grades_flow(h)
    with tx(h.store) as repo:
        evs = [e for e in repo.find(Evidence, {"tenant_id": TENANT})
               if "PATHOPHYS" in str(e.content) and "91" in str(e.content)]
        ev = evs[0]
        content = dict(ev.content)
        records = {k: dict(v) for k, v in content["records"].items()}
        records["PATHOPHYS"]["score"] = None      # history rewritten after the fact
        repo.change(ev, content={**content, "records": records})
    report = replay_task(h.store, TENANT, "grades_demo")
    assert report.mismatches, "replay must not silently agree with rewritten evidence"


@pytest.mark.parametrize("method", ["insert", "insert_if_absent", "change", "try_change", "delete"])
def test_t22_replay_repository_refuses_writes(h, method):
    h.standard()
    h.run()
    with h.store.transaction() as uow:
        repo = ReadOnlyRepo(uow)
        step = repo.find(Step, {"tenant_id": TENANT}, limit=1)[0]
        with pytest.raises(PolicyDenied):
            getattr(repo, method)(step) if method != "change" else repo.change(step, status=step.status)


def test_t22_unknown_task_is_not_found(h):
    h.standard()
    with pytest.raises(NotFound):
        replay_task(h.store, TENANT, "nope")
    with pytest.raises(NotFound):
        replay_task(h.store, "tenant_b", "grades_demo")


def test_t22_replay_survives_crash_and_recovery_history(h):
    """Crash-interrupted steps leave no committed result; replay covers only committed history."""
    h.standard()
    h.run()
    h.k.grades.set_score("PHARM", 88)
    h.faults.arm("before_commit_T4")
    from wakecore.kernel.ports.faults import SimulatedCrash
    try:
        h.cycle()
    except SimulatedCrash:
        pass
    h.restart()
    for _ in range(3):
        h.cycle(60)
    assert len(h.inbox()) == 1
    report = replay_task(h.store, TENANT, "grades_demo")
    assert not report.mismatches and report.items
