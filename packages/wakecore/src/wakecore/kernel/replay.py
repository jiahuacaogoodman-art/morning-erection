"""Deterministic replay and diagnostics (RFC §14, T22).

Replay re-runs the pure reducer over recorded observations (evidence content + the
policy snapshot and checkpoint basis saved with each observation step), and re-admits
recorded model outputs through the same admission rule. It reads through a repository
that refuses every write and it never receives tools, so historical side effects cannot
be re-executed: doing that again would have to be a new, explicit control command.
"""
from dataclasses import dataclass, field
from typing import Any, Optional

from .decision.profiles import PROFILES
from .decision.wake import WakeDecision, admit_model_decision
from .domain.enums import Completeness, ObservationOutcome, Route, StepKind, StepStatus
from .domain.errors import NotFound, PolicyDenied
from .domain.model import AuditEntry, DecisionRecord, Evidence, ModelCall, ObservationRecord, Step, TaskRuntime
from .events.reducer import REDUCER_VERSION, CheckpointState, PolicySnapshot, reduce
from .ports.observation import ObservationResult
from .ports.reasoning import DecisionProposal
from .ports.store import StateStore
from .repo import Repo

OBSERVATION_KINDS = (StepKind.PROBE, StepKind.PROCESS_DELIVERY, StepKind.WAIT_RESUME)


class ReadOnlyRepo(Repo):
    """Replay has no write authority, including over the kernel's own tables."""

    def _deny(self, *_: Any, **__: Any) -> Any:
        raise PolicyDenied("replay is read-only")

    insert = insert_if_absent = change = try_change = delete = _deny  # type: ignore[assignment]


@dataclass
class ReplayItem:
    step_id: str
    kind: str
    recorded: dict[str, Any]
    replayed: dict[str, Any]
    match: bool
    would_propose: list[dict[str, Any]] = field(default_factory=list)
    note: Optional[str] = None


@dataclass
class ReplayReport:
    tenant_id: str
    task_id: str
    reducer_version: str
    items: list[ReplayItem]
    skipped: list[dict[str, Any]]
    external_writes: int = 0  # structurally always zero: replay has no tools

    @property
    def matches(self) -> int:
        return sum(1 for i in self.items if i.match)

    @property
    def mismatches(self) -> list[ReplayItem]:
        return [i for i in self.items if not i.match]

    def summary(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "replayed": len(self.items), "matches": self.matches,
                "mismatches": [i.step_id for i in self.mismatches], "skipped": len(self.skipped),
                "external_writes": self.external_writes, "reducer_version": self.reducer_version}


def replay_task(store: StateStore, tenant_id: str, task_id: str) -> ReplayReport:
    with store.transaction() as uow:
        repo = ReadOnlyRepo(uow)
        rt = repo.get(TaskRuntime, tenant_id=tenant_id, task_id=task_id)
        if rt is None:
            raise NotFound(f"task {task_id}")
        return _replay(repo, rt)


def _commit_order(repo: Repo, tenant_id: str, task_id: str) -> list[str]:
    """Steps in the order their results were committed (audit seq is commit-ordered)."""
    seen, order = set(), []
    for au in repo.find(AuditEntry, {"tenant_id": tenant_id, "task_id": task_id, "kind": "step.succeeded"},
                        order_by=("seq",)):
        if au.subject_id not in seen:
            seen.add(au.subject_id)
            order.append(au.subject_id)
    return order


def _replay(repo: Repo, rt: TaskRuntime) -> ReplayReport:
    items: list[ReplayItem] = []
    skipped: list[dict[str, Any]] = []
    # Replayed checkpoint per scope: starts empty and is rebuilt only from recorded evidence.
    state: dict[str, tuple[CheckpointState, Optional[str]]] = {}
    for step_id in _commit_order(repo, rt.tenant_id, rt.task_id):
        step = repo.get(Step, tenant_id=rt.tenant_id, step_id=step_id)
        if step is None or step.status is not StepStatus.SUCCEEDED or not step.result:
            continue
        if step.kind in OBSERVATION_KINDS:
            if "replay" not in step.result:
                skipped.append({"step_id": step_id, "reason": step.result.get("skipped", "no_replay_inputs")})
                continue
            items.append(_replay_observation(repo, step, state))
        elif step.kind is StepKind.JUDGE and step.result.get("model_call_id"):
            items.append(_replay_judge(repo, step))
        else:
            skipped.append({"step_id": step_id, "reason": f"{step.kind}:{step.result.get('fallback', 'not_replayed')}"})
    return ReplayReport(tenant_id=rt.tenant_id, task_id=rt.task_id, reducer_version=REDUCER_VERSION, items=items,
                        skipped=skipped)


def _policy(data: dict[str, Any]) -> PolicySnapshot:
    return PolicySnapshot(**{**data, "target_resources": tuple(data.get("target_resources", ())),
                             "ignore_fields": tuple(data.get("ignore_fields", ())),
                             "decision_params": dict(data.get("decision_params") or {})})


