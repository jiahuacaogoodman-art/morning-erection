"""Step executor (RFC §11.3 T2–T4, §12.3, §8).

Every step follows the same shape:

    short read transaction  ->  external call with NO transaction open  ->  commit transaction

The commit transaction re-locks the task aggregate (root first), re-verifies owner +
lease_epoch + slot (§12.3) and re-checks the lifecycle before anything is written. A
worker whose lease was taken over can therefore never overwrite a newer result (T07),
and a task cancelled while the connector or model was running writes nothing but a
CANCELLED step (T10).
"""
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from .. import audit
from ..actions.intents import ProposedIntent, propose
from ..budget.ledger import AccountSpec, mark_awaiting_reconciliation, period_key, release, reserve, settle
from ..commands.tasks import load_spec, scope_key_for
from ..context import KernelContext
from ..decision.profiles import get_profile
from ..decision.templates import budget_deferred_notification, review_notification
from ..decision.wake import WakeDecision, admit_model_decision
from ..domain.canonical import digest, short_hash
from ..domain.enums import (
    TERMINAL_LIFECYCLES,
    Completeness,
    DeliveryStatus,
    ModelCallStatus,
    ObservationOutcome,
    Route,
    StepKind,
    StepStatus,
    TaskLifecycle,
    WaitReason,
)
from ..domain.errors import BudgetDeferred, KernelError, StaleLease
from ..domain.model import (
    DecisionRecord,
    Delivery,
    EventRecord,
    Evidence,
    ModelCall,
    ObservationRecord,
    SourceBinding,
    SourceCheckpoint,
    Step,
    TaskRuntime,
    TriggerRecord,
)
from ..domain.taskspec import _jsonable
from ..events.reducer import REDUCER_VERSION, CheckpointState, PolicySnapshot, reduce
from ..lifecycle import PAUSED_UNTIL, enter_draining, maybe_complete, terminate
from ..locks import lock_task, tx
from ..ports.faults import SimulatedCrash
from ..ports.observation import ObservationRequest, ObservationResult
from ..ports.reasoning import ContextBundle, DecisionProposal, ModelTimeout, PlanProposal
from ..repo import Repo
from ..scheduling.followup import admit_followup
from ..scheduling.waits import register_wait
from .leases import claim_next_step, release_slot, verify_for_commit
from .steps import add_step, set_step, settle_run_after_step

TRANSITION_EVENT_TYPE = "wakecore.source.transition"
INTERNAL_SOURCE_URI = "wakecore://kernel/transitions"


# --------------------------------------------------------------------------- entry points

def run_next(ctx: KernelContext, worker_id: str) -> Optional[dict[str, Any]]:
    """Claim and execute one step. Returns None when nothing is runnable."""
    step = claim_next_step(ctx, worker_id)
    if step is None:
        return None
    return execute(ctx, step)


def execute(ctx: KernelContext, step: Step) -> dict[str, Any]:
    handler = _HANDLERS[step.kind]
    try:
        return handler(ctx, step)
    except StaleLease:
        return {"step_id": step.step_id, "outcome": "stale_lease"}  # a newer owner decides
    except SimulatedCrash:
        raise
    except Exception as exc:  # noqa: BLE001 - every other failure is retried or recorded
        return _retry_or_fail(ctx, step, exc)


# --------------------------------------------------------------------------- shared helpers

def backoff_seconds(ctx: KernelContext, step_id: str, attempts: int) -> int:
    base = ctx.config.retry_base_seconds
    delay = min(ctx.config.retry_max_seconds, base * (2 ** max(0, attempts - 1)))
    jitter = int(short_hash(step_id, str(attempts), length=8), 16) % max(1, base)  # deterministic
    return min(ctx.config.retry_max_seconds, delay + jitter)


def _retry_or_fail(ctx: KernelContext, step: Step, exc: Exception) -> dict[str, Any]:
    error = f"{type(exc).__name__}: {exc}"[:500]
    try:
        with tx(ctx.store) as repo:
            lock_task(repo, step.tenant_id, step.task_id)
            cur = verify_for_commit(repo, step)
            return requeue_or_fail(repo, ctx, cur, error=error, actor="executor")
    except StaleLease:
        return {"step_id": step.step_id, "outcome": "stale_lease"}


def requeue_or_fail(repo: Repo, ctx: KernelContext, step: Step, *, error: str, actor: str) -> dict[str, Any]:
    """RUNNING step whose attempt did not commit: back to READY with backoff, or FAILED."""
    now = repo.uow.db_now()
    if step.attempts >= step.max_attempts:
        set_step(repo, step, StepStatus.FAILED, last_error=error, lease_until=None)
        if step.kind is StepKind.PROCESS_DELIVERY:
            _set_delivery(repo, step, DeliveryStatus.FAILED, reason="max_attempts")
        outcome = "failed"
    else:
        delay = backoff_seconds(ctx, step.step_id, step.attempts)
        set_step(repo, step, StepStatus.READY, last_error=error, lease_owner=None, lease_until=None,
                 available_at=now + timedelta(seconds=delay))
        outcome = "retry_scheduled"
    release_slot(repo, step)
    settle_run_after_step(repo, step.run_id, step.tenant_id)
    audit.record(repo, ctx.ids, tenant_id=step.tenant_id, kind=f"step.{outcome}", actor=actor, subject_type="step",
                 subject_id=step.step_id, task_id=step.task_id, from_state="RUNNING", reason=error[:200],
                 refs={"attempts": step.attempts, "lease_epoch": step.lease_epoch})
    return {"step_id": step.step_id, "outcome": outcome, "error": error}


