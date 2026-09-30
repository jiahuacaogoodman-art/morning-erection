"""Action coordinator: the only path from an admitted action to a tool (RFC §9, §11.3 T5/T6, §13.3).

    claim outbox entry (short tx)
    T5  lock task -> re-check lifecycle, epoch, grant, tool version, policy, approval
        -> action DISPATCHING + attempt + permit (commit)
    tool.execute(AuthorizedAction)                 <- no transaction open
    T6  record result: CONFIRMED / FAILED_NO_EFFECT / UNKNOWN (commit)

A timeout or lost response is UNKNOWN, never "failed": the effect may have happened, so
recovery reconciles by effect_key and never blindly re-sends (K-06, T06).
"""
from dataclasses import replace
from datetime import timedelta
from typing import Any, Optional

from .. import audit
from ..commands.tasks import load_spec
from ..context import KernelContext
from ..domain.canonical import digest
from ..domain.enums import (
    ActionStatus,
    ApprovalDecision,
    AuthzOutcome,
    OutboxStatus,
    TaskLifecycle,
)
from ..domain.errors import PolicyDenied
from ..domain.model import ActionAttempt, ActionRecord, Approval, Grant, OutboxEntry, SourceBinding, TaskRuntime
from ..domain.statemachines import ensure
from ..lifecycle import DISPATCH_OUTBOX, base_effect_key, maybe_complete, terminate
from ..locks import lock_task, tx
from ..policy.authority import check_grant, evaluate
from ..ports.action import ActionResult, AuthorizedAction, ReconcileRequest
from ..ports.faults import SimulatedCrash
from ..repo import Repo
from .intents import ProposedIntent, propose, stored_digest

MAX_NO_EFFECT_RETRIES = 3
PAUSED_RECHECK_SECONDS = 60
DEADLINE_GRACE_SECONDS = 30


# --------------------------------------------------------------------------- outbox claim

def claim_outbox(ctx: KernelContext, worker_id: str) -> Optional[OutboxEntry]:
    with tx(ctx.store) as repo:
        now = repo.uow.db_now()
        rows = repo.find(OutboxEntry, {"kind": DISPATCH_OUTBOX, "status": OutboxStatus.PENDING},
                         conds=[("available_at", "<=", now)], order_by=("available_at", "outbox_id"), limit=1,
                         for_update=True, skip_locked=True)
        if not rows:
            return None
        e = rows[0]
        return repo.change(e, expect={"status": OutboxStatus.PENDING, "lease_epoch": e.lease_epoch},
                           status=OutboxStatus.CLAIMED, lease_owner=worker_id, lease_epoch=e.lease_epoch + 1,
                           lease_until=now + timedelta(seconds=ctx.config.dispatch_lease_seconds),
                           attempts=e.attempts + 1, updated_at=now)


def dispatch_once(ctx: KernelContext, worker_id: str) -> Optional[dict[str, Any]]:
    """Dispatch at most one action. Returns None when the outbox is empty."""
    entry = claim_outbox(ctx, worker_id)
    if entry is None:
        return None
    return dispatch_entry(ctx, entry)


def dispatch_entry(ctx: KernelContext, entry: OutboxEntry) -> dict[str, Any]:
    with tx(ctx.store) as repo:
        granted = grant_permit(repo, ctx, entry)
    if granted.get("outcome") != "permitted":
        return granted
    ctx.faults.check("after_dispatch_permit")
    request: AuthorizedAction = granted.pop("request")
    secret_ref = granted.pop("secret_ref", None)
    if secret_ref:  # resolved outside any transaction; never persisted with the action
        request = replace(request, secret=ctx.secrets.resolve(request.tenant_id, secret_ref))
    tool = ctx.tools[request.tool_id]
    try:
        result = tool.execute(request)
        if not isinstance(result, ActionResult) or result.status not in ("confirmed", "failed_no_effect", "unknown"):
            result = ActionResult(status="unknown", error="tool_contract_violation")
    except SimulatedCrash:
        raise
    except Exception as exc:  # noqa: BLE001 - we cannot know whether the effect happened
        result = ActionResult(status="unknown", error=f"{type(exc).__name__}: {exc}"[:500])
    ctx.faults.check("after_tool_execute")
    with tx(ctx.store) as repo:
        return record_result(repo, ctx, tenant_id=request.tenant_id, action_id=request.action_id,
                             attempt_id=request.attempt_id, result=result, outbox_id=entry.outbox_id)