def _observation(o: ObservationRecord, ev: Optional[Evidence]) -> ObservationResult:
    content = ev.content if ev is not None else {}
    return ObservationResult(
        outcome=ObservationOutcome(o.outcome), completeness=Completeness(o.completeness), scope=o.scope,
        records=content.get("records", {}) if isinstance(content.get("records"), dict) else {},
        observed_at=o.observed_at, source_revision=o.source_revision, coverage_gaps=tuple(o.coverage_gaps),
        history_coverage=o.history_coverage, error_code=o.error_code)


def _replay_observation(repo: Repo, step: Step, state: dict[str, tuple[CheckpointState, Optional[str]]]) -> ReplayItem:
    rec = step.result
    inputs = rec["replay"]
    policy = _policy(inputs["policy"])
    o = repo.get(ObservationRecord, tenant_id=step.tenant_id, observation_id=rec["observation_id"])
    ev = repo.get(Evidence, tenant_id=step.tenant_id, evidence_ref=o.evidence_ref) if o and o.evidence_ref else None
    dec = repo.get(DecisionRecord, tenant_id=step.tenant_id, decision_id=rec["decision_id"])
    cp, trusted_obs = state.get(policy.scope_key, (CheckpointState(0, None), None))
    basis = inputs.get("basis", {})
    note = None
    if inputs.get("reducer_version") != REDUCER_VERSION:
        note = f"recorded with {inputs.get('reducer_version')}, replayed with {REDUCER_VERSION}"
    if basis.get("local_revision") != cp.local_revision or basis.get("trusted_observation_id") != trusted_obs:
        note = (note + "; " if note else "") + "checkpoint basis differs from the replayed chain"

    t = reduce(cp, _observation(o, ev), policy)
    if t.trusted:
        state[policy.scope_key] = (CheckpointState(t.revision, t.snapshot), o.observation_id)
    recorded = {"route": str(dec.route) if dec else rec.get("route"),
                "reason_code": dec.reason_code if dec else rec.get("reason_code"),
                "revision": rec.get("revision"), "trusted": rec.get("trusted"), "health": rec.get("health"),
                "goal_satisfied": rec.get("goal_satisfied")}
    replayed = {"route": str(t.decision.route), "reason_code": t.decision.reason_code, "revision": t.revision,
                "trusted": t.trusted, "health": str(t.health), "goal_satisfied": t.goal_satisfied}
    would = []
    profile = PROFILES.get(policy.decision_profile)
    if profile and t.decision.route is Route.TEMPLATE_ACTION and t.decision.template_changes:
        logical_action, payload = profile.template(t.decision.template_changes, policy.decision_params)
        would.append({"logical_action": logical_action, "payload": payload})
    if t.decision.route is Route.PLANNER and t.decision.template_changes:
        would.append({"logical_action": "plan", "changes": len(t.decision.template_changes)})
    if t.decision.judge_changes:
        would.append({"logical_action": "judge_or_review", "changes": len(t.decision.judge_changes)})
    return ReplayItem(step_id=step.step_id, kind=str(step.kind), recorded=recorded, replayed=replayed,
                      match=recorded == replayed, would_propose=would, note=note)


def _replay_judge(repo: Repo, step: Step) -> ReplayItem:
    """Execution diagnosis with the *recorded* model output; no model is called."""
    mc = repo.get(ModelCall, tenant_id=step.tenant_id, call_id=step.result["model_call_id"])
    dec = repo.get(DecisionRecord, tenant_id=step.tenant_id, decision_id=step.result["decision_id"])
    out = (mc.output if mc else None) or {}
    proposal = DecisionProposal(
        route=Route(out["route"]) if out.get("route") in {r.value for r in Route} else Route.HUMAN_REVIEW,
        reason_code=out.get("reason_code", ""), supporting_evidence_refs=tuple(out.get("supporting_evidence_refs", ())),
        uncertainty_flags=tuple(out.get("uncertainty_flags", ())),
        suggested_capabilities=tuple(out.get("suggested_capabilities", ())), importance=out.get("importance", "normal"))
    rule = WakeDecision(route=Route.JUDGE, reason_code=step.input.get("reason_code", "semantic_classification"),
                        judge_changes=tuple(step.input.get("changes", [])), protected=bool(step.input.get("protected")))
    d = admit_model_decision(rule, proposal)
    recorded = {"route": str(dec.route) if dec else None, "reason_code": dec.reason_code if dec else None,
                "uncertainty_flags": list(dec.uncertainty_flags) if dec else None}
    replayed = {"route": str(d.route), "reason_code": d.reason_code, "uncertainty_flags": list(d.uncertainty_flags)}
    return ReplayItem(step_id=step.step_id, kind=str(step.kind), recorded=recorded, replayed=replayed,
                      match=recorded == replayed, note="recorded model output re-admitted; model not called")
