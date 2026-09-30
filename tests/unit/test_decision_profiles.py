"""V0.3 P1: decision profiles. grades.v1 is unchanged; generic_watch.v1 is declarative and model-free."""
import pytest

from harness import TENANT, base_spec, model_spec
from wakecore.kernel.decision.profiles import (
    GENERIC_WATCH_V1,
    GRADES_V1,
    classify_watch,
    condition_holds,
    validate_decision,
)
from wakecore.kernel.domain.enums import Route, TaskLifecycle
from wakecore.kernel.domain.errors import SchemaMismatch
from wakecore.kernel.domain.taskspec import parse_task_spec
from wakecore.kernel.replay import replay_task

SEATS = {"field": "seats", "op": "gt", "value": 0, "kind": "slot_opened"}
WATCH = {"conditions": [SEATS], "watch_fields": ["seats", "status"], "label_field": "name"}
SECTIONS = {"PHARM": {"name": "药理学", "seats": 0, "status": "full", "enrolled": False},
            "PATHOPHYS": {"name": "病理生理学", "seats": 0, "status": "full", "enrolled": False}}


# ------------------------------------------------------------------ pure

@pytest.mark.parametrize("cond,rec,ok", [
    (SEATS, {"seats": 1}, True),
    (SEATS, {"seats": 0}, False),
    (SEATS, {"seats": "3"}, False),          # a string is not a number: never "met" by accident
    (SEATS, {"seats": True}, False),         # neither is a bool
    (SEATS, {}, False),
    ({"field": "s", "op": "eq", "value": "open"}, {"s": "open"}, True),
    ({"field": "s", "op": "eq", "value": 1}, {"s": True}, False),
    ({"field": "s", "op": "contains", "value": "余"}, {"s": "有余量"}, True),
    ({"field": "s", "op": "truthy"}, {"s": ""}, False),
    ({"field": "s", "op": "lte", "value": 2.5}, {"s": 2}, True),
])
def test_condition_is_total_and_type_strict(cond, rec, ok):
    assert condition_holds(cond, rec) is ok


@pytest.mark.parametrize("old,new,kind", [
    ({"seats": 0}, {"seats": 2}, "slot_opened"),
    ({"seats": 2}, {"seats": 0}, "condition_cleared"),
    ({"seats": 2}, {"seats": 3}, "watched_changed"),
    ({"seats": 0, "status": "full"}, {"seats": 0, "status": "closed"}, "watched_changed"),
    ({"seats": 0, "room": "A"}, {"seats": 0, "room": "B"}, "unwatched_changed"),
    (None, {"seats": 0}, "record_added"),
    (None, {"seats": 4}, "slot_opened"),
    ({"seats": 0}, None, "record_removed"),
])
def test_generic_watch_kinds(old, new, kind):
    assert classify_watch({"resource": "X", "old": old, "new": new}, WATCH) == kind


def _route(changes, **kw):
    params = {**WATCH, **kw.pop("params", {})}
    args = {**dict(source_ok=True, coverage_ok=True, blocked_reason="", baseline=False, model_available=True,
                   egress_allowed=True), **kw}
    return GENERIC_WATCH_V1.route(params, changes=GENERIC_WATCH_V1.annotate(changes, params), **args)


def test_generic_watch_routing():
    opened = [{"resource": "A", "old": {"seats": 0}, "new": {"seats": 1}}]
    assert _route(opened).route is Route.TEMPLATE_ACTION                        # default on_met=notify
    assert _route(opened, params={"on_met": "plan"}).route is Route.PLANNER      # no JUDGE call needed
    d = _route(opened, params={"on_met": "plan"}, model_available=False)
    assert d.route is Route.TEMPLATE_ACTION and "model_unavailable" in d.uncertainty_flags
    removed = [{"resource": "A", "old": {"seats": 0}, "new": None}]
    assert _route(removed).route is Route.JUDGE and _route(removed).protected
    assert _route([{"resource": "A", "old": {"seats": 2}, "new": {"seats": 0}}]).route is Route.NOOP