# --------------------------------------------------------------------------- T5

def _close_entry(repo: Repo, entry: OutboxEntry, status: OutboxStatus, error: Optional[str] = None) -> None:
    cur = repo.get(OutboxEntry, for_update=True, tenant_id=entry.tenant_id, outbox_id=entry.outbox_id)
    if cur is not None and cur.status in (OutboxStatus.PENDING, OutboxStatus.CLAIMED):
        repo.change(cur, status=status, lease_until=None, last_error=error, updated_at=repo.uow.db_now())


def _stop(repo: Repo, ctx: KernelContext, action: ActionRecord, entry: OutboxEntry, to: ActionStatus,
          reason: str, *, rt: TaskRuntime) -> dict[str, Any]:
    now = repo.uow.db_now()
    if action.status is not to:
        ensure("action", action.status, to)
        repo.change(action, expect={"status": action.status}, status=to, resolution=reason, updated_at=now)
    _close_entry(repo, entry, OutboxStatus.DEAD if to is ActionStatus.CANCELLED else OutboxStatus.DONE, reason)
    audit.record(repo, ctx.ids, tenant_id=action.tenant_id, kind="action.dispatch_refused", actor="coordinator",
                 subject_type="action", subject_id=action.action_id, task_id=action.task_id,
                 root_task_id=action.root_task_id, from_state=action.status, to_state=to, reason=reason)
    if rt.lifecycle is TaskLifecycle.DRAINING:
        maybe_complete(repo, ctx.ids, rt, notify_capability=ctx.config.notify_capability)
    return {"action_id": action.action_id, "outcome": str(to).lower(), "reason": reason}


