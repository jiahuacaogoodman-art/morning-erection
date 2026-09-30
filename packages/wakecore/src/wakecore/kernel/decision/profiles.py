"""Decision profiles (V0.3 P1): the domain-specific part of wake decisions, made pluggable.

A profile decides, deterministically and without a model:
  * what kind each change is          (annotate)
  * which reasoning tier handles it   (route)
  * the model-free notification text  (template)
  * when the task's goal is reached   (completion evaluators)

`grades.v1` is the original rule set, unchanged (old tasks and old replay records use it
by default). `generic_watch.v1` is the "插个眼" profile: declarative conditions over
observed records ("seats_available > 0"), evaluated on every observation without an LLM.

Kinds for generic_watch.v1:
  <condition kind>  a condition went false -> true   actionable (plan or notify, per params)
  record_removed    a watched record disappeared      protected (never silently dropped)
  watched_changed   a watched field changed otherwise ambiguous (JUDGE / human review)
  condition_cleared / record_added / unwatched_changed  informational
"""
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..domain.enums import Route
from ..domain.errors import SchemaMismatch
from . import rules
from .templates import grades_notification
from .wake import WakeDecision
from .wake import annotate as grades_annotate
from .wake import route as grades_route

DEFAULT_PROFILE = "grades.v1"

Evaluator = Callable[[dict[str, dict[str, Any]], tuple[str, ...], dict[str, Any]], bool]


@dataclass(frozen=True)
class DecisionProfile:
    profile_id: str
    validate: Callable[[dict[str, Any]], None]
    annotate: Callable[[list[dict[str, Any]], dict[str, Any]], list[dict[str, Any]]]
    route: Callable[..., WakeDecision]
    template: Callable[[tuple[dict[str, Any], ...], dict[str, Any]], tuple[str, dict[str, Any]]]
    evaluators: dict[tuple[str, int], Evaluator]


# --------------------------------------------------------------------------- grades.v1

def _grades_validate(params: dict[str, Any]) -> None:
    if params:
        raise SchemaMismatch("decision.params: grades.v1 takes no parameters")


GRADES_V1 = DecisionProfile(
    profile_id="grades.v1",
    validate=_grades_validate,
    annotate=lambda changes, params: grades_annotate(changes),
    route=lambda params, **kw: grades_route(**kw),
    template=lambda changes, params: ("notify.grades_published", grades_notification(changes)),
    evaluators={k: (lambda fn: lambda snap, targets, params: fn(snap, targets))(fn)
                for k, fn in rules.COMPLETION_EVALUATORS.items()},
)


# --------------------------------------------------------------------------- generic_watch.v1

OPS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte", "truthy", "falsy", "contains", "not_contains"})
_UNARY = frozenset({"truthy", "falsy"})
_RESERVED_KINDS = frozenset({"record_removed", "watched_changed", "condition_cleared", "record_added",
                             "unwatched_changed"})
MAX_CONDITIONS = 16


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def condition_holds(cond: dict[str, Any], rec: Optional[dict[str, Any]]) -> bool:
    """Total and type-strict: a missing field or a type mismatch is simply "not met"."""
    if not isinstance(rec, dict) or cond["field"] not in rec:
        return False
    v, op, want = rec[cond["field"]], cond["op"], cond.get("value")
    if op == "truthy":
        return bool(v)
    if op == "falsy":
        return not v
    if op == "eq":
        return v == want and type(v) is type(want) or (_number(v) and _number(want) and v == want)
    if op == "ne":
        return not (v == want and (type(v) is type(want) or (_number(v) and _number(want))))
    if op in ("contains", "not_contains"):
        if not isinstance(v, (str, list)) or (isinstance(v, str) and not isinstance(want, str)):
            return False
        return (want in v) == (op == "contains")
    if not (_number(v) and _number(want)):
        return False
    return {"gt": v > want, "gte": v >= want, "lt": v < want, "lte": v <= want}[op]


