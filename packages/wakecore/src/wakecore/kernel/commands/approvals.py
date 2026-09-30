"""Approval commands (RFC §9.2, T11, T12).

An approval is bound to: the authenticated actor, the immutable payload digest, the
grant version, the task revocation epoch and an expiry. Text inside events or model
output can never produce an approval: only this command path, called with an
authenticated principal, writes APPROVED.
"""
from typing import Any, Optional

from .. import audit
from ..actions.intents import ProposedIntent, enqueue_dispatch, propose
from ..context import KernelContext
from ..domain.enums import ActionStatus, ApprovalDecision, OutboxStatus, TaskLifecycle
from ..domain.errors import ApprovalStale, InvalidTransition, NotFound, PolicyDenied, TaskNotActive
from ..domain.model import ActionRecord, Approval, Grant, OutboxEntry, TaskRuntime
from ..domain.statemachines import ensure
from ..locks import lock_task
from ..repo import Repo


def _load(repo: Repo, tenant_id: str, approval_id: str) -> tuple[TaskRuntime, TaskRuntime, Approval, ActionRecord]:
    peek = repo.get(Approval, tenant_id=tenant_id, approval_id=approval_id)
    if peek is None:
        raise NotFound(f"approval {approval_id}")
    root, rt = lock_task(repo, tenant_id, peek.task_id)
    action = repo.get(ActionRecord, for_update=True, tenant_id=tenant_id, action_id=peek.action_id)
    ap = repo.get(Approval, for_update=True, tenant_id=tenant_id, approval_id=approval_id)
    assert action is not None and ap is not None
    return root, rt, ap, action


def approve(repo: Repo, ctx: KernelContext, *, tenant_id: str, actor: str, approval_id: str,
            payload_digest: str) -> dict[str, Any]:
    root, rt, ap, action = _load(repo, tenant_id, approval_id)
    now = repo.uow.db_now()
    if ap.decision is ApprovalDecision.APPROVED and ap.actor == actor and ap.payload_digest == payload_digest:
        return _result(ap, action)  # idempotent repeat
    if ap.decision is not ApprovalDecision.PENDING:
        raise ApprovalStale(f"approval is {ap.decision}")
    if now >= ap.expires_at:
        raise ApprovalStale("approval expired")
    if payload_digest != ap.payload_digest or payload_digest != action.payload_digest:
        raise ApprovalStale("payload digest does not match the action content")
    grant = repo.get(Grant, tenant_id=tenant_id, grant_ref=ap.grant_ref)
    if grant is None or grant.revoked or grant.version != ap.grant_version or \
            (grant.expires_at is not None and grant.expires_at <= now):
        raise ApprovalStale("grant changed since the approval was requested")
    if actor != grant.principal:
        raise PolicyDenied("only the grant principal can approve")
    for t in {root.task_id: root, rt.task_id: rt}.values():
        if t.lifecycle not in (TaskLifecycle.ACTIVE, TaskLifecycle.DRAINING, TaskLifecycle.PAUSED):
            raise TaskNotActive(f"task {t.task_id} is {t.lifecycle}")
    if rt.revocation_epoch != action.revocation_epoch:
        raise ApprovalStale("task was revoked since the action was proposed")
    if action.status is not ActionStatus.WAITING_APPROVAL:
        raise ApprovalStale(f"action is {action.status}")

    ensure("approval", ap.decision, ApprovalDecision.APPROVED)
    ap = repo.change(ap, expect={"decision": ApprovalDecision.PENDING}, decision=ApprovalDecision.APPROVED,
                     actor=actor, decided_at=now)
    ensure("action", action.status, ActionStatus.READY)
    action = repo.change(action, expect={"status": ActionStatus.WAITING_APPROVAL}, status=ActionStatus.READY,
                         updated_at=now)
    enqueue_dispatch(repo, ctx.ids, action)
    audit.record(repo, ctx.ids, tenant_id=tenant_id, kind="approval.approved", actor=actor, subject_type="approval",
                 subject_id=approval_id, task_id=rt.task_id, root_task_id=rt.root_task_id, from_state="PENDING",
                 to_state="APPROVED", refs={"action_id": action.action_id, "payload_digest": payload_digest,
                                            "grant_version": ap.grant_version})
    return _result(ap, action)


def reject(repo: Repo, ctx: KernelContext, *, tenant_id: str, actor: str, approval_id: str,
           reason: Optional[str] = None) -> dict[str, Any]:
    """Rejects one action; the task itself keeps running (RFC §15)."""
    _, rt, ap, action = _load(repo, tenant_id, approval_id)
    grant = repo.get(Grant, tenant_id=tenant_id, grant_ref=ap.grant_ref)
    if grant is None or actor != grant.principal:
        raise PolicyDenied("only the grant principal can reject")
    if ap.decision is ApprovalDecision.REJECTED:
        return _result(ap, action)
    if ap.decision is not ApprovalDecision.PENDING:
        raise ApprovalStale(f"approval is {ap.decision}")
    now = repo.uow.db_now()
    ap = repo.change(ap, decision=ApprovalDecision.REJECTED, actor=actor, decided_at=now, reason=reason)
    if action.status is ActionStatus.WAITING_APPROVAL:
        action = repo.change(action, status=ActionStatus.DENIED, resolution="rejected_by_user", updated_at=now)
    audit.record(repo, ctx.ids, tenant_id=tenant_id, kind="approval.rejected", actor=actor, subject_type="approval",
                 subject_id=approval_id, task_id=rt.task_id, root_task_id=rt.root_task_id, from_state="PENDING",
                 to_state="REJECTED", reason=reason, refs={"action_id": action.action_id})
    return _result(ap, action)