def grant_permit(repo: Repo, ctx: KernelContext, entry: OutboxEntry) -> dict[str, Any]:
    peek = repo.get(ActionRecord, tenant_id=entry.tenant_id, action_id=entry.ref_id)
    if peek is None:
        _close_entry(repo, entry, OutboxStatus.DEAD, "action_missing")
        return {"outcome": "dead", "reason": "action_missing"}
    root, rt = lock_task(repo, peek.tenant_id, peek.task_id)
    action = repo.get(ActionRecord, for_update=True, tenant_id=peek.tenant_id, action_id=peek.action_id)
    cur = repo.get(OutboxEntry, for_update=True, tenant_id=entry.tenant_id, outbox_id=entry.outbox_id)
    now = repo.uow.db_now()
    if cur is None or cur.status is not OutboxStatus.CLAIMED or cur.lease_epoch != entry.lease_epoch or \
            cur.lease_owner != entry.lease_owner:
        return {"action_id": action.action_id, "outcome": "stale_claim"}
    if action.status is not ActionStatus.READY:
        _close_entry(repo, entry, OutboxStatus.DONE, f"action_{action.status.lower()}")
        return {"action_id": action.action_id, "outcome": "not_ready", "status": str(action.status)}

    # lifecycle (task and root) -------------------------------------------------------------
    if rt.lifecycle in (TaskLifecycle.ACTIVE, TaskLifecycle.PAUSED, TaskLifecycle.DRAINING) and rt.expires_at <= now:
        rt, _ = terminate(repo, ctx.ids, rt, TaskLifecycle.EXPIRED, actor="coordinator", reason="expired")
        action = repo.get(ActionRecord, for_update=True, tenant_id=action.tenant_id, action_id=action.action_id)
    if TaskLifecycle.PAUSED in (rt.lifecycle, root.lifecycle):
        repo.change(cur, status=OutboxStatus.PENDING, lease_owner=None, lease_until=None, last_error="task_paused",
                    available_at=now + timedelta(seconds=PAUSED_RECHECK_SECONDS), updated_at=now)
        return {"action_id": action.action_id, "outcome": "deferred", "reason": "task_paused"}
    live = (TaskLifecycle.ACTIVE, TaskLifecycle.DRAINING)
    if rt.lifecycle not in live or root.lifecycle not in live:
        return _stop(repo, ctx, action, entry, ActionStatus.CANCELLED, f"task_{str(rt.lifecycle).lower()}", rt=rt)
    if action.revocation_epoch != rt.revocation_epoch:
        return _stop(repo, ctx, action, entry, ActionStatus.CANCELLED, "revocation_epoch_changed", rt=rt)

    # authority ------------------------------------------------------------------------------
    try:
        grant = check_grant(repo.get(Grant, tenant_id=rt.tenant_id, grant_ref=rt.grant_ref),
                            tenant_id=rt.tenant_id, now=now)
    except PolicyDenied as exc:
        return _stop(repo, ctx, action, entry, ActionStatus.DENIED, f"grant_invalid:{exc}", rt=rt)
    if grant.version != rt.grant_version:
        return _stop(repo, ctx, action, entry, ActionStatus.DENIED, "grant_version_changed", rt=rt)
    tool = ctx.tools.get(action.tool_id)
    if tool is None:
        return _stop(repo, ctx, action, entry, ActionStatus.DENIED, "tool_not_installed", rt=rt)
    if tool.descriptor.tool_version != action.tool_version:
        return _stop(repo, ctx, action, entry, ActionStatus.DENIED, "tool_version_changed", rt=rt)
    # The stored row must still be exactly what was admitted/approved (scope + egress included).
    if stored_digest(rt, action) != action.payload_digest:
        return _stop(repo, ctx, action, entry, ActionStatus.DENIED, "digest_mismatch", rt=rt)
    decision = evaluate(rt.effective_authority, tool=tool.descriptor, capability=action.capability,
                        payload=action.canonical_payload, data_egress=action.data_egress,
                        resource_scope=action.resource_scope,
                        auto_approve=ctx.system_policy.auto_approve_capabilities)
    if decision.outcome is AuthzOutcome.DENY or \
            (decision.outcome is AuthzOutcome.REQUIRE_APPROVAL and not action.requires_approval):
        return _stop(repo, ctx, action, entry, ActionStatus.DENIED, f"policy:{decision.reason}", rt=rt)
    # Effective authority was computed at activation; the operator policy may have narrowed since.
    sp = ctx.system_policy
    if action.capability not in sp.allowed_capabilities:
        return _stop(repo, ctx, action, entry, ActionStatus.DENIED, "system_policy:capability", rt=rt)
    if not set(action.data_egress) <= set(sp.allowed_data_egress):
        return _stop(repo, ctx, action, entry, ActionStatus.DENIED, "system_policy:data_egress", rt=rt)
    approval_id = None
    if action.requires_approval:
        aps = repo.find(Approval, {"tenant_id": action.tenant_id, "action_id": action.action_id,
                                   "decision": ApprovalDecision.APPROVED}, order_by=("-approval_revision",), limit=1)
        ap = aps[0] if aps else None
        if ap is None or ap.expires_at <= now or ap.payload_digest != action.payload_digest or \
                ap.grant_version != grant.version:
            return _stop(repo, ctx, action, entry, ActionStatus.DENIED, "approval_stale", rt=rt)
        approval_id = ap.approval_id

    # permit ---------------------------------------------------------------------------------
    attempt_id = ctx.ids.new("att")
    permit = digest({"action_id": action.action_id, "attempt_id": attempt_id, "payload_digest": action.payload_digest,
                     "revocation_epoch": rt.revocation_epoch, "grant_version": grant.version,
                     "tool_version": action.tool_version, "approval_id": approval_id})
    ensure("action", action.status, ActionStatus.DISPATCHING)
    action = repo.change(action, expect={"status": ActionStatus.READY}, status=ActionStatus.DISPATCHING,
                         updated_at=now)
    # The tool gets a hard deadline: min(tool contract, task max_run_seconds). The attempt lease
    # outlives it slightly, so an overrunning tool is classified UNKNOWN by recovery, never retried.
    budget = max(1, min(tool.descriptor.max_duration_seconds, load_spec(repo, rt).limits.max_run_seconds))
    deadline = now + timedelta(seconds=budget)
    until = now + timedelta(seconds=max(ctx.config.dispatch_lease_seconds, budget + DEADLINE_GRACE_SECONDS))
    repo.insert(ActionAttempt(
        tenant_id=action.tenant_id, attempt_id=attempt_id, action_id=action.action_id,
        status=ActionStatus.DISPATCHING, lease_owner=entry.lease_owner or "coordinator", lease_epoch=entry.lease_epoch,
        lease_until=until, started_at=now, finished_at=None, provider_ref=None, provider_request_id=None,
        receipt=None, late_receipt=None, error=None))
    repo.change(cur, lease_until=until, updated_at=now)
    audit.record(repo, ctx.ids, tenant_id=action.tenant_id, kind="action.dispatching", actor="coordinator",
                 subject_type="action", subject_id=action.action_id, task_id=action.task_id,
                 root_task_id=action.root_task_id, from_state="READY", to_state="DISPATCHING",
                 refs={"attempt_id": attempt_id, "permit": permit, "approval_id": approval_id,
                       "effect_key": action.effect_key})
    request = AuthorizedAction(
        tenant_id=action.tenant_id, action_id=action.action_id, attempt_id=attempt_id, effect_key=action.effect_key,
        task_id=action.task_id, tool_id=action.tool_id, tool_version=action.tool_version,
        payload=action.canonical_payload, payload_digest=action.payload_digest,
        revocation_epoch=action.revocation_epoch, permit=permit, secret=None,
        resource_scope=action.resource_scope, data_egress=tuple(action.data_egress), deadline_at=deadline)
    return {"action_id": action.action_id, "outcome": "permitted", "attempt_id": attempt_id, "request": request,
            "secret_ref": _credential_ref(repo, rt, tool.descriptor)}