def _set_delivery(repo: Repo, step: Step, status: DeliveryStatus, *, reason: Optional[str] = None,
                  decision_ref: Optional[str] = None) -> None:
    event_id = step.input.get("event_id")
    if not event_id:
        return
    d = repo.get(Delivery, for_update=True, tenant_id=step.tenant_id, event_id=event_id, task_id=step.task_id)
    if d is not None and d.status is DeliveryStatus.PENDING:
        repo.change(d, status=status, reason=reason, decision_ref=decision_ref, run_id=step.run_id,
                    updated_at=repo.uow.db_now())


def _finish(repo: Repo, ctx: KernelContext, step: Step, to: StepStatus, *, result: dict[str, Any],
            reason: str) -> dict[str, Any]:
    set_step(repo, step, to, result=result, lease_until=None, last_error=None if to is StepStatus.SUCCEEDED
             else reason)
    release_slot(repo, step)
    settle_run_after_step(repo, step.run_id, step.tenant_id)
    audit.record(repo, ctx.ids, tenant_id=step.tenant_id, kind=f"step.{str(to).lower()}", actor="executor",
                 subject_type="step", subject_id=step.step_id, task_id=step.task_id, from_state="RUNNING",
                 to_state=to, reason=reason, refs={"kind": str(step.kind), "lease_epoch": step.lease_epoch,
                                                   **{k: v for k, v in result.items() if k not in ("changes", "replay")}})
    return {"step_id": step.step_id, "outcome": str(to).lower(), "reason": reason, **result}


def _gate(repo: Repo, ctx: KernelContext, root: TaskRuntime, rt: TaskRuntime, step: Step) -> Optional[dict[str, Any]]:
    """Lifecycle re-check inside the commit transaction. Returns a result if the step must stop here."""
    now = repo.uow.db_now()
    if rt.lifecycle in (TaskLifecycle.ACTIVE, TaskLifecycle.PAUSED, TaskLifecycle.DRAINING) and rt.expires_at <= now:
        rt, _ = terminate(repo, ctx.ids, rt, TaskLifecycle.EXPIRED, actor="kernel", reason="expired")
    if rt.lifecycle in TERMINAL_LIFECYCLES or root.lifecycle in TERMINAL_LIFECYCLES:
        if step.kind is StepKind.PROCESS_DELIVERY:
            _set_delivery(repo, step, DeliveryStatus.SKIPPED, reason=f"task_{str(rt.lifecycle).lower()}")
        return _finish(repo, ctx, step, StepStatus.CANCELLED, result={}, reason=f"task_{str(rt.lifecycle).lower()}")
    paused = rt.lifecycle is TaskLifecycle.PAUSED or root.lifecycle is TaskLifecycle.PAUSED
    if paused:
        if step.kind is StepKind.PROBE:  # a fresh probe is scheduled on resume
            return _finish(repo, ctx, step, StepStatus.CANCELLED, result={}, reason="task_paused")
        set_step(repo, step, StepStatus.READY, lease_owner=None, lease_until=None, available_at=PAUSED_UNTIL,
                 attempts=max(0, step.attempts - 1))  # parking is not a failed attempt
        release_slot(repo, step)
        settle_run_after_step(repo, step.run_id, step.tenant_id)
        return {"step_id": step.step_id, "outcome": "parked", "reason": "task_paused"}
    if rt.lifecycle is TaskLifecycle.DRAINING and step.kind in (StepKind.PROBE, StepKind.PROCESS_DELIVERY,
                                                               StepKind.WAIT_RESUME, StepKind.PLAN):
        if step.kind is StepKind.PROCESS_DELIVERY:
            _set_delivery(repo, step, DeliveryStatus.SKIPPED, reason="task_draining")
        return _finish(repo, ctx, step, StepStatus.SUCCEEDED, result={"skipped": "task_draining"},
                       reason="task_draining")
    if step.kind is StepKind.PROBE and step.input.get("trigger_id"):
        trg = repo.get(TriggerRecord, tenant_id=step.tenant_id, trigger_id=step.input["trigger_id"])
        if trg is None or trg.generation != step.input.get("generation"):
            return _finish(repo, ctx, step, StepStatus.CANCELLED, result={}, reason="stale_trigger_generation")
    return None


