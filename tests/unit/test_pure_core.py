"""Pure kernel functions: change detection, rules, routing, reducer, state machines, canonical form."""
from datetime import datetime, timezone

import pytest

from harness import base_spec
from wakecore.kernel.decision.rules import all_expected_published, classify_change
from wakecore.kernel.decision.wake import admit_model_decision, annotate, route
from wakecore.kernel.domain.canonical import canonical_json, digest
from wakecore.kernel.domain.enums import Completeness, ObservationOutcome, Route, SourceHealth
from wakecore.kernel.domain.errors import InvalidTransition, SchemaMismatch
from wakecore.kernel.domain.statemachines import ACTION, TASK, can, ensure
from wakecore.kernel.domain.taskspec import parse_task_spec
from wakecore.kernel.events.change_detection import diff, merge_declared, normalise
from wakecore.kernel.events.reducer import CheckpointState, PolicySnapshot, reduce
from wakecore.kernel.ports.observation import ObservationResult
from wakecore.kernel.ports.reasoning import DecisionProposal

NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)
POLICY = PolicySnapshot(tenant_id="t", task_id="task", source_ref="src", scope_key="sk", target_key="course_ids",
                        target_resources=("A", "B"), completeness_required="full_target_scope",
                        first_snapshot="notify_existing_results", ignore_fields=("page_rendered_at",),
                        completion_evaluator="all_expected_courses_published", completion_evaluator_version=1,
                        model_available=True, model_egress_allowed=True)


def obs(records, *, outcome=ObservationOutcome.SUCCESS, completeness=Completeness.COMPLETE, read=None, error=None):
    scope = {"course_ids": list(read if read is not None else records)} if outcome in (
        ObservationOutcome.SUCCESS, ObservationOutcome.PARTIAL) else {}
    return ObservationResult(outcome=outcome, completeness=completeness, scope=scope, records=records,
                             observed_at=NOW, error_code=error)


def rec(score, **kw):
    return {"name": "x", "score": score, "page_rendered_at": kw.pop("render", "r1"), **kw}


# ------------------------------------------------------------------ change detection

def test_ignore_fields_are_stripped_before_comparison():
    a = normalise({"A": rec(1, render="r1")}, ["page_rendered_at"])
    b = normalise({"A": rec(1, render="r2")}, ["page_rendered_at"])
    assert diff(a, b) == []


def test_partial_merge_never_reads_unread_as_deleted():
    trusted = {"A": {"score": 1}, "B": {"score": 2}}
    merged = merge_declared(trusted, {"A": {"score": 3}}, ["A"])
    assert merged == {"A": {"score": 3}, "B": {"score": 2}}
    # Inside the read scope, absence means confirmed absence.
    assert merge_declared(trusted, {}, ["A"]) == {"B": {"score": 2}}


@pytest.mark.parametrize("old,new,kind", [
    (None, {"score": None}, "record_added"),
    ({"score": None}, {"score": 90}, "grade_published"),
    ({"score": 90}, {"score": 91}, "grade_changed"),
    ({"score": 90}, {"score": None}, "grade_withdrawn"),
    ({"score": 90, "r": 1}, {"score": 90, "r": 2}, "record_changed"),
])
def test_rule_classification(old, new, kind):
    assert classify_change({"old": old, "new": new}) == kind


def test_completion_evaluator_needs_every_target():
    assert not all_expected_published({"A": {"score": 1}}, ("A", "B"))
    assert all_expected_published({"A": {"score": 1}, "B": {"score": 0}}, ("A", "B"))
    assert not all_expected_published({}, ())


# ------------------------------------------------------------------ routing (RFC §8.1)

def _route(changes, **kw):
    args = dict(source_ok=True, coverage_ok=True, blocked_reason="", changes=annotate(changes), baseline=False,
                model_available=True, egress_allowed=True)
    args.update(kw)
    return route(**args)


def test_route_order():
    assert _route([], source_ok=False, blocked_reason="login_page").route is Route.SOURCE_BLOCKED
    assert _route([], coverage_ok=False).route is Route.SOURCE_BLOCKED
    assert _route([]).route is Route.NOOP
    assert _route([{"resource": "A", "old": None, "new": {"score": None}}]).reason_code == "informational_only"
    published = {"resource": "A", "old": {"score": None}, "new": {"score": 1}}
    assert _route([published]).route is Route.TEMPLATE_ACTION
    ambiguous = {"resource": "A", "old": {"score": 1, "r": 1}, "new": {"score": 1, "r": 2}}
    assert _route([ambiguous]).route is Route.JUDGE
    d = _route([ambiguous], egress_allowed=False)
    assert d.route is Route.HUMAN_REVIEW and "egress_not_allowed" in d.uncertainty_flags
    d = _route([ambiguous], model_available=False)
    assert d.route is Route.HUMAN_REVIEW and "model_unavailable" in d.uncertainty_flags
    mixed = _route([published, ambiguous])
    assert mixed.route is Route.TEMPLATE_ACTION and mixed.judge_changes and \
        "ambiguous_changes_pending" in mixed.uncertainty_flags


def test_model_cannot_lower_protected_floor_or_invent_routes():
    withdrawn = {"resource": "A", "old": {"score": 1}, "new": {"score": None}}
    rule = _route([withdrawn], model_available=True)
    assert rule.route is Route.JUDGE and rule.protected
    d = admit_model_decision(rule, DecisionProposal(route=Route.NOOP, reason_code="ignore_it"))
    assert d.route is Route.HUMAN_REVIEW and "rule_protected_floor" in d.uncertainty_flags
    d = admit_model_decision(rule, DecisionProposal(route=Route.SOURCE_BLOCKED, reason_code="x"))
    assert d.route is Route.HUMAN_REVIEW and "invalid_model_route" in d.uncertainty_flags