def _credential_ref(repo: Repo, rt: TaskRuntime, descriptor: Any) -> Optional[str]:
    """A tool that operates the user's own account gets the source binding's *handle*
    (e.g. a browser session_ref). Passwords/cookies stay in the runtime (V0.3 rule 5)."""
    if descriptor.credential != "source_binding":
        return None
    spec = load_spec(repo, rt)
    binding = repo.get(SourceBinding, tenant_id=rt.tenant_id, source_ref=spec.source.binding_ref)
    return binding.secret_ref if binding is not None else None


# --------------------------------------------------------------------------- T6

_STATUS = {"confirmed": ActionStatus.CONFIRMED, "failed_no_effect": ActionStatus.FAILED_NO_EFFECT,
           "unknown": ActionStatus.UNKNOWN}


def record_result(repo: Repo, ctx: KernelContext, *, tenant_id: str, action_id: str, attempt_id: str,
                  result: ActionResult, outbox_id: Optional[str] = None) -> dict[str, Any]:
    peek = repo.get(ActionRecord, tenant_id=tenant_id, action_id=action_id)
    root, rt = lock_task(repo, tenant_id, peek.task_id)
    action = repo.get(ActionRecord, for_update=True, tenant_id=tenant_id, action_id=action_id)
    attempt = repo.get(ActionAttempt, for_update=True, tenant_id=tenant_id, attempt_id=attempt_id)
    now = repo.uow.db_now()
    to = _STATUS[result.status]
    receipt = {"status": result.status, "provider_ref": result.provider_ref,
               "provider_request_id": result.provider_request_id, "receipt": result.receipt, "error": result.error}
    if outbox_id:
        e = repo.get(OutboxEntry, for_update=True, tenant_id=tenant_id, outbox_id=outbox_id)
        if e is not None and e.status is OutboxStatus.CLAIMED:
            repo.change(e, status=OutboxStatus.DONE, lease_until=None, updated_at=now)

    if action.status is not ActionStatus.DISPATCHING or attempt is None or attempt.status is not \
            ActionStatus.DISPATCHING:
        # Late response (e.g. recovery already marked UNKNOWN): keep the receipt; it may settle UNKNOWN.
        if attempt is not None:
            repo.change(attempt, late_receipt=receipt)
        settled = None
        if action.status is ActionStatus.UNKNOWN and to is not ActionStatus.UNKNOWN:
            settled = _settle(repo, ctx, rt, action, to, reason=f"late_receipt:{result.status}")
        audit.record(repo, ctx.ids, tenant_id=tenant_id, kind="action.late_receipt", actor="coordinator",
                     subject_type="action", subject_id=action_id, task_id=action.task_id,
                     root_task_id=action.root_task_id, reason=result.status,
                     refs={"attempt_id": attempt_id, "settled": settled})
        return {"action_id": action_id, "outcome": "late_receipt", "status": str(action.status if settled is None
                                                                                  else settled)}

    ensure("action", action.status, to)
    repo.change(attempt, status=to, finished_at=now, provider_ref=result.provider_ref,
                provider_request_id=result.provider_request_id, receipt=receipt, error=result.error)
    action = repo.change(action, expect={"status": ActionStatus.DISPATCHING}, status=to,
                         resolution=result.error if to is not ActionStatus.CONFIRMED else None, updated_at=now)
    successor = None
    if to is ActionStatus.FAILED_NO_EFFECT:
        successor = _retry_no_effect(repo, ctx, rt, action)
    audit.record(repo, ctx.ids, tenant_id=tenant_id, kind="action.result", actor="coordinator",
                 subject_type="action", subject_id=action_id, task_id=action.task_id,
                 root_task_id=action.root_task_id, from_state="DISPATCHING", to_state=to, reason=result.error,
                 refs={"attempt_id": attempt_id, "provider_ref": result.provider_ref, "successor": successor})
    if rt.lifecycle is TaskLifecycle.DRAINING:
        maybe_complete(repo, ctx.ids, rt, notify_capability=ctx.config.notify_capability)
    return {"action_id": action_id, "outcome": str(to).lower(), "attempt_id": attempt_id, "successor": successor}


