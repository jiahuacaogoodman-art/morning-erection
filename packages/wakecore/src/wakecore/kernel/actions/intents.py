"""Immutable action intents (RFC §4, §9.1, §9.2).

An action is canonicalised and digested *before* approval. Changing any protected field
means a new action and a new approval (T11). Authorisation comes from policy over the
task's effective authority, never from the proposer (K-01, K-02).
"""
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from .. import audit
from ..context import KernelContext
from ..domain.canonical import canonical_json, digest, short_hash
from ..domain.enums import ActionStatus, ApprovalDecision, AuthzOutcome, OutboxStatus
from ..domain.model import ActionRecord, Approval, OutboxEntry, QuarantineRecord, TaskRuntime
from ..domain.statemachines import ensure
from ..lifecycle import DISPATCH_OUTBOX
from ..policy.authority import evaluate
from ..repo import Repo


@dataclass(frozen=True)
class ProposedIntent:
    logical_action: str
    tool_id: str
    capability: str
    payload: dict[str, Any]
    reason_code: str
    preconditions: dict[str, Any]
    data_egress: tuple[str, ...] = ()
    resource_scope: Optional[dict[str, Any]] = None  # None: the task authority's whole scope


def effect_key(tenant_id: str, task_id: str, causal_event_id: Optional[str], logical_action: str) -> str:
    return f"{tenant_id}:{task_id}:{causal_event_id or '-'}:{logical_action}"


def action_digest(rt: TaskRuntime, tool_id: str, tool_version: str, capability: str, payload: dict[str, Any],
                  preconditions: dict[str, Any], resource_scope: dict[str, Any], data_egress: list[str]) -> str:
    """The approval binds exactly this: what, with which tool, inside which resource domain,
    sending data where (V0.3 P0). Widening any of them is a different action."""
    return digest({
        "tenant_id": rt.tenant_id, "task_id": rt.task_id, "tool_id": tool_id, "tool_version": tool_version,
        "capability": capability, "payload": payload, "preconditions": preconditions,
        "resource_scope": resource_scope, "data_egress": sorted(data_egress),
        "grant_ref": rt.grant_ref, "grant_version": rt.grant_version,
    })


def stored_digest(rt: TaskRuntime, action: ActionRecord) -> str:
    """Recompute the digest from the stored row (dispatch-time tamper check)."""
    return action_digest(rt, action.tool_id, action.tool_version, action.capability, action.canonical_payload,
                         action.preconditions, action.resource_scope, list(action.data_egress))


def enqueue_dispatch(repo: Repo, ids: Any, action: ActionRecord) -> bool:
    now = repo.uow.db_now()
    return repo.insert_if_absent(OutboxEntry(
        tenant_id=action.tenant_id, outbox_id=ids.new("obx"), kind=DISPATCH_OUTBOX, ref_id=action.action_id,
        dedupe_key=f"dispatch:{action.action_id}", status=OutboxStatus.PENDING, available_at=now, attempts=0,
        lease_owner=None, lease_epoch=0, lease_until=None, last_error=None, created_at=now, updated_at=now))