def _notify(repo: Repo, ctx: KernelContext, rt: TaskRuntime, step: Step, *, logical_action: str,
            payload: dict[str, Any], reason_code: str, causal_event_id: Optional[str]) -> Optional[str]:
    action = propose(repo, ctx, rt, run_id=step.run_id, causal_event_id=causal_event_id, actor="kernel",
                     intent=ProposedIntent(logical_action=logical_action, tool_id=ctx.config.notify_tool_id,
                                           capability=ctx.config.notify_capability, payload=payload,
                                           reason_code=reason_code, preconditions={}))
    return action.action_id if action else None


def _egress_allowed(ctx: KernelContext, rt: TaskRuntime) -> bool:
    return ctx.reasoning is not None and \
        ctx.reasoning.data_egress_target in rt.effective_authority.get("data_egress", [])


# --------------------------------------------------------------------------- observation steps (T2 + T4)

def _observe(ctx: KernelContext, step: Step) -> dict[str, Any]:
    with tx(ctx.store) as repo:  # read phase: no locks held across the connector call
        rt = repo.get(TaskRuntime, tenant_id=step.tenant_id, task_id=step.task_id)
        spec = load_spec(repo, rt)
        binding = repo.get(SourceBinding, tenant_id=step.tenant_id, source_ref=spec.source.binding_ref)
        cp = repo.get(SourceCheckpoint, tenant_id=step.tenant_id, source_ref=binding.source_ref,
                      scope_key=scope_key_for(spec))
        runnable = rt.lifecycle is TaskLifecycle.ACTIVE and rt.expires_at > repo.uow.db_now()

    obs: Optional[ObservationResult] = None
    if runnable:
        connector = ctx.connectors.get(binding.connector_id)
        secret = ctx.secrets.resolve(step.tenant_id, binding.secret_ref) if binding.secret_ref else None
        request = ObservationRequest(tenant_id=step.tenant_id, source_ref=binding.source_ref,
                                     resource_scope=spec.source.resource_scope, secret=secret,
                                     cursor=cp.cursor if cp else None, requested_at=ctx.clock.utc_now())
        obs = _fetch(connector, request, ctx.clock.utc_now())
    ctx.faults.check("after_fetch")

    with tx(ctx.store) as repo:
        root, rt = lock_task(repo, step.tenant_id, step.task_id)
        step = verify_for_commit(repo, step)
        stop = _gate(repo, ctx, root, rt, step)
        if stop is not None:
            return stop
        if obs is None:  # became runnable between phases (e.g. resumed): observe on the next attempt
            raise RuntimeError("observation skipped because the task was not runnable at read time")
        out = _commit_observation(repo, ctx, rt, step, obs)
        ctx.faults.check("before_commit_T4")
    ctx.faults.check("after_commit_T4")
    return out


def _fetch(connector: Any, request: ObservationRequest, now: datetime) -> ObservationResult:
    if connector is None:
        return ObservationResult(outcome=ObservationOutcome.UNAVAILABLE, completeness=Completeness.UNKNOWN,
                                 scope={}, records={}, observed_at=now, error_code="connector_not_installed")
    try:
        obs = connector.fetch(request)
    except Exception:  # noqa: BLE001 - a connector failure is a fact, not a crash (K-04)
        return ObservationResult(outcome=ObservationOutcome.UNAVAILABLE, completeness=Completeness.UNKNOWN,
                                 scope={}, records={}, observed_at=now, error_code="connector_exception")
    if not isinstance(obs, ObservationResult) or not isinstance(obs.records, dict) or \
            not all(isinstance(v, dict) for v in obs.records.values()):
        return ObservationResult(outcome=ObservationOutcome.SCHEMA_INVALID, completeness=Completeness.UNKNOWN,
                                 scope={}, records={}, observed_at=now, error_code="connector_contract_violation")
    return obs


