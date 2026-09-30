"""Crash recovery (RFC §12.4, T05, T06, T09).

Recovery is idempotent and runs in small, separate transactions. It never repeats an
external effect: expired step leases go back to READY (or FAILED) with backoff, expired
dispatch attempts become UNKNOWN and are reconciled by effect_key, and a model call whose
outcome is unknown keeps its budget reservation until it is reconciled.
"""
from typing import Any

from .. import audit
from ..actions.coordinator import reconcile_unknown
from ..budget.ledger import mark_awaiting_reconciliation
from ..context import KernelContext
from ..domain.enums import (
    ActionStatus,
    ApprovalDecision,
    ModelCallStatus,
    OutboxStatus,
    StepKind,
    StepStatus,
    TaskLifecycle,
)
from ..domain.model import (
    ActionAttempt,
    ActionRecord,
    Approval,
    ExecutionSlot,
    ModelCall,
    OutboxEntry,
    Step,
    TaskRuntime,
)
from ..lifecycle import DISPATCH_OUTBOX, maybe_complete, terminate
from ..locks import lock_task, tx
from ..scheduling.waits import resolve_waits
from .executor import requeue_or_fail


def recover(ctx: KernelContext) -> dict[str, Any]:
    limit = ctx.config.recovery_batch
    out: dict[str, Any] = {}
    out["steps"] = _expired_steps(ctx, limit)
    out["slots"] = _stale_slots(ctx, limit)
    out["dispatch"] = _expired_dispatch(ctx, limit)
    out["reconcile"] = reconcile_unknown(ctx, limit=limit)
    with tx(ctx.store) as repo:
        out["waits"] = resolve_waits(repo, limit=limit)
    out["approvals"] = _expired_approvals(ctx, limit)
    out["expired_tasks"] = _expired_tasks(ctx, limit)
    out["completed"] = _drain(ctx, limit)
    return out


def _expired_steps(ctx: KernelContext, limit: int) -> int:
    with tx(ctx.store) as repo:
        now = repo.uow.db_now()
        rows = [(s.tenant_id, s.task_id, s.step_id) for s in repo.find(
            Step, {"status": StepStatus.RUNNING}, conds=[("lease_until", "<=", now)],
            order_by=("lease_until", "step_id"), limit=limit)]
    n = 0
    for tenant_id, task_id, step_id in rows:
        with tx(ctx.store) as repo:
            lock_task(repo, tenant_id, task_id)
            step = repo.get(Step, for_update=True, tenant_id=tenant_id, step_id=step_id)
            now = repo.uow.db_now()
            if step is None or step.status is not StepStatus.RUNNING or step.lease_until is None or \
                    step.lease_until > now:
                continue  # renewed or finished meanwhile
            if step.kind in (StepKind.JUDGE, StepKind.PLAN):
                for mc in repo.find(ModelCall, {"tenant_id": tenant_id, "step_id": step_id,
                                                "status": ModelCallStatus.STARTED}):
                    # The request may have reached the provider: cost unknown, reservation kept (T16).
                    repo.change(mc, status=ModelCallStatus.UNKNOWN, error="lease_expired", finished_at=now)
                    if mc.reservation_id:
                        mark_awaiting_reconciliation(repo, tenant_id=tenant_id, reservation_id=mc.reservation_id)
            requeue_or_fail(repo, ctx, step, error="lease_expired", actor="recovery")
            n += 1
    return n


def _stale_slots(ctx: KernelContext, limit: int) -> int:
    n = 0
    with tx(ctx.store) as repo:
        now = repo.uow.db_now()
        for slot in repo.find(ExecutionSlot, {}, conds=[("lease_until", "<=", now)], limit=limit, for_update=True,
                              skip_locked=True):
            holder = repo.get(Step, tenant_id=slot.tenant_id, step_id=slot.holder_step_id)
            if holder is not None and holder.status is StepStatus.RUNNING and holder.lease_until is not None and \
                    holder.lease_until > now:
                continue
            repo.delete(slot, expect={"lease_epoch": slot.lease_epoch})
            n += 1
    return n


