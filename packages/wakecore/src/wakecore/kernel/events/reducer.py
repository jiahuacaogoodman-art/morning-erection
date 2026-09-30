"""Pure reducer: (checkpoint, observation, policy) -> Transition (RFC §3.2, §7).

No I/O, no clock reads, no randomness: the same recorded inputs always give the same
transition, which is what makes deterministic replay (RFC §14) possible.
"""
from dataclasses import dataclass, field
from typing import Any, Optional

from ..decision.profiles import DEFAULT_PROFILE, PROFILES
from ..decision.wake import WakeDecision, route
from ..domain.enums import Completeness, ObservationOutcome, SourceHealth
from ..ports.observation import ObservationResult
from .change_detection import diff, merge_declared, normalise

REDUCER_VERSION = "reducer-v1"

_HEALTH = {
    ObservationOutcome.AUTH_REQUIRED: SourceHealth.AUTH_REQUIRED,
    ObservationOutcome.UNAVAILABLE: SourceHealth.UNAVAILABLE,
    ObservationOutcome.SCHEMA_INVALID: SourceHealth.SCHEMA_INVALID,
}


@dataclass(frozen=True)
class CheckpointState:
    local_revision: int
    trusted_snapshot: Optional[dict[str, dict[str, Any]]]


@dataclass(frozen=True)
class PolicySnapshot:
    tenant_id: str
    task_id: str
    source_ref: str
    scope_key: str
    target_key: str
    target_resources: tuple[str, ...]
    completeness_required: str
    first_snapshot: str
    ignore_fields: tuple[str, ...]
    completion_evaluator: str
    completion_evaluator_version: int
    model_available: bool
    model_egress_allowed: bool
    # V0.3: recorded with every observation, so replay uses the profile that was in force.
    decision_profile: str = DEFAULT_PROFILE
    decision_params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Transition:
    health: SourceHealth
    health_reason: Optional[str]
    trusted: bool
    snapshot: Optional[dict[str, dict[str, Any]]]
    revision: int
    changes: tuple[dict[str, Any], ...]
    transition_key: Optional[str]
    decision: WakeDecision
    goal_satisfied: bool
    facts: tuple[str, ...] = field(default=())


def _blocked(state: CheckpointState, health: SourceHealth, reason: str, source_ok: bool) -> Transition:
    decision = route(source_ok=source_ok, coverage_ok=False, blocked_reason=reason, changes=[], baseline=False,
                     model_available=False, egress_allowed=False)
    return Transition(health=health, health_reason=reason, trusted=False, snapshot=None,
                      revision=state.local_revision, changes=(), transition_key=None, decision=decision,
                      goal_satisfied=False, facts=("trusted_snapshot_kept",))


def reduce(state: CheckpointState, obs: ObservationResult, policy: PolicySnapshot) -> Transition:
    if obs.outcome not in (ObservationOutcome.SUCCESS, ObservationOutcome.PARTIAL):
        return _blocked(state, _HEALTH[obs.outcome], obs.error_code or str(obs.outcome), source_ok=False)

    scope_list = obs.scope.get(policy.target_key)
    read = set(scope_list) if isinstance(scope_list, list) else set(obs.records)
    targets = set(policy.target_resources)
    full = (obs.outcome is ObservationOutcome.SUCCESS and obs.completeness is Completeness.COMPLETE
            and targets <= read)
    health = SourceHealth.HEALTHY if full else SourceHealth.DEGRADED

    if policy.completeness_required == "full_target_scope" and not full:
        return _blocked(state, SourceHealth.DEGRADED, "coverage_insufficient", source_ok=True)
    if targets and not (read & targets):
        return _blocked(state, SourceHealth.DEGRADED, "coverage_insufficient", source_ok=True)

    old = state.trusted_snapshot or {}
    first = state.trusted_snapshot is None
    fresh = normalise(obs.records, policy.ignore_fields)
    if full:
        merged = {k: v for k, v in fresh.items() if k in read}
        keys = None
    else:
        merged = merge_declared(old, fresh, read)
        keys = read

    profile = PROFILES.get(policy.decision_profile)
    if profile is None:  # unknown profile: never act on it, a human must look
        return _blocked(state, SourceHealth.DEGRADED, "unknown_decision_profile", source_ok=True)
    params = policy.decision_params
    changes = profile.annotate(diff(old, merged, keys), params)
    baseline = first and policy.first_snapshot == "baseline_only"
    moved = bool(changes) or first
    revision = state.local_revision + 1 if moved else state.local_revision
    transition_key = f"{policy.tenant_id}:{policy.source_ref}:{policy.scope_key}:{revision}" if moved else None

    decision = profile.route(params, source_ok=True, coverage_ok=True, blocked_reason="",
                             changes=[] if baseline else changes, baseline=baseline,
                             model_available=policy.model_available, egress_allowed=policy.model_egress_allowed)

    evaluator = profile.evaluators.get((policy.completion_evaluator, policy.completion_evaluator_version))
    goal = bool(evaluator and evaluator(merged, policy.target_resources, params))
    facts = ["partial_merge"] if not full else []
    if evaluator is None:
        facts.append("unknown_completion_evaluator")
    if baseline:
        facts.append("baseline_only")
    return Transition(
        health=health,
        health_reason=None if full else "partial_snapshot",
        trusted=True,
        snapshot=merged,
        revision=revision,
        changes=tuple(changes),
        transition_key=transition_key,
        decision=decision,
        goal_satisfied=goal,
        facts=tuple(facts),
    )