def _commit_observation(repo: Repo, ctx: KernelContext, rt: TaskRuntime, step: Step,
                        obs: ObservationResult) -> dict[str, Any]:
    now = repo.uow.db_now()
    tenant = step.tenant_id
    spec = load_spec(repo, rt)
    binding = repo.get(SourceBinding, for_update=True, tenant_id=tenant, source_ref=spec.source.binding_ref)
    scope_key = scope_key_for(spec)
    cp = repo.get(SourceCheckpoint, for_update=True, tenant_id=tenant, source_ref=binding.source_ref,
                  scope_key=scope_key)
    if cp is None:
        repo.insert_if_absent(SourceCheckpoint(
            tenant_id=tenant, source_ref=binding.source_ref, scope_key=scope_key, task_id=rt.task_id,
            local_revision=0, trusted_revision=None, trusted_snapshot=None, trusted_observation_id=None,
            trusted_at=None, cursor=None, version=0, updated_at=now))
        cp = repo.get(SourceCheckpoint, for_update=True, tenant_id=tenant, source_ref=binding.source_ref,
                      scope_key=scope_key)

    usable = obs.outcome in (ObservationOutcome.SUCCESS, ObservationOutcome.PARTIAL)
    content = {"records": obs.records, "scope": obs.scope, "raw_excerpt": obs.raw_excerpt} if usable else \
        {"error_code": obs.error_code, "raw_excerpt": obs.raw_excerpt}
    content_digest = digest(content)
    evidence_ref = "evd_" + short_hash(tenant, binding.source_ref, content_digest)
    repo.insert_if_absent(Evidence(tenant_id=tenant, evidence_ref=evidence_ref, source_ref=binding.source_ref,
                                   kind="observation", digest=content_digest, content=content,
                                   visibility="owner", created_at=now))
    observation_id = "obs_" + short_hash(tenant, step.step_id, step.current_attempt_id or "")
    repo.insert(ObservationRecord(
        tenant_id=tenant, observation_id=observation_id, source_ref=binding.source_ref, task_id=rt.task_id,
        run_id=step.run_id, scope=obs.scope, outcome=obs.outcome, completeness=obs.completeness,
        source_revision=obs.source_revision, observed_at=obs.observed_at,
        payload_digest=digest(obs.records) if usable else None, evidence_ref=evidence_ref,
        coverage_gaps=list(obs.coverage_gaps), history_coverage=obs.history_coverage, error_code=obs.error_code,
        connector_version=_connector_version(ctx, binding)))

    goal_possible = rt.lifecycle is TaskLifecycle.ACTIVE
    policy = policy_snapshot(ctx, rt, spec, binding.source_ref, scope_key)
    basis = {"local_revision": cp.local_revision, "trusted_observation_id": cp.trusted_observation_id}
    t = reduce(CheckpointState(cp.local_revision, cp.trusted_snapshot), obs, policy)

    repo.change(binding, health=t.health, health_reason=t.health_reason, last_attempt_at=now,
                last_trusted_at=now if t.trusted else binding.last_trusted_at, updated_at=now)
    if t.trusted:
        repo.change(cp, expect={"version": cp.version}, local_revision=t.revision, trusted_snapshot=t.snapshot,
                    trusted_revision=obs.source_revision, trusted_observation_id=observation_id, trusted_at=now,
                    cursor=obs.cursor or cp.cursor, version=cp.version + 1, updated_at=now)

    causal_in = step.input.get("event_id")
    transition_event_id = None
    if t.transition_key:
        transition_event_id = "evt_" + short_hash(tenant, "internal", t.transition_key)
        repo.insert_if_absent(EventRecord(
            tenant_id=tenant, event_id=transition_event_id, seq=repo.uow.next_seq("events"),
            source_ref=binding.source_ref, source_uri=INTERNAL_SOURCE_URI,
            source_event_id=f"rev:{scope_key}:{t.revision}", specversion="1.0", type=TRANSITION_EVENT_TYPE,
            subject=scope_key, occurred_at=obs.observed_at, received_at=now,
            payload_digest=digest({"changes": list(t.changes), "revision": t.revision}),
            data={"transition_key": t.transition_key, "revision": t.revision, "changes": list(t.changes),
                  "facts": list(t.facts), "task_id": rt.task_id},
            verified=True, internal=True, ingress_principal="kernel", causation_id=causal_in,
            evidence_ref=evidence_ref))

    decision_id = "dec_" + short_hash(tenant, step.step_id)
    d = t.decision
    repo.insert(DecisionRecord(
        tenant_id=tenant, decision_id=decision_id, task_id=rt.task_id, run_id=step.run_id,
        event_id=transition_event_id or causal_in, route=d.route, reason_code=d.reason_code,
        evidence_refs=[evidence_ref], uncertainty_flags=list(d.uncertainty_flags), suggested_capabilities=[],
        model_call_ref=None, policy_version=ctx.config.policy_version, created_at=now))

    actions: list[str] = []
    followups: list[str] = []
    causal = transition_event_id
    if d.route is Route.TEMPLATE_ACTION and d.template_changes:
        logical_action, payload = get_profile(policy.decision_profile).template(d.template_changes,
                                                                                policy.decision_params)
        aid = _notify(repo, ctx, rt, step, logical_action=logical_action, payload=payload,
                      reason_code=d.reason_code, causal_event_id=causal)
        actions += [aid] if aid else []
    if d.route is Route.PLANNER and d.template_changes and goal_possible and not t.goal_satisfied:
        # A deterministic rule already decided this needs acting on: go straight to planning.
        plan = add_step(repo, _run(repo, step), kind=StepKind.PLAN, logical_step_id=f"plan:{causal}",
                        input={"event_id": causal, "changes": list(d.template_changes), "evidence_ref": evidence_ref,
                               "reason_code": d.reason_code},
                        max_attempts=ctx.config.max_step_attempts, max_steps=spec.limits.max_steps_per_run)
        if plan is None:
            aid = _notify(repo, ctx, rt, step, logical_action="notify.review",
                          payload=review_notification(d.template_changes, "step_limit"), reason_code="step_limit",
                          causal_event_id=causal)
            actions += [aid] if aid else []
        else:
            followups.append(plan.step_id)
    pending = d.judge_changes if d.route in (Route.TEMPLATE_ACTION, Route.JUDGE, Route.HUMAN_REVIEW,
                                             Route.PLANNER) else ()
    if pending:
        use_model = (d.route is not Route.HUMAN_REVIEW and not (t.goal_satisfied and goal_possible)
                     and policy.model_available and policy.model_egress_allowed)
        judge = None
        if use_model:
            judge = add_step(repo, _run(repo, step), kind=StepKind.JUDGE, logical_step_id=f"judge:{causal}",
                             input={"event_id": causal, "changes": list(pending), "evidence_ref": evidence_ref,
                                    "protected": d.protected, "reason_code": d.reason_code},
                             max_attempts=ctx.config.max_step_attempts, max_steps=spec.limits.max_steps_per_run)
            followups += [judge.step_id] if judge else []
        if judge is None:
            flags = [f for f in d.uncertainty_flags if f != "ambiguous_changes_pending"]
            reason = flags[0] if flags else ("step_limit" if use_model else d.reason_code)
            aid = _notify(repo, ctx, rt, step, logical_action="notify.review",
                          payload=review_notification(pending, reason), reason_code=reason, causal_event_id=causal)
            actions += [aid] if aid else []

    if t.goal_satisfied and goal_possible:
        rt = enter_draining(repo, ctx.ids, rt, reason="goal_satisfied")
        rt = maybe_complete(repo, ctx.ids, rt, notify_capability=ctx.config.notify_capability) or rt
    repo.change(rt, last_progress_at=now)

    if step.kind is StepKind.PROCESS_DELIVERY:
        _set_delivery(repo, step, DeliveryStatus.PROCESSED, decision_ref=decision_id)
    return _finish(repo, ctx, step, StepStatus.SUCCEEDED, reason=str(d.route), result={
        "observation_id": observation_id, "route": str(d.route), "reason_code": d.reason_code,
        "revision": t.revision, "trusted": t.trusted, "health": str(t.health), "decision_id": decision_id,
        "transition_event_id": transition_event_id, "actions": actions, "followup_steps": followups,
        "goal_satisfied": t.goal_satisfied, "lifecycle": str(rt.lifecycle),
        # Recorded inputs for deterministic replay (RFC §14).
        "replay": {"policy": _jsonable(asdict(policy)), "basis": basis, "reducer_version": REDUCER_VERSION,
                   "rules_version": ctx.config.rules_version}})