def _check_conditions(conds: Any, where: str) -> None:
    if not isinstance(conds, list) or not conds or len(conds) > MAX_CONDITIONS:
        raise SchemaMismatch(f"{where} must be a non-empty list (max {MAX_CONDITIONS})")
    for i, c in enumerate(conds):
        if not isinstance(c, dict) or set(c) - {"field", "op", "value", "kind"}:
            raise SchemaMismatch(f"{where}[{i}] has unknown fields")
        if not isinstance(c.get("field"), str) or not c["field"]:
            raise SchemaMismatch(f"{where}[{i}].field must be a non-empty string")
        if c.get("op") not in OPS:
            raise SchemaMismatch(f"{where}[{i}].op must be one of {sorted(OPS)}")
        if c["op"] not in _UNARY and "value" not in c:
            raise SchemaMismatch(f"{where}[{i}].value is required for op {c['op']}")
        if "value" in c and not (c["value"] is None or isinstance(c["value"], (str, int, float, bool))):
            raise SchemaMismatch(f"{where}[{i}].value must be a scalar")
        kind = c.get("kind", "condition_met")
        if not isinstance(kind, str) or not kind.replace("_", "").isalnum() or not kind.islower() \
                or kind in _RESERVED_KINDS:
            raise SchemaMismatch(f"{where}[{i}].kind must be a lower_snake identifier, not a reserved kind")


def _watch_validate(params: dict[str, Any]) -> None:
    allowed = {"conditions", "watch_fields", "on_met", "label_field", "complete_when", "title"}
    if set(params) - allowed:
        raise SchemaMismatch(f"decision.params: unknown fields {sorted(set(params) - allowed)}")
    _check_conditions(params.get("conditions"), "decision.params.conditions")
    if "complete_when" in params:
        _check_conditions(params["complete_when"], "decision.params.complete_when")
    wf = params.get("watch_fields", [])
    if not isinstance(wf, list) or not all(isinstance(f, str) and f for f in wf):
        raise SchemaMismatch("decision.params.watch_fields must be a list of field names")
    if params.get("on_met", "notify") not in ("notify", "plan"):
        raise SchemaMismatch("decision.params.on_met must be 'notify' or 'plan'")
    for key in ("label_field", "title"):
        if key in params and not isinstance(params[key], str):
            raise SchemaMismatch(f"decision.params.{key} must be a string")


def _watched(params: dict[str, Any]) -> Optional[set[str]]:
    wf = params.get("watch_fields") or []
    if not wf:
        return None  # everything is watched
    return set(wf) | {c["field"] for c in params["conditions"]}


def classify_watch(change: dict[str, Any], params: dict[str, Any]) -> str:
    old, new = change.get("old"), change.get("new")
    conds = params["conditions"]
    if new is None:
        return "record_removed"
    newly = [c for c in conds if not condition_holds(c, old) and condition_holds(c, new)]
    if newly:
        return newly[0].get("kind", "condition_met")
    if old is None:
        return "record_added"
    if any(condition_holds(c, old) and not condition_holds(c, new) for c in conds):
        return "condition_cleared"
    fields = _watched(params)
    moved = {k for k in set(old) | set(new) if old.get(k) != new.get(k)}
    if fields is None or moved & fields:
        return "watched_changed"
    return "unwatched_changed"


def _met_kinds(params: dict[str, Any]) -> frozenset[str]:
    return frozenset(c.get("kind", "condition_met") for c in params["conditions"])


_WATCH_INFO = frozenset({"condition_cleared", "record_added", "unwatched_changed"})