def _settle(repo: Repo, ctx: KernelContext, rt: TaskRuntime, action: ActionRecord, to: ActionStatus, *,
            reason: str) -> ActionStatus:
    """UNKNOWN -> CONFIRMED / FAILED_NO_EFFECT from evidence (reconciliation or a late receipt)."""
    ensure("action", action.status, to)
    action = repo.change(action, expect={"status": ActionStatus.UNKNOWN}, status=to, resolution=reason,
                         updated_at=repo.uow.db_now())
    if to is ActionStatus.FAILED_NO_EFFECT:
        _retry_no_effect(repo, ctx, rt, action)
    if rt.lifecycle is TaskLifecycle.DRAINING:
        maybe_complete(repo, ctx.ids, rt, notify_capability=ctx.config.notify_capability)
    return to


def _retry_no_effect(repo: Repo, ctx: KernelContext, rt: TaskRuntime, action: ActionRecord) -> Optional[str]:
    """A proven no-effect failure may be retried as a new, linked action (never for approved effects,
    which need a fresh approval). A paused task still gets its successor: the dispatcher defers it
    until resume, whereas skipping it would lose the notification for good."""
    if action.requires_approval or \
            rt.lifecycle not in (TaskLifecycle.ACTIVE, TaskLifecycle.PAUSED, TaskLifecycle.DRAINING):
        return None
    base = base_effect_key(action.effect_key)
    n = repo.count(ActionRecord, {"tenant_id": action.tenant_id, "task_id": action.task_id},
                   conds=[("effect_key", "in", [f"{base}#retry{i}" for i in range(1, MAX_NO_EFFECT_RETRIES + 1)])])
    if n >= MAX_NO_EFFECT_RETRIES:
        return None
    successor = propose(repo, ctx, rt, run_id=action.run_id, causal_event_id=action.causal_event_id,
                        actor="coordinator", effect_key_override=f"{base}#retry{n + 1}",
                        intent=ProposedIntent(logical_action=action.logical_action, tool_id=action.tool_id,
                                              capability=action.capability, payload=action.canonical_payload,
                                              reason_code=action.reason_code or "retry",
                                              preconditions=action.preconditions,
                                              data_egress=tuple(action.data_egress),
                                              resource_scope=action.resource_scope))
    if successor is None:
        return None
    delay = min(ctx.config.retry_max_seconds, ctx.config.retry_base_seconds * (2 ** n))
    for e in repo.find(OutboxEntry, {"tenant_id": action.tenant_id, "ref_id": successor.action_id,
                                     "status": OutboxStatus.PENDING}):
        repo.change(e, available_at=repo.uow.db_now() + timedelta(seconds=delay))
    return successor.action_id