def policy_snapshot(ctx: KernelContext, rt: TaskRuntime, spec: Any, source_ref: str, scope_key: str) -> PolicySnapshot:
    return PolicySnapshot(
        tenant_id=rt.tenant_id, task_id=rt.task_id, source_ref=source_ref, scope_key=scope_key,
        target_key=spec.observation.target_key, target_resources=spec.target_resources,
        completeness_required=spec.observation.completeness_required,
        first_snapshot=spec.observation.first_snapshot, ignore_fields=spec.observation.ignore_fields,
        completion_evaluator=spec.completion.evaluator,
        completion_evaluator_version=spec.completion.evaluator_version,
        model_available=ctx.reasoning is not None, model_egress_allowed=_egress_allowed(ctx, rt),
        decision_profile=spec.decision_profile, decision_params=spec.decision_params)


def run_elapsed_seconds(repo: Repo, step: Step) -> float:
    """Active time of the step's run: since it was created, or since it last resumed from a
    wait (a run parked on a wait/budget is not busy, see scheduling.waits)."""
    run = _run(repo, step)
    since = run.created_at
    stamp = (run.checkpoint or {}).get("active_since")
    if stamp:
        since = datetime.fromisoformat(stamp)
    return (repo.uow.db_now() - since).total_seconds()


def _connector_version(ctx: KernelContext, binding: SourceBinding) -> str:
    c = ctx.connectors.get(binding.connector_id)
    return c.descriptor.version if c is not None else "unknown"


def _run(repo: Repo, step: Step):
    from ..domain.model import Run

    return repo.get(Run, tenant_id=step.tenant_id, run_id=step.run_id)


# --------------------------------------------------------------------------- model steps (JUDGE / PLAN)

