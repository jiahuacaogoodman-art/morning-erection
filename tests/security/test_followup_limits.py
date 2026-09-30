"""T13: self-registered follow-ups cannot extend expiry, depth, count, scope, capabilities or
source beyond the root; and a follow-up planned by the model goes through the same gate."""
from datetime import timedelta

import pytest

from harness import TENANT, base_spec, model_spec, trigger_ambiguous_change
from wakecore.kernel.commands.tasks import load_spec
from wakecore.kernel.domain.enums import Route, TriggerStatus
from wakecore.kernel.domain.errors import PolicyDenied
from wakecore.kernel.domain.model import Run, TriggerOccurrence, TriggerRecord
from wakecore.kernel.locks import lock_task, tx
from wakecore.kernel.ports.reasoning import DecisionProposal, FollowupProposal, PlanProposal, Usage
from wakecore.kernel.scheduling.followup import admit_followup

FU = {"followup": {"enabled": True}}


def first_run(h):
    return h.all(Run, {"task_id": "grades_demo"}, order_by=("created_at", "run_id"))[0]


def proposal(h, run_id=None, **over):
    base = dict(root_task_id="grades_demo", originating_run_id=run_id or first_run(h).run_id,
                reason="re-check after registrar update", due_at=h.clock.utc_now() + timedelta(hours=2),
                followup_key="recheck-1", expected_action_class="notify")
    base.update(over)
    return FollowupProposal(**base)


def admit(h, p):
    with tx(h.store) as repo:
        root, rt = lock_task(repo, TENANT, "grades_demo")
        spec = load_spec(repo, rt)
        return admit_followup(repo, h.ctx, root=root, rt=rt, root_spec=spec, spec=spec, proposal=p)


@pytest.fixture
def fu(h):
    h.standard(base_spec(**FU))
    h.run()
    return h


def test_t13_disabled_by_default(h):
    h.standard(base_spec())
    h.run()
    with pytest.raises(PolicyDenied, match="not enabled"):
        admit(h, proposal(h))


def test_t13_admitted_followup_is_bounded_by_the_root(fu):
    trg = admit(fu, proposal(fu))
    assert trg.depth == 1 and trg.expiry == fu.runtime().expires_at and trg.status is TriggerStatus.ACTIVE
    assert fu.audit("followup.admitted")


def test_t13_same_key_is_registered_once(fu):
    a = admit(fu, proposal(fu))
    b = admit(fu, proposal(fu, reason="model says again"))
    assert a.trigger_id == b.trigger_id
    assert len(fu.all(TriggerRecord, conds=[("followup_key", "not_null", None)])) == 1


def test_t13_count_limit(fu):
    admit(fu, proposal(fu, followup_key="k1"))
    admit(fu, proposal(fu, followup_key="k2"))                 # max_followup_count = 2
    with pytest.raises(PolicyDenied, match="count"):
        admit(fu, proposal(fu, followup_key="k3"))


@pytest.mark.parametrize("over,match", [
    ({"due_at_days": 30}, "expires"),
    ({"due_at_seconds": 60}, "minimum check interval"),
    ({"capabilities": ("email.send",)}, "stronger capabilities"),
    ({"resource_scope": {"semester": "spring_other"}}, "resource scope"),
    ({"source_ref": "someone_elses_source"}, "source"),
    ({"root_task_id": "another_root"}, "different root"),
    ({"followup_key": ""}, "followup_key"),
])
def test_t13_root_limits_cannot_be_exceeded(fu, over, match):
    over = dict(over)
    if "due_at_days" in over:
        over["due_at"] = fu.clock.utc_now() + timedelta(days=over.pop("due_at_days"))
    if "due_at_seconds" in over:
        over["due_at"] = fu.clock.utc_now() + timedelta(seconds=over.pop("due_at_seconds"))
    with pytest.raises(PolicyDenied, match=match):
        admit(fu, proposal(fu, **over))
    assert not fu.all(TriggerRecord, conds=[("followup_key", "not_null", None)])


def test_t13_originating_run_must_belong_to_the_task(h):
    h.standard(base_spec(**FU))
    other = base_spec(task_id="other_task", root_task_id="other_task", **FU)
    h.create(other)
    h.run()
    foreign = h.all(Run, {"task_id": "other_task"})[0]
    with pytest.raises(PolicyDenied, match="originating run"):
        admit(h, proposal(h, run_id=foreign.run_id))
    with pytest.raises(PolicyDenied, match="originating run"):
        admit(h, proposal(h, run_id="run_does_not_exist"))


def test_t13_depth_follows_the_chain_across_runs(fu):
    """A run started by a follow-up trigger is itself at depth 1; its follow-up would be depth 2."""
    trg = admit(fu, proposal(fu))
    fu.clock.set(trg.due_at)
    fu.run()
    [occ] = fu.all(TriggerOccurrence, {"trigger_id": trg.trigger_id})
    assert occ.run_id is not None
    with pytest.raises(PolicyDenied, match="depth"):
        admit(fu, proposal(fu, run_id=occ.run_id, followup_key="recheck-2"))
    # an ordinary interval run is still depth 0, so the root can still use its remaining count
    later = [r for r in fu.all(Run, {"task_id": "grades_demo"}) if r.run_id not in (occ.run_id,)][-1]
    assert admit(fu, proposal(fu, run_id=later.run_id, followup_key="recheck-3")).depth == 1


def test_t13_planner_followups_pass_the_same_gate(h):
    h.standard(model_spec(**FU))
    h.run()
    now = h.clock.utc_now()
    runs_before = {r.run_id for r in h.all(Run)}
    h.k.model.script_classify(DecisionProposal(route=Route.PLANNER, reason_code="plan_followup",
                                               usage=Usage(1, 1, 0), model_ref="offline-scripted"))

    def followups(run_id):
        return (FollowupProposal(root_task_id="grades_demo", originating_run_id=run_id, reason="ok",
                                 due_at=now + timedelta(hours=3), followup_key="ok", expected_action_class="notify"),
                FollowupProposal(root_task_id="grades_demo", originating_run_id=run_id, reason="too far",
                                 due_at=now + timedelta(days=60), followup_key="late",
                                 expected_action_class="notify"),
                FollowupProposal(root_task_id="grades_demo", originating_run_id=run_id, reason="escalate",
                                 due_at=now + timedelta(hours=3), followup_key="esc", expected_action_class="write",
                                 capabilities=("email.send",)))

    # The run id is only known once the step runs, so script the plan lazily.
    real_plan = h.k.model.plan

    def plan(bundle):
        run_id = [r for r in h.all(Run) if r.run_id not in runs_before][0].run_id
        return PlanProposal(steps=(), followups=followups(run_id), usage=Usage(1, 1, 0), model_ref="offline-scripted")

    h.k.model.plan = plan
    try:
        trigger_ambiguous_change(h)
    finally:
        h.k.model.plan = real_plan
    [plan_step] = [s for s in h.steps() if str(s.kind) == "plan"]
    assert len(plan_step.result["followups"]) == 1
    assert sorted(r["followup_key"] for r in plan_step.result["rejected_followups"]) == ["esc", "late"]
    assert len(h.audit("followup.rejected")) == 2