def propose(repo: Repo, ctx: KernelContext, rt: TaskRuntime, *, run_id: str, causal_event_id: Optional[str],
            intent: ProposedIntent, actor: str, effect_key_override: Optional[str] = None) -> Optional[ActionRecord]:
    """Insert the intent idempotently by effect_key and admit it through policy.
    Returns the (possibly pre-existing) action; None if the identity conflicted."""
    now = repo.uow.db_now()
    tool = ctx.tools.get(intent.tool_id)
    tool_version = tool.descriptor.tool_version if tool else "unknown"
    payload = json_roundtrip(intent.payload)
    key = effect_key_override or effect_key(rt.tenant_id, rt.task_id, causal_event_id, intent.logical_action)
    scope = json_roundtrip(intent.resource_scope if intent.resource_scope is not None
                           else rt.effective_authority.get("resource_scope", {}))
    egress = sorted(set(intent.data_egress) | set(tool.descriptor.required_egress if tool else ()))
    pd = action_digest(rt, intent.tool_id, tool_version, intent.capability, payload, intent.preconditions,
                       scope, egress)
    action = ActionRecord(
        tenant_id=rt.tenant_id, action_id="act_" + short_hash(rt.tenant_id, rt.task_id, key), task_id=rt.task_id,
        root_task_id=rt.root_task_id, run_id=run_id, effect_key=key, causal_event_id=causal_event_id,
        logical_action=intent.logical_action, tool_id=intent.tool_id, tool_version=tool_version,
        capability=intent.capability, canonical_payload=payload, payload_digest=pd,
        preconditions=intent.preconditions, resource_scope=scope, data_egress=egress,
        status=ActionStatus.PROPOSED, requires_approval=False,
        revocation_epoch=rt.revocation_epoch, reason_code=intent.reason_code, resolution=None,
        created_at=now, updated_at=now)
    if not repo.insert_if_absent(action):
        existing = repo.get(ActionRecord, tenant_id=rt.tenant_id, action_id=action.action_id)
        if existing is None:
            existing = next(iter(repo.find(ActionRecord, {"tenant_id": rt.tenant_id, "task_id": rt.task_id,
                                                          "effect_key": key})), None)
        if existing is not None and existing.payload_digest == pd:
            return existing  # same identity, same content: duplicate
        repo.insert(QuarantineRecord(
            tenant_id=rt.tenant_id, quarantine_id=ids_new(ctx, "qua"), kind="action_identity_conflict", ref=key,
            reason="same effect_key, different payload digest", payload={"digest": pd}, created_at=now))
        return None

    decision = evaluate(rt.effective_authority, tool=tool.descriptor if tool else None, capability=intent.capability,
                        payload=payload, data_egress=egress, resource_scope=scope,
                        auto_approve=ctx.system_policy.auto_approve_capabilities)
    if decision.outcome is AuthzOutcome.DENY:
        ensure("action", action.status, ActionStatus.DENIED)
        action = repo.change(action, status=ActionStatus.DENIED, resolution=decision.reason, updated_at=now)
    elif decision.outcome is AuthzOutcome.REQUIRE_APPROVAL:
        ensure("action", action.status, ActionStatus.WAITING_APPROVAL)
        action = repo.change(action, status=ActionStatus.WAITING_APPROVAL, requires_approval=True, updated_at=now)
        expires = min(now + timedelta(seconds=ctx.config.approval_ttl_seconds), rt.expires_at)
        repo.insert(Approval(
            tenant_id=rt.tenant_id, approval_id="apr_" + short_hash(rt.tenant_id, action.action_id, "1"),
            action_id=action.action_id, task_id=rt.task_id, approval_revision=1, payload_digest=pd,
            grant_ref=rt.grant_ref, grant_version=rt.grant_version, expires_at=expires,
            decision=ApprovalDecision.PENDING, actor=None, decided_at=None, reason=None, created_at=now))
    else:
        ensure("action", action.status, ActionStatus.READY)
        action = repo.change(action, status=ActionStatus.READY, updated_at=now)
        enqueue_dispatch(repo, ctx.ids, action)
    audit.record(repo, ctx.ids, tenant_id=rt.tenant_id, kind="action.proposed", actor=actor, subject_type="action",
                 subject_id=action.action_id, task_id=rt.task_id, root_task_id=rt.root_task_id,
                 to_state=action.status, reason=decision.reason,
                 refs={"effect_key": key, "payload_digest": pd, "tool_id": intent.tool_id})
    return action


def ids_new(ctx: KernelContext, prefix: str) -> str:
    return ctx.ids.new(prefix)


def json_roundtrip(value: dict[str, Any]) -> dict[str, Any]:
    """Reject anything that is not canonical JSON (e.g. floats NaN, non-string keys)."""
    import json

    return json.loads(canonical_json(value))