def _model_step(ctx: KernelContext, step: Step, *, kind: str,
                call: Callable[[ContextBundle], Any],
                apply: Callable[[Repo, TaskRuntime, TaskRuntime, Step, Any, ModelCall], dict[str, Any]],
                fallback: Callable[[Repo, TaskRuntime, Step, str], dict[str, Any]]) -> dict[str, Any]:
    # --- reserve (short transaction): budget is held before any request leaves the kernel (§10)
    with tx(ctx.store) as repo:
        root, rt = lock_task(repo, step.tenant_id, step.task_id)
        step = verify_for_commit(repo, step)
        stop = _gate(repo, ctx, root, rt, step)
        if stop is not None:
            return stop
        if not _egress_allowed(ctx, rt):
            return fallback(repo, rt, step, "egress_not_allowed")
        spec = load_spec(repo, rt)
        if run_elapsed_seconds(repo, step) > spec.limits.max_run_seconds:
            # A run that has been busy too long does not get to think more: a human reviews
            # instead (the observation that led here is already committed).
            audit.record(repo, ctx.ids, tenant_id=step.tenant_id, kind="run.time_limit", actor="executor",
                         subject_type="run", subject_id=step.run_id, task_id=step.task_id,
                         reason="run_time_limit", refs={"max_run_seconds": spec.limits.max_run_seconds,
                                                        "step": step.logical_step_id})
            return fallback(repo, rt, step, "run_time_limit")
        root_spec = spec if root.task_id == rt.task_id else load_spec(repo, root)
        now = repo.uow.db_now()
        bundle = _bundle(ctx, rt, spec, step, now)
        owner = _owner(repo, rt)
        accounts = [
            AccountSpec("user", owner, "model_attempts", "attempt", ctx.config.user_model_attempts_per_day),
            AccountSpec("root_task", root.task_id, "model_attempts", "attempt",
                        root_spec.limits.max_model_attempts_per_day),
        ]
        period = period_key(now, root_spec.accounting_timezone)
        try:
            reservation_id = reserve(repo, ctx.ids, tenant_id=step.tenant_id, accounts=accounts, amount=1,
                                     root_task_id=root.task_id, attempt_id=step.current_attempt_id or step.step_id,
                                     period=period)
        except BudgetDeferred as exc:
            return _budget_deferred(repo, ctx, rt, step, exc, now, root_spec.accounting_timezone)
        call_row = ModelCall(
            tenant_id=step.tenant_id, call_id="mdl_" + short_hash(step.tenant_id, step.step_id,
                                                                  step.current_attempt_id or ""),
            run_id=step.run_id, step_id=step.step_id, logical_step_id=step.logical_step_id, task_id=rt.task_id,
            kind=kind, model_ref=ctx.reasoning.model_ref, prompt_version=bundle.prompt_version,
            input_digest=digest(_bundle_json(bundle)), status=ModelCallStatus.STARTED, output=None, usage=None,
            reservation_id=reservation_id, provider_request_id=None, error=None, created_at=now, finished_at=None)
        repo.insert(call_row)
    ctx.faults.check("after_model_reserve")

    # --- external call: no transaction is open
    outcome, value = "ok", None
    try:
        value = call(bundle)
    except ModelTimeout as exc:
        outcome, value = "unknown", f"ModelTimeout: {exc}"
    except Exception as exc:  # noqa: BLE001
        outcome, value = "failed", f"{type(exc).__name__}: {exc}"
    ctx.faults.check("after_model_call")

    # --- commit
    with tx(ctx.store) as repo:
        root, rt = lock_task(repo, step.tenant_id, step.task_id)
        mc = repo.get(ModelCall, for_update=True, tenant_id=call_row.tenant_id, call_id=call_row.call_id)
        now = repo.uow.db_now()
        try:
            step = verify_for_commit(repo, step)
        except StaleLease:
            # Our lease is gone, but the call happened: record what we know about its cost.
            _record_call(repo, mc, outcome, value, now)
            raise
        _record_call(repo, mc, outcome, value, now)
        stop = _gate(repo, ctx, root, rt, step)
        if stop is not None:
            return stop
        if outcome == "unknown":
            return fallback(repo, rt, step, "model_timeout")
        if outcome == "failed" or not _well_formed(kind, value):
            return fallback(repo, rt, step, "model_failed" if outcome == "failed" else "model_output_invalid")
        out = apply(repo, root, rt, step, value, mc)
        ctx.faults.check("before_commit_model")
    return out


def _record_call(repo: Repo, mc: ModelCall, outcome: str, value: Any, now: datetime) -> None:
    if mc is None or mc.status is not ModelCallStatus.STARTED:
        return  # recovery already classified it (e.g. UNKNOWN after lease expiry)
    if outcome == "ok":
        usage = asdict(value.usage) if hasattr(value, "usage") else {}
        repo.change(mc, status=ModelCallStatus.SUCCEEDED, output=_proposal_json(value), usage=usage,
                    provider_request_id=getattr(value, "provider_request_id", None), finished_at=now)
        settle(repo, tenant_id=mc.tenant_id, reservation_id=mc.reservation_id, actual=1)
    elif outcome == "unknown":
        # The provider may have billed the request: keep the reservation (K-10, T16).
        repo.change(mc, status=ModelCallStatus.UNKNOWN, error=str(value)[:500], finished_at=now)
        mark_awaiting_reconciliation(repo, tenant_id=mc.tenant_id, reservation_id=mc.reservation_id)
    else:
        repo.change(mc, status=ModelCallStatus.FAILED, error=str(value)[:500], finished_at=now)
        release(repo, tenant_id=mc.tenant_id, reservation_id=mc.reservation_id)


def _well_formed(kind: str, value: Any) -> bool:
    if kind == "judge":
        return isinstance(value, DecisionProposal) and isinstance(value.route, Route)
    return isinstance(value, PlanProposal)


def _proposal_json(value: Any) -> dict[str, Any]:
    return _jsonable(asdict(value))