# --------------------------------------------------------------------------- reconciliation (T06)

def reconcile_unknown(ctx: KernelContext, *, limit: int = 50) -> dict[str, int]:
    """Ask tools what happened by effect_key; never re-send an UNKNOWN effect."""
    counts = {"confirmed": 0, "no_effect": 0, "still_unknown": 0, "manual": 0}
    with tx(ctx.store) as repo:
        unknown = repo.find(ActionRecord, {"status": ActionStatus.UNKNOWN}, order_by=("updated_at", "action_id"),
                            limit=limit)
        targets = []
        now = repo.uow.db_now()
        for a in unknown:
            last = repo.find(ActionAttempt, {"tenant_id": a.tenant_id, "action_id": a.action_id},
                             order_by=("-started_at",), limit=1)
            tool = ctx.tools.get(a.tool_id)
            rt = repo.get(TaskRuntime, tenant_id=a.tenant_id, task_id=a.task_id)
            ref = _credential_ref(repo, rt, tool.descriptor) if tool is not None and rt is not None else None
            targets.append((a, last[0] if last else None, ref))
    for a, last, secret_ref in targets:
        tool = ctx.tools.get(a.tool_id)
        if tool is None or not tool.descriptor.supports_reconciliation:
            counts["manual"] += 1  # stays UNKNOWN until an operator resolves it (RESOLVED_MANUALLY)
            continue
        try:
            rr = tool.reconcile(ReconcileRequest(
                tenant_id=a.tenant_id, action_id=a.action_id, effect_key=a.effect_key, tool_id=a.tool_id,
                payload_digest=a.payload_digest, provider_request_id=last.provider_request_id if last else None,
                payload=a.canonical_payload, resource_scope=a.resource_scope,
                attempted_at=last.started_at if last else None, requested_at=now,
                secret=ctx.secrets.resolve(a.tenant_id, secret_ref) if secret_ref else None))
        except Exception:  # noqa: BLE001
            counts["still_unknown"] += 1
            continue
        if rr.status not in ("confirmed", "no_effect"):
            counts["still_unknown"] += 1
            continue
        with tx(ctx.store) as repo:
            _, rt = lock_task(repo, a.tenant_id, a.task_id)
            cur = repo.get(ActionRecord, for_update=True, tenant_id=a.tenant_id, action_id=a.action_id)
            if cur is None or cur.status is not ActionStatus.UNKNOWN:
                continue
            to = ActionStatus.CONFIRMED if rr.status == "confirmed" else ActionStatus.FAILED_NO_EFFECT
            _settle(repo, ctx, rt, cur, to, reason=f"reconciled:{rr.status}")
            audit.record(repo, ctx.ids, tenant_id=a.tenant_id, kind="action.reconciled", actor="recovery",
                         subject_type="action", subject_id=a.action_id, task_id=a.task_id,
                         root_task_id=a.root_task_id, from_state="UNKNOWN", to_state=to,
                         refs={"evidence_digest": digest(rr.evidence), "effect_key": a.effect_key})
        counts["confirmed" if rr.status == "confirmed" else "no_effect"] += 1
    return counts