@pytest.mark.parametrize("params", [
    {},
    {"conditions": []},
    {"conditions": [{"field": "seats", "op": "regex", "value": ".*"}]},
    {"conditions": [{"field": "seats", "op": "gt"}]},
    {"conditions": [{**SEATS, "kind": "record_removed"}]},
    {"conditions": [{**SEATS, "value": {"$gt": 0}}]},
    {"conditions": [SEATS], "on_met": "execute"},
    {"conditions": [SEATS], "surprise": 1},
])
def test_generic_watch_params_are_validated(params):
    with pytest.raises(SchemaMismatch):
        validate_decision("generic_watch.v1", params, ("all_targets_match", 1))


def test_profile_must_provide_the_completion_evaluator():
    with pytest.raises(SchemaMismatch):
        validate_decision("generic_watch.v1", WATCH, ("all_expected_courses_published", 1))
    with pytest.raises(SchemaMismatch):
        validate_decision("no_such.v9", {}, ("x", 1))
    validate_decision("grades.v1", {}, ("all_expected_courses_published", 1))
    assert set(GRADES_V1.evaluators) == {("all_expected_courses_published", 1)}


def test_specs_without_decision_keep_their_digest():
    """Old tasks must not change identity just because the parser learned a new field."""
    spec = parse_task_spec(base_spec(), tenant_id=TENANT)
    assert spec.decision is None and spec.decision_profile == "grades.v1"
    assert "decision" not in spec.to_dict()


# ------------------------------------------------------------------ through the kernel

def watch_spec(**params):
    return model_spec(
        source={"binding_ref": "school_account_demo",
                "resource_scope": {"semester": "fall_demo", "course_ids": ["PHARM", "PATHOPHYS"]}},
        observation={"first_snapshot": "baseline_only"},
        completion={"evaluator": "all_targets_match", "evaluator_version": 1, "required_outputs": []},
        decision={"profile": "generic_watch.v1",
                  "params": {**WATCH, "complete_when": [{"field": "enrolled", "op": "eq", "value": True}],
                             **params}})


@pytest.fixture
def hw(tmp_path):
    from harness import Harness

    return Harness(str(tmp_path / "w.db"), courses=SECTIONS)


def test_watch_notifies_on_condition_without_any_model_call(hw):
    hw.standard(watch_spec())
    hw.run()
    hw.k.grades.set_field("PHARM", room="B-201")   # unwatched: nothing happens
    hw.cycle()
    assert hw.inbox() == [] and hw.k.model.calls == []
    hw.k.grades.set_field("PHARM", seats=2, status="open")
    hw.cycle()
    [msg] = hw.inbox()
    assert msg.title == "关注的条件已满足" and "药理学: slot_opened" in msg.body
    assert hw.k.model.calls == []                   # the eye is deterministic (constraint 3)
    s = replay_task(hw.store, TENANT, "grades_demo")
    assert s.summary()["mismatches"] == []
    assert any(w["logical_action"] == "notify.condition_met" for i in s.items for w in i.would_propose)


def test_watch_goes_straight_to_the_planner_and_completes_on_goal(hw):
    from wakecore.kernel.ports.reasoning import PlanProposal, Usage

    hw.standard(watch_spec(on_met="plan"))
    hw.run()
    hw.k.model.script_plan(PlanProposal(steps=(), usage=Usage(1, 1, 1), model_ref="offline-scripted"))
    hw.k.grades.set_field("PHARM", seats=1, status="open")
    hw.cycle()
    assert [k for k, _ in hw.k.model.calls] == ["plan"]         # no JUDGE: the rule already decided
    [bundle] = [b for k, b in hw.k.model.calls]
    assert bundle.event["changes"][0]["kind"] == "slot_opened"
    for cid in ("PHARM", "PATHOPHYS"):
        hw.k.grades.set_field(cid, enrolled=True)
    hw.cycle()
    assert hw.runtime().lifecycle is TaskLifecycle.COMPLETED
    s = replay_task(hw.store, TENANT, "grades_demo").summary()
    assert s["mismatches"] == [] and s["external_writes"] == 0
