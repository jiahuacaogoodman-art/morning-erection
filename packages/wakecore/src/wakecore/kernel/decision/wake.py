"""Wake Decision: which (if any) reasoning tier handles a transition (RFC §8.1, §8.2).

Routing order:
  source invalid / coverage insufficient -> SOURCE_BLOCKED
  source valid, no change                -> NOOP (no model call, K-03)
  rule-handled change                    -> TEMPLATE_ACTION
  semantic classification needed         -> JUDGE (if a model and egress are allowed)
  otherwise                              -> HUMAN_REVIEW
"""
from dataclasses import dataclass, field
from typing import Any

from ..domain.enums import Route
from ..ports.reasoning import DecisionProposal
from .rules import CRITICAL, INFORMATIONAL, PROTECTED, classify_change

MODEL_ROUTES = frozenset({Route.NOOP, Route.TEMPLATE_ACTION, Route.HUMAN_REVIEW, Route.PLANNER})


@dataclass(frozen=True)
class WakeDecision:
    route: Route
    reason_code: str
    template_changes: tuple[dict[str, Any], ...] = ()
    judge_changes: tuple[dict[str, Any], ...] = ()
    uncertainty_flags: tuple[str, ...] = ()
    protected: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def annotate(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**c, "kind": classify_change(c)} for c in changes]


def route(
    *,
    source_ok: bool,
    coverage_ok: bool,
    blocked_reason: str,
    changes: list[dict[str, Any]],
    baseline: bool,
    model_available: bool,
    egress_allowed: bool,
) -> WakeDecision:
    if not source_ok:
        return WakeDecision(Route.SOURCE_BLOCKED, blocked_reason)
    if not coverage_ok:
        return WakeDecision(Route.SOURCE_BLOCKED, "coverage_insufficient")
    if baseline:
        return WakeDecision(Route.NOOP, "baseline_established")
    if not changes:
        return WakeDecision(Route.NOOP, "no_change")
    critical = tuple(c for c in changes if c["kind"] in CRITICAL)
    rest = tuple(c for c in changes if c["kind"] not in CRITICAL and c["kind"] not in INFORMATIONAL)
    protected = any(c["kind"] in PROTECTED for c in rest)
    if critical:
        flags = ("ambiguous_changes_pending",) if rest else ()
        return WakeDecision(Route.TEMPLATE_ACTION, "grade_rule", critical, rest, flags, protected)
    if not rest:
        return WakeDecision(Route.NOOP, "informational_only")
    if model_available and egress_allowed:
        return WakeDecision(Route.JUDGE, "semantic_classification", (), rest, (), protected)
    flag = "model_unavailable" if not model_available else "egress_not_allowed"
    return WakeDecision(Route.HUMAN_REVIEW, "needs_review", (), rest, (flag,), protected)


def admit_model_decision(rule: WakeDecision, proposal: DecisionProposal) -> WakeDecision:
    """Admit a JUDGE proposal. The model can raise attention but never lower a rule floor,
    and anything it says about capabilities is recorded, not granted (K-02)."""
    flags = list(proposal.uncertainty_flags)
    chosen = proposal.route
    if chosen not in MODEL_ROUTES:
        flags.append("invalid_model_route")
        chosen = Route.HUMAN_REVIEW
    if rule.protected and chosen is Route.NOOP:
        flags.append("rule_protected_floor")
        chosen = Route.HUMAN_REVIEW
    return WakeDecision(
        route=chosen,
        reason_code=proposal.reason_code[:120] or "model_decision",
        template_changes=rule.template_changes,
        judge_changes=rule.judge_changes,
        uncertainty_flags=tuple(flags),
        protected=rule.protected,
        extra={"suggested_capabilities": list(proposal.suggested_capabilities), "importance": proposal.importance},
    )