def test_model_suggested_capabilities_are_recorded_not_granted():
    rule = _route([{"resource": "A", "old": {"score": 1, "r": 1}, "new": {"score": 1, "r": 2}}])
    d = admit_model_decision(rule, DecisionProposal(route=Route.NOOP, reason_code="fine",
                                                    suggested_capabilities=("email.send", "admin")))
    assert d.extra["suggested_capabilities"] == ["email.send", "admin"]
    assert d.route is Route.NOOP  # unprotected change: the model may lower attention, but gains nothing


# ------------------------------------------------------------------ reducer (T03/T04 at the pure level)

def test_reducer_a_b_a_gives_two_transitions():
    s0 = CheckpointState(0, None)
    t1 = reduce(s0, obs({"A": rec(1), "B": rec(None)}), POLICY)
    s1 = CheckpointState(t1.revision, t1.snapshot)
    t2 = reduce(s1, obs({"A": rec(2), "B": rec(None)}), POLICY)
    s2 = CheckpointState(t2.revision, t2.snapshot)
    t3 = reduce(s2, obs({"A": rec(1), "B": rec(None)}), POLICY)
    assert [t1.revision, t2.revision, t3.revision] == [1, 2, 3]
    assert t2.transition_key != t3.transition_key
    assert [c["kind"] for c in t3.changes] == ["grade_changed"]


def test_reducer_same_content_is_noop_even_if_render_changes():
    t1 = reduce(CheckpointState(0, None), obs({"A": rec(1), "B": rec(2)}), POLICY)
    t2 = reduce(CheckpointState(t1.revision, t1.snapshot), obs({"A": rec(1, render="x"), "B": rec(2, render="y")}),
                POLICY)
    assert t2.decision.route is Route.NOOP and t2.transition_key is None and t2.revision == t1.revision


@pytest.mark.parametrize("o,health", [
    (obs({}, outcome=ObservationOutcome.AUTH_REQUIRED, completeness=Completeness.UNKNOWN, error="login_page"),
     SourceHealth.AUTH_REQUIRED),
    (obs({}, outcome=ObservationOutcome.UNAVAILABLE, completeness=Completeness.UNKNOWN, error="http_503"),
     SourceHealth.UNAVAILABLE),
    (obs({}, outcome=ObservationOutcome.SCHEMA_INVALID, completeness=Completeness.UNKNOWN), SourceHealth.SCHEMA_INVALID),
    (ObservationResult(outcome=ObservationOutcome.SUCCESS, completeness=Completeness.UNKNOWN, scope={}, records={},
                       observed_at=NOW), SourceHealth.DEGRADED),
    (obs({"A": rec(1)}, outcome=ObservationOutcome.PARTIAL, completeness=Completeness.PARTIAL), SourceHealth.DEGRADED),
])
def test_reducer_never_trusts_bad_or_insufficient_observations(o, health):
    state = CheckpointState(4, {"A": {"name": "x", "score": 1}, "B": {"name": "x", "score": 2}})
    t = reduce(state, o, POLICY)
    assert not t.trusted and t.snapshot is None and t.revision == 4
    assert t.decision.route is Route.SOURCE_BLOCKED and t.health is health


def test_reducer_is_deterministic():
    o = obs({"A": rec(1), "B": rec(None)})
    assert reduce(CheckpointState(0, None), o, POLICY) == reduce(CheckpointState(0, None), o, POLICY)


# ------------------------------------------------------------------ state machines and canonical form

def test_terminal_states_have_no_exits():
    for table in (TASK, ACTION):
        for src, dsts in table.items():
            if not dsts:
                assert not any(can("task" if table is TASK else "action", src, d) for d in table)


def test_unknown_action_cannot_be_redispatched():
    with pytest.raises(InvalidTransition):
        ensure("action", "UNKNOWN", "DISPATCHING")
    with pytest.raises(InvalidTransition):
        ensure("action", "CONFIRMED", "DISPATCHING")
    with pytest.raises(InvalidTransition):
        ensure("task", "COMPLETED", "ACTIVE")


def test_canonical_json_is_order_independent():
    assert canonical_json({"b": 1, "a": [1, {"d": 2, "c": 3}]}) == canonical_json({"a": [1, {"c": 3, "d": 2}], "b": 1})
    assert digest({"x": 1}).startswith("sha256:")
    assert digest({"x": 1}) != digest({"x": 2})


# ------------------------------------------------------------------ TaskSpec parsing

def test_spec_parses_and_digest_is_stable():
    a = parse_task_spec(base_spec(), tenant_id="local_user")
    b = parse_task_spec(base_spec(), tenant_id="local_user")
    assert a.digest() == b.digest() and a.target_resources == ("PHARM", "PATHOPHYS")


@pytest.mark.parametrize("over", [
    {"schema_version": 2},
    {"surprise": True},
    {"trigger": {"kind": "interval", "every_seconds": 1, "timezone": "Asia/Shanghai",
                 "catchup_policy": "coalesce_latest"}},
    {"expires_at": "2026-10-06T00:00:00"},  # naive timestamps are ambiguous
    {"tenant_id": "someone_else"},
])
def test_spec_rejects_invalid_input(over):
    with pytest.raises(SchemaMismatch):
        parse_task_spec(base_spec(**over), tenant_id="local_user")