def _bundle(ctx: KernelContext, rt: TaskRuntime, spec: Any, step: Step, now: datetime) -> ContextBundle:
    # Minimal, versioned context: changes + evidence refs, never secrets or whole histories.
    return ContextBundle(
        tenant_id=rt.tenant_id, task_id=rt.task_id, purpose=spec.purpose,
        event={"event_id": step.input.get("event_id"), "changes": step.input.get("changes", []),
               "reason_code": step.input.get("reason_code")},
        evidence=({"evidence_ref": step.input.get("evidence_ref")},) if step.input.get("evidence_ref") else (),
        progress={"lifecycle": str(rt.lifecycle), "step": step.logical_step_id},
        allowed_tools=allowed_tools(ctx, rt), remaining_budget={"model_attempts": 1},
        deadline=now + timedelta(seconds=ctx.config.lease_seconds))


def allowed_tools(ctx: KernelContext, rt: TaskRuntime) -> tuple[dict[str, Any], ...]:
    """What the planner may see: only installed tools this task is actually authorised to use
    (capability granted, the tool's own egress granted, system policy allows) — V0.3 rule 6.
    Connectors' read capabilities are not tools and never appear here."""
    authority = rt.effective_authority
    caps, egress = set(authority.get("capabilities", [])), set(authority.get("data_egress", []))
    sp = ctx.system_policy
    out = []
    for tool_id in sorted(ctx.tools):
        d = ctx.tools[tool_id].descriptor
        if d.capability_type not in caps or d.capability_type not in sp.allowed_capabilities:
            continue
        if not set(d.required_egress) <= egress & set(sp.allowed_data_egress):
            continue
        entry: dict[str, Any] = {"tool_id": d.tool_id, "capability": d.capability_type,
                                 "side_effect_class": str(d.side_effect_class), "input_schema": d.input_schema}
        if d.url_fields:
            entry["allowed_origins"] = list(authority.get("resource_scope", {}).get("origins", []))
        out.append(entry)
    return tuple(out)


def _bundle_json(bundle: ContextBundle) -> dict[str, Any]:
    return _jsonable(asdict(bundle))


def _owner(repo: Repo, rt: TaskRuntime) -> str:
    from ..commands.tasks import _owner as owner_of

    return owner_of(repo, rt)


def _budget_deferred(repo: Repo, ctx: KernelContext, rt: TaskRuntime, step: Step, exc: BudgetDeferred,
                     now: datetime, tz: str) -> dict[str, Any]:
    """No model call; park the step until the next budget period and tell the user (T15)."""
    retry_at = _next_period_start(now, tz)
    changes = step.input.get("changes", [])
    aid = _notify(repo, ctx, rt, step, logical_action="notify.budget_deferred",
                  payload=budget_deferred_notification(len(changes)), reason_code="budget_deferred",
                  causal_event_id=step.input.get("event_id"))
    register_wait(repo, ctx, step, reason=WaitReason.BUDGET, match={}, due_at=retry_at,
                  result={"budget": exc.details})
    release_slot(repo, step)
    settle_run_after_step(repo, step.run_id, step.tenant_id)
    audit.record(repo, ctx.ids, tenant_id=step.tenant_id, kind="step.budget_deferred", actor="executor",
                 subject_type="step", subject_id=step.step_id, task_id=step.task_id, from_state="RUNNING",
                 to_state="WAITING", reason="budget_deferred", refs={**exc.details, "retry_at": retry_at.isoformat(),
                                                                     "action_id": aid})
    return {"step_id": step.step_id, "outcome": "waiting", "reason": "budget_deferred", "action_id": aid}


def _next_period_start(now: datetime, tz: str) -> datetime:
    from zoneinfo import ZoneInfo

    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        zone = ZoneInfo("UTC")
    local = now.astimezone(zone)
    nxt = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.astimezone(now.tzinfo)


def _review_fallback(ctx: KernelContext) -> Callable[[Repo, TaskRuntime, Step, str], dict[str, Any]]:
    def fallback(repo: Repo, rt: TaskRuntime, step: Step, reason: str) -> dict[str, Any]:
        changes = tuple(step.input.get("changes", []))
        aid = None
        if changes or step.kind is StepKind.JUDGE:
            aid = _notify(repo, ctx, rt, step, logical_action="notify.review",
                          payload=review_notification(changes, reason), reason_code=reason,
                          causal_event_id=step.input.get("event_id"))
        return _finish(repo, ctx, step, StepStatus.SUCCEEDED, reason=reason,
                       result={"route": str(Route.HUMAN_REVIEW), "fallback": reason, "actions": [aid] if aid else []})
    return fallback