def _watch_route(params: dict[str, Any], *, source_ok: bool, coverage_ok: bool, blocked_reason: str,
                 changes: list[dict[str, Any]], baseline: bool, model_available: bool,
                 egress_allowed: bool) -> WakeDecision:
    if not source_ok:
        return WakeDecision(Route.SOURCE_BLOCKED, blocked_reason)
    if not coverage_ok:
        return WakeDecision(Route.SOURCE_BLOCKED, "coverage_insufficient")
    if baseline:
        return WakeDecision(Route.NOOP, "baseline_established")
    if not changes:
        return WakeDecision(Route.NOOP, "no_change")
    kinds = _met_kinds(params)
    met = tuple(c for c in changes if c["kind"] in kinds)
    rest = tuple(c for c in changes if c["kind"] not in kinds and c["kind"] not in _WATCH_INFO)
    protected = any(c["kind"] == "record_removed" for c in rest)
    pending = ("ambiguous_changes_pending",) if rest else ()
    if met:
        if params.get("on_met", "notify") == "plan":
            if model_available and egress_allowed:
                return WakeDecision(Route.PLANNER, "watch_condition_met", met, rest, pending, protected)
            flag = "model_unavailable" if not model_available else "egress_not_allowed"
            return WakeDecision(Route.TEMPLATE_ACTION, "watch_condition_met", met, rest, (flag,) + pending,
                                protected)
        return WakeDecision(Route.TEMPLATE_ACTION, "watch_condition_met", met, rest, pending, protected)
    if not rest:
        return WakeDecision(Route.NOOP, "informational_only")
    if model_available and egress_allowed:
        return WakeDecision(Route.JUDGE, "semantic_classification", (), rest, (), protected)
    flag = "model_unavailable" if not model_available else "egress_not_allowed"
    return WakeDecision(Route.HUMAN_REVIEW, "needs_review", (), rest, (flag,), protected)


def watch_notification(changes: tuple[dict[str, Any], ...], params: dict[str, Any]) -> dict[str, Any]:
    label = params.get("label_field", "name")
    fields = sorted({c["field"] for c in params["conditions"]} | set(params.get("watch_fields") or []))
    lines, data = [], []
    for c in changes:
        rec = c.get("new") or c.get("old") or {}
        shown = {f: rec.get(f) for f in fields if f in rec}
        name = str(rec.get(label) or c["resource"])
        lines.append(f"{name}: {c['kind']} " + ", ".join(f"{k}={v}" for k, v in shown.items()))
        data.append({"resource": c["resource"], "kind": c["kind"], "fields": shown})
    return {"recipient": "self", "title": params.get("title", "关注的条件已满足"), "body": "\n".join(lines),
            "data": {"changes": data}}


def _all_targets_match(snapshot: dict[str, dict[str, Any]], targets: tuple[str, ...],
                       params: dict[str, Any]) -> bool:
    conds = params.get("complete_when")
    if not targets or not conds:
        return False
    return all(all(condition_holds(c, snapshot.get(t)) for c in conds) for t in targets)


GENERIC_WATCH_V1 = DecisionProfile(
    profile_id="generic_watch.v1",
    validate=_watch_validate,
    annotate=lambda changes, params: [{**c, "kind": classify_watch(c, params)} for c in changes],
    route=_watch_route,
    template=lambda changes, params: ("notify.condition_met", watch_notification(changes, params)),
    evaluators={("all_targets_match", 1): _all_targets_match},
)

PROFILES: dict[str, DecisionProfile] = {p.profile_id: p for p in (GRADES_V1, GENERIC_WATCH_V1)}


def get_profile(profile_id: str) -> DecisionProfile:
    try:
        return PROFILES[profile_id]
    except KeyError:
        raise SchemaMismatch(f"unknown decision profile {profile_id!r}") from None


def validate_decision(profile_id: str, params: dict[str, Any], completion: tuple[str, int]) -> None:
    profile = get_profile(profile_id)
    profile.validate(params)
    if completion not in profile.evaluators:
        raise SchemaMismatch(f"completion evaluator {completion[0]} v{completion[1]} is not provided by "
                             f"decision profile {profile_id}")
    if completion == ("all_targets_match", 1) and "complete_when" not in params:
        raise SchemaMismatch("completion all_targets_match needs decision.params.complete_when")