def revise_action(repo: Repo, ctx: KernelContext, *, tenant_id: str, actor: str, action_id: str,
                  payload: dict[str, Any]) -> dict[str, Any]:
    """Changing any protected field creates a new action and a new approval (T11)."""
    peek = repo.get(ActionRecord, tenant_id=tenant_id, action_id=action_id)
    if peek is None:
        raise NotFound(f"action {action_id}")
    _, rt = lock_task(repo, tenant_id, peek.task_id)
    old = repo.get(ActionRecord, for_update=True, tenant_id=tenant_id, action_id=action_id)
    grant = repo.get(Grant, tenant_id=tenant_id, grant_ref=rt.grant_ref)
    if grant is None or actor != grant.principal:
        raise PolicyDenied("only the grant principal can revise an action")
    if old.status not in (ActionStatus.WAITING_APPROVAL, ActionStatus.READY):
        raise InvalidTransition(f"action is {old.status}; it can no longer be revised")
    if rt.lifecycle not in (TaskLifecycle.ACTIVE, TaskLifecycle.DRAINING, TaskLifecycle.PAUSED):
        raise TaskNotActive(f"task is {rt.lifecycle}")
    now = repo.uow.db_now()
    for ap in repo.find(Approval, {"tenant_id": tenant_id, "action_id": action_id},
                        conds=[("decision", "in", [ApprovalDecision.PENDING, ApprovalDecision.APPROVED])]):
        repo.change(ap, decision=ApprovalDecision.REVOKED, reason="content_revised", decided_at=now)
    for ob in repo.find(OutboxEntry, {"tenant_id": tenant_id, "ref_id": action_id},
                        conds=[("status", "in", [OutboxStatus.PENDING, OutboxStatus.CLAIMED])]):
        repo.change(ob, status=OutboxStatus.DEAD, last_error="content_revised", updated_at=now)
    ensure("action", old.status, ActionStatus.CANCELLED)
    repo.change(old, status=ActionStatus.CANCELLED, resolution="superseded_by_revision", updated_at=now)
    revision = repo.count(ActionRecord, {"tenant_id": tenant_id, "task_id": rt.task_id,
                                         "logical_action": old.logical_action}) + 1
    new = propose(repo, ctx, rt, run_id=old.run_id, causal_event_id=old.causal_event_id, actor=actor,
                  intent=ProposedIntent(logical_action=old.logical_action, tool_id=old.tool_id,
                                        capability=old.capability, payload=payload,
                                        reason_code="revised_by_user", preconditions=old.preconditions,
                                        data_egress=tuple(old.data_egress), resource_scope=old.resource_scope),
                  effect_key_override=f"{old.effect_key}#rev{revision}")
    audit.record(repo, ctx.ids, tenant_id=tenant_id, kind="action.revised", actor=actor, subject_type="action",
                 subject_id=action_id, task_id=rt.task_id, root_task_id=rt.root_task_id,
                 refs={"new_action_id": new.action_id if new else None})
    return {"old_action_id": action_id, "action_id": new.action_id if new else None,
            "status": str(new.status) if new else None,
            "approval_id": _approval_id(repo, tenant_id, new.action_id) if new else None}


def resolve_manually(repo: Repo, ctx: KernelContext, *, tenant_id: str, actor: str, action_id: str,
                     note: str) -> dict[str, Any]:
    """An operator settles an UNKNOWN effect after checking the provider by hand."""
    peek = repo.get(ActionRecord, tenant_id=tenant_id, action_id=action_id)
    if peek is None:
        raise NotFound(f"action {action_id}")
    _, rt = lock_task(repo, tenant_id, peek.task_id)
    action = repo.get(ActionRecord, for_update=True, tenant_id=tenant_id, action_id=action_id)
    grant = repo.get(Grant, tenant_id=tenant_id, grant_ref=rt.grant_ref)
    if grant is None or actor != grant.principal:
        raise PolicyDenied("only the grant principal can resolve an action")
    ensure("action", action.status, ActionStatus.RESOLVED_MANUALLY)
    now = repo.uow.db_now()
    action = repo.change(action, status=ActionStatus.RESOLVED_MANUALLY, resolution=note[:500], updated_at=now)
    if rt.lifecycle is TaskLifecycle.DRAINING:
        from ..lifecycle import maybe_complete

        maybe_complete(repo, ctx.ids, rt, notify_capability=ctx.config.notify_capability)
    audit.record(repo, ctx.ids, tenant_id=tenant_id, kind="action.resolved_manually", actor=actor,
                 subject_type="action", subject_id=action_id, task_id=rt.task_id, root_task_id=rt.root_task_id,
                 from_state="UNKNOWN", to_state="RESOLVED_MANUALLY", reason=note[:200])
    return {"action_id": action_id, "status": str(action.status)}


def _approval_id(repo: Repo, tenant_id: str, action_id: str) -> Optional[str]:
    rows = repo.find(Approval, {"tenant_id": tenant_id, "action_id": action_id}, order_by=("-approval_revision",),
                     limit=1)
    return rows[0].approval_id if rows else None


def _result(ap: Approval, action: ActionRecord) -> dict[str, Any]:
    return {"approval_id": ap.approval_id, "decision": str(ap.decision), "action_id": action.action_id,
            "action_status": str(action.status), "payload_digest": ap.payload_digest}