def _judge(ctx: KernelContext, step: Step) -> dict[str, Any]:
    def apply(repo: Repo, root: TaskRuntime, rt: TaskRuntime, step: Step, proposal: DecisionProposal,
              mc: ModelCall) -> dict[str, Any]:
        rule = WakeDecision(route=Route.JUDGE, reason_code=step.input.get("reason_code", "semantic_classification"),
                            judge_changes=tuple(step.input.get("changes", [])),
                            protected=bool(step.input.get("protected")))
        d = admit_model_decision(rule, proposal)
        now = repo.uow.db_now()
        decision_id = "dec_" + short_hash(step.tenant_id, step.step_id)
        repo.insert(DecisionRecord(
            tenant_id=step.tenant_id, decision_id=decision_id, task_id=rt.task_id, run_id=step.run_id,
            event_id=step.input.get("event_id"), route=d.route, reason_code=d.reason_code,
            evidence_refs=list(proposal.supporting_evidence_refs) or [step.input.get("evidence_ref")],
            uncertainty_flags=list(d.uncertainty_flags),
            suggested_capabilities=list(proposal.suggested_capabilities),  # recorded, never granted (K-02)
            model_call_ref=mc.call_id, policy_version=ctx.config.policy_version, created_at=now))
        actions, steps = [], []
        causal = step.input.get("event_id")
        if d.route in (Route.TEMPLATE_ACTION, Route.HUMAN_REVIEW):
            aid = _notify(repo, ctx, rt, step, logical_action="notify.judged",
                          payload=review_notification(d.judge_changes, d.reason_code), reason_code=d.reason_code,
                          causal_event_id=causal)
            actions += [aid] if aid else []
        elif d.route is Route.PLANNER and rt.lifecycle is TaskLifecycle.ACTIVE:
            spec = load_spec(repo, rt)
            plan = add_step(repo, _run(repo, step), kind=StepKind.PLAN, logical_step_id=f"plan:{causal}",
                            input={**{k: step.input.get(k) for k in ("event_id", "changes", "evidence_ref")},
                                   "reason_code": d.reason_code},
                            max_attempts=ctx.config.max_step_attempts, max_steps=spec.limits.max_steps_per_run)
            if plan is None:
                aid = _notify(repo, ctx, rt, step, logical_action="notify.review",
                              payload=review_notification(d.judge_changes, "step_limit"), reason_code="step_limit",
                              causal_event_id=causal)
                actions += [aid] if aid else []
            else:
                steps.append(plan.step_id)
        return _finish(repo, ctx, step, StepStatus.SUCCEEDED, reason=str(d.route), result={
            "route": str(d.route), "reason_code": d.reason_code, "decision_id": decision_id,
            "model_call_id": mc.call_id, "actions": actions, "followup_steps": steps,
            "uncertainty_flags": list(d.uncertainty_flags)})

    return _model_step(ctx, step, kind="judge", call=lambda b: ctx.reasoning.classify(b), apply=apply,
                       fallback=_review_fallback(ctx))


def _plan(ctx: KernelContext, step: Step) -> dict[str, Any]:
    def apply(repo: Repo, root: TaskRuntime, rt: TaskRuntime, step: Step, plan: PlanProposal,
              mc: ModelCall) -> dict[str, Any]:
        spec = load_spec(repo, rt)
        root_spec = spec if root.task_id == rt.task_id else load_spec(repo, root)
        causal = step.input.get("event_id")
        actions, rejected, followups = [], [], []
        for p in plan.steps[: spec.limits.max_steps_per_run]:
            # model_claims (e.g. "safe", "already approved") are deliberately ignored.
            a = propose(repo, ctx, rt, run_id=step.run_id, causal_event_id=causal, actor="planner",
                        intent=ProposedIntent(logical_action=f"plan:{p.logical_step_id}", tool_id=p.tool_id,
                                              capability=p.capability, payload=dict(p.payload),
                                              reason_code=p.reason_code[:120] or "planned",
                                              preconditions={}, data_egress=tuple(p.data_egress)))
            if a is not None:
                actions.append({"action_id": a.action_id, "status": str(a.status)})
        for f in plan.followups:
            try:
                trg = admit_followup(repo, ctx, root=root, rt=rt, root_spec=root_spec, spec=spec, proposal=f)
                followups.append(trg.trigger_id)
            except KernelError as exc:
                rejected.append({"followup_key": f.followup_key, "code": exc.code, "reason": str(exc)})
                audit.record(repo, ctx.ids, tenant_id=rt.tenant_id, kind="followup.rejected", actor="planner",
                             subject_type="task", subject_id=rt.task_id, task_id=rt.task_id,
                             root_task_id=rt.root_task_id, reason=str(exc)[:200],
                             refs={"followup_key": f.followup_key, "code": exc.code})
        return _finish(repo, ctx, step, StepStatus.SUCCEEDED, reason="planned", result={
            "model_call_id": mc.call_id, "actions": actions, "followups": followups,
            "rejected_followups": rejected})

    return _model_step(ctx, step, kind="plan", call=lambda b: ctx.reasoning.plan(b), apply=apply,
                       fallback=_review_fallback(ctx))


_HANDLERS: dict[StepKind, Callable[[KernelContext, Step], dict[str, Any]]] = {
    StepKind.PROBE: _observe,
    StepKind.PROCESS_DELIVERY: _observe,
    StepKind.WAIT_RESUME: _observe,
    StepKind.JUDGE: _judge,
    StepKind.PLAN: _plan,
}