def _expired_dispatch(ctx: KernelContext, limit: int) -> dict[str, int]:
    counts = {"unknown": 0, "requeued": 0, "closed": 0}
    with tx(ctx.store) as repo:
        now = repo.uow.db_now()
        attempts = [(a.tenant_id, a.attempt_id, a.action_id) for a in repo.find(
            ActionAttempt, {"status": ActionStatus.DISPATCHING}, conds=[("lease_until", "<=", now)],
            order_by=("lease_until", "attempt_id"), limit=limit)]
    for tenant_id, attempt_id, action_id in attempts:
        with tx(ctx.store) as repo:
            peek = repo.get(ActionRecord, tenant_id=tenant_id, action_id=action_id)
            lock_task(repo, tenant_id, peek.task_id)
            action = repo.get(ActionRecord, for_update=True, tenant_id=tenant_id, action_id=action_id)
            att = repo.get(ActionAttempt, for_update=True, tenant_id=tenant_id, attempt_id=attempt_id)
            now = repo.uow.db_now()
            if att is None or att.status is not ActionStatus.DISPATCHING or att.lease_until > now:
                continue
            # We do not know whether the effect happened: UNKNOWN, reconcile, never resend (K-06).
            repo.change(att, status=ActionStatus.UNKNOWN, finished_at=now, error="dispatch_lease_expired")
            if action.status is ActionStatus.DISPATCHING:
                repo.change(action, expect={"status": ActionStatus.DISPATCHING}, status=ActionStatus.UNKNOWN,
                            resolution="dispatch_lease_expired", updated_at=now)
            audit.record(repo, ctx.ids, tenant_id=tenant_id, kind="action.unknown", actor="recovery",
                         subject_type="action", subject_id=action_id, task_id=action.task_id,
                         root_task_id=action.root_task_id, from_state="DISPATCHING", to_state="UNKNOWN",
                         reason="dispatch_lease_expired", refs={"attempt_id": attempt_id})
            counts["unknown"] += 1
    with tx(ctx.store) as repo:
        now = repo.uow.db_now()
        for e in repo.find(OutboxEntry, {"kind": DISPATCH_OUTBOX, "status": OutboxStatus.CLAIMED},
                           conds=[("lease_until", "<=", now)], limit=limit, for_update=True, skip_locked=True):
            action = repo.get(ActionRecord, tenant_id=e.tenant_id, action_id=e.ref_id)
            if action is not None and action.status is ActionStatus.READY:
                # Claimed but no permit was granted: nothing was sent, so dispatch may be retried.
                repo.change(e, status=OutboxStatus.PENDING, lease_owner=None, lease_until=None,
                            last_error="claim_expired", updated_at=now)
                counts["requeued"] += 1
            else:
                repo.change(e, status=OutboxStatus.DONE, lease_until=None, last_error="claim_expired",
                            updated_at=now)
                counts["closed"] += 1
    return counts


def _expired_approvals(ctx: KernelContext, limit: int) -> int:
    with tx(ctx.store) as repo:
        now = repo.uow.db_now()
        rows = [(a.tenant_id, a.task_id, a.approval_id) for a in repo.find(
            Approval, {"decision": ApprovalDecision.PENDING}, conds=[("expires_at", "<=", now)],
            order_by=("expires_at", "approval_id"), limit=limit)]
    n = 0
    for tenant_id, task_id, approval_id in rows:
        with tx(ctx.store) as repo:
            _, rt = lock_task(repo, tenant_id, task_id)
            ap = repo.get(Approval, for_update=True, tenant_id=tenant_id, approval_id=approval_id)
            now = repo.uow.db_now()
            if ap is None or ap.decision is not ApprovalDecision.PENDING or ap.expires_at > now:
                continue
            repo.change(ap, expect={"decision": ApprovalDecision.PENDING}, decision=ApprovalDecision.EXPIRED,
                        decided_at=now, reason="expired")
            action = repo.get(ActionRecord, for_update=True, tenant_id=tenant_id, action_id=ap.action_id)
            if action is not None and action.status is ActionStatus.WAITING_APPROVAL:
                repo.change(action, expect={"status": ActionStatus.WAITING_APPROVAL}, status=ActionStatus.CANCELLED,
                            resolution="approval_expired", updated_at=now)
            audit.record(repo, ctx.ids, tenant_id=tenant_id, kind="approval.expired", actor="recovery",
                         subject_type="approval", subject_id=approval_id, task_id=task_id,
                         root_task_id=rt.root_task_id, from_state="PENDING", to_state="EXPIRED",
                         refs={"action_id": ap.action_id})
            if rt.lifecycle is TaskLifecycle.DRAINING:
                maybe_complete(repo, ctx.ids, rt, notify_capability=ctx.config.notify_capability)
            n += 1
    return n


def _expired_tasks(ctx: KernelContext, limit: int) -> int:
    live = [TaskLifecycle.ACTIVE, TaskLifecycle.PAUSED, TaskLifecycle.DRAINING]
    with tx(ctx.store) as repo:
        now = repo.uow.db_now()
        rows = [(t.tenant_id, t.task_id) for t in repo.find(
            TaskRuntime, {}, conds=[("lifecycle", "in", live), ("expires_at", "<=", now)],
            order_by=("expires_at", "task_id"), limit=limit)]
    n = 0
    for tenant_id, task_id in rows:
        with tx(ctx.store) as repo:
            _, rt = lock_task(repo, tenant_id, task_id)
            if rt.lifecycle in live and rt.expires_at <= repo.uow.db_now():
                terminate(repo, ctx.ids, rt, TaskLifecycle.EXPIRED, actor="recovery", reason="expired")
                n += 1
    return n


def _drain(ctx: KernelContext, limit: int) -> int:
    with tx(ctx.store) as repo:
        rows = [(t.tenant_id, t.task_id) for t in repo.find(
            TaskRuntime, {"lifecycle": TaskLifecycle.DRAINING}, order_by=("task_id",), limit=limit)]
    n = 0
    for tenant_id, task_id in rows:
        with tx(ctx.store) as repo:
            _, rt = lock_task(repo, tenant_id, task_id)
            if maybe_complete(repo, ctx.ids, rt, notify_capability=ctx.config.notify_capability) is not None:
                n += 1
    return n
