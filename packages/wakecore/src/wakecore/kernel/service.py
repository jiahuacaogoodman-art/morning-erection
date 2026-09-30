"""Management facade (RFC §15): idempotent, tenant-scoped commands and read models.

Every state-changing call runs in one transaction together with its IdempotencyRecord:
the same principal + key + request returns the stored response, the same key with a
different request is IDEMPOTENCY_CONFLICT (409). Every lookup is scoped to the caller's
tenant, so an object id, secret_ref or evidence_ref from another tenant is NOT_FOUND
(T20) rather than a permission hint.
"""
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

from .commands import approvals as approval_cmd
from .commands import setup as setup_cmd
from .commands import tasks as task_cmd
from .context import KernelContext
from .domain.canonical import digest
from .domain.enums import ApprovalDecision, TriggerStatus
from .domain.errors import IdempotencyConflict, NotFound, SchemaMismatch
from .domain.model import (
    ActionAttempt,
    ActionRecord,
    Approval,
    AuditEntry,
    BudgetReservation,
    DecisionRecord,
    Delivery,
    EventRecord,
    Evidence,
    IdempotencyRecord,
    ModelCall,
    ObservationRecord,
    Run,
    SourceBinding,
    Step,
    TaskRuntime,
    TaskSpecRecord,
    TriggerRecord,
    WaitRecord,
)
from .events.admission import admit
from .lifecycle import in_flight
from .locks import tx
from .ports.annotations import connector_view, to_mcp_tool, tool_view
from .ports.store import DuplicateKey
from .repo import Repo

MAX_IDEMPOTENCY_KEY = 128


@dataclass(frozen=True)
class Principal:
    """An authenticated caller. Only the API layer constructs this, after authentication."""

    tenant_id: str
    subject: str


def view(value: Any) -> Any:
    """Record -> JSON-safe dict, keeping nulls (read models must show what is unknown)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: view(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, (list, tuple)):
        return [view(v) for v in value]
    if isinstance(value, dict):
        return {k: view(v) for k, v in value.items()}
    return value


@dataclass(frozen=True)
class Response:
    status_code: int
    body: dict[str, Any]
    replayed: bool = False


class KernelService:
    def __init__(self, ctx: KernelContext) -> None:
        self.ctx = ctx

    # ================================================================ command plumbing

    def _command(self, p: Principal, op: str, request: dict[str, Any], idem_key: Optional[str],
                 fn: Callable[[Repo], dict[str, Any]], *, status_code: int = 200) -> Response:
        if idem_key is not None and (not idem_key or len(idem_key) > MAX_IDEMPOTENCY_KEY):
            raise SchemaMismatch("Idempotency-Key must be 1..128 characters")
        request_digest = digest({"op": op, "request": view(request)})
        for attempt in range(2):
            try:
                with tx(self.ctx.store) as repo:
                    if idem_key is not None:
                        prior = repo.get(IdempotencyRecord, tenant_id=p.tenant_id, principal=p.subject,
                                         idem_key=idem_key)
                        if prior is not None:
                            if prior.request_digest != request_digest:
                                raise IdempotencyConflict("Idempotency-Key was used for a different request")
                            return Response(prior.status_code, prior.response, replayed=True)
                    body = view(fn(repo))
                    if idem_key is not None:
                        repo.insert(IdempotencyRecord(
                            tenant_id=p.tenant_id, principal=p.subject, idem_key=idem_key,
                            request_digest=request_digest, status_code=status_code, response=body,
                            created_at=repo.uow.db_now()))
                    return Response(status_code, body)
            except DuplicateKey:
                if attempt:  # a concurrent request with the same key committed first: read its result
                    raise
        raise AssertionError("unreachable")

    # ================================================================ setup (user-confirmed only)

    def register_grant(self, p: Principal, *, grant_ref: str, capabilities: list[str], data_egress: list[str],
                       resource_scope: dict[str, Any], expires_at: Optional[datetime] = None,
                       idem_key: Optional[str] = None) -> Response:
        req = dict(grant_ref=grant_ref, capabilities=capabilities, data_egress=data_egress,
                   resource_scope=resource_scope, expires_at=expires_at)
        return self._command(p, "grant.register", req, idem_key, lambda repo: setup_cmd.register_grant(
            repo, self.ctx, tenant_id=p.tenant_id, principal=p.subject, **req), status_code=201)

    def revoke_grant(self, p: Principal, grant_ref: str, *, idem_key: Optional[str] = None) -> Response:
        return self._command(p, "grant.revoke", {"grant_ref": grant_ref}, idem_key, lambda repo: setup_cmd.revoke_grant(
            repo, self.ctx, tenant_id=p.tenant_id, principal=p.subject, grant_ref=grant_ref))

    def register_binding(self, p: Principal, *, source_ref: str, connector_id: str, source_uri: str,
                         resource_scope: dict[str, Any], capabilities: list[str], secret_ref: Optional[str],
                         ingress_secret_ref: Optional[str], idem_key: Optional[str] = None) -> Response:
        req = dict(source_ref=source_ref, connector_id=connector_id, source_uri=source_uri,
                   resource_scope=resource_scope, capabilities=capabilities, secret_ref=secret_ref,
                   ingress_secret_ref=ingress_secret_ref)

        def run(repo: Repo) -> Any:
            b = setup_cmd.register_source_binding(repo, self.ctx, tenant_id=p.tenant_id, owner=p.subject, **req)
            return _binding_view(b)
        return self._command(p, "binding.register", req, idem_key, run, status_code=201)

    def mark_source_reauthorised(self, p: Principal, source_ref: str, *, idem_key: Optional[str] = None) -> Response:
        return self._command(p, "binding.reauthorised", {"source_ref": source_ref}, idem_key,
                             lambda repo: _binding_view(setup_cmd.mark_source_reauthorised(
                                 repo, self.ctx, tenant_id=p.tenant_id, owner=p.subject, source_ref=source_ref)))

    # ================================================================ tasks

    def create_task(self, p: Principal, spec: dict[str, Any], *, idem_key: Optional[str] = None) -> Response:
        def run(repo: Repo) -> dict[str, Any]:
            rec = task_cmd.create_draft(repo, self.ctx, tenant_id=p.tenant_id, principal=p.subject, raw=spec)
            rt = repo.get(TaskRuntime, tenant_id=p.tenant_id, task_id=rec.task_id)
            return {"task_id": rec.task_id, "spec_version": rec.spec_version, "spec_digest": rec.spec_digest,
                    "spec_status": rec.status, "lifecycle": rt.lifecycle, "version": rt.version,
                    "message": "已保存为草稿；需要确认激活后才会开始运行"}
        return self._command(p, "task.create", {"spec": spec}, idem_key, run, status_code=201)

    def activate_task(self, p: Principal, task_id: str, *, spec_version: Optional[int] = None,
                      spec_digest: Optional[str] = None, expected_version: Optional[int] = None,
                      idem_key: Optional[str] = None) -> Response:
        req = dict(task_id=task_id, spec_version=spec_version, spec_digest=spec_digest,
                   expected_version=expected_version)

        def run(repo: Repo) -> dict[str, Any]:
            rt = task_cmd.activate(repo, self.ctx, tenant_id=p.tenant_id, principal=p.subject, task_id=task_id,
                                   spec_version=spec_version, expected_digest=spec_digest,
                                   expected_version=expected_version)
            return {"task_id": rt.task_id, "lifecycle": rt.lifecycle, "version": rt.version,
                    "spec_version": rt.active_spec_version, "grant_version": rt.grant_version,
                    "effective_authority": rt.effective_authority, "expires_at": rt.expires_at}
        return self._command(p, "task.activate", req, idem_key, run)

    def pause_task(self, p: Principal, task_id: str, *, expected_version: Optional[int] = None,
                   idem_key: Optional[str] = None) -> Response:
        return self._command(p, "task.pause", {"task_id": task_id, "expected_version": expected_version}, idem_key,
                             lambda repo: task_cmd.pause(repo, self.ctx, tenant_id=p.tenant_id, principal=p.subject,
                                                         task_id=task_id, expected_version=expected_version))

    def resume_task(self, p: Principal, task_id: str, *, expected_version: Optional[int] = None,
                    idem_key: Optional[str] = None) -> Response:
        return self._command(p, "task.resume", {"task_id": task_id, "expected_version": expected_version}, idem_key,
                             lambda repo: task_cmd.resume(repo, self.ctx, tenant_id=p.tenant_id, principal=p.subject,
                                                          task_id=task_id, expected_version=expected_version))

    def cancel_task(self, p: Principal, task_id: str, *, reason: str = "cancelled_by_user",
                    expected_version: Optional[int] = None, idem_key: Optional[str] = None) -> Response:
        req = {"task_id": task_id, "reason": reason, "expected_version": expected_version}
        return self._command(p, "task.cancel", req, idem_key, lambda repo: task_cmd.cancel(
            repo, self.ctx, tenant_id=p.tenant_id, principal=p.subject, task_id=task_id, reason=reason,
            expected_version=expected_version))

    # ================================================================ approvals / actions

    def approve(self, p: Principal, approval_id: str, *, payload_digest: str,
                idem_key: Optional[str] = None) -> Response:
        req = {"approval_id": approval_id, "payload_digest": payload_digest}
        return self._command(p, "approval.approve", req, idem_key, lambda repo: approval_cmd.approve(
            repo, self.ctx, tenant_id=p.tenant_id, actor=p.subject, approval_id=approval_id,
            payload_digest=payload_digest))

    def reject(self, p: Principal, approval_id: str, *, reason: Optional[str] = None,
               idem_key: Optional[str] = None) -> Response:
        req = {"approval_id": approval_id, "reason": reason}
        return self._command(p, "approval.reject", req, idem_key, lambda repo: approval_cmd.reject(
            repo, self.ctx, tenant_id=p.tenant_id, actor=p.subject, approval_id=approval_id, reason=reason))

    def revise_action(self, p: Principal, action_id: str, *, payload: dict[str, Any],
                      idem_key: Optional[str] = None) -> Response:
        req = {"action_id": action_id, "payload": payload}
        return self._command(p, "action.revise", req, idem_key, lambda repo: approval_cmd.revise_action(
            repo, self.ctx, tenant_id=p.tenant_id, actor=p.subject, action_id=action_id, payload=payload),
            status_code=201)

    def resolve_action(self, p: Principal, action_id: str, *, note: str,
                       idem_key: Optional[str] = None) -> Response:
        req = {"action_id": action_id, "note": note}
        return self._command(p, "action.resolve", req, idem_key, lambda repo: approval_cmd.resolve_manually(
            repo, self.ctx, tenant_id=p.tenant_id, actor=p.subject, action_id=action_id, note=note))

    # ================================================================ ingress (data plane only)

    def ingest(self, source_ref: str, body: bytes, signature: Optional[str]) -> Response:
        """Persist a verified event. Never a control command (K-02); identity dedupes it (T02)."""
        with tx(self.ctx.store) as repo:
            out = admit(repo, self.ctx, source_ref=source_ref, body=body, signature=signature)
        code = out.pop("http_status", None) or (202 if out["status"] == "accepted" else 200)
        return Response(code, out)

    # ================================================================ queries

    def _owned_task(self, repo: Repo, p: Principal, task_id: str) -> TaskRuntime:
        rt = repo.get(TaskRuntime, tenant_id=p.tenant_id, task_id=task_id)
        if rt is None or task_cmd._owner(repo, rt) != p.subject:
            raise NotFound(f"task {task_id}")  # other tenants/principals learn nothing (T20)
        return rt

    def get_task(self, p: Principal, task_id: str) -> dict[str, Any]:
        with tx(self.ctx.store) as repo:
            rt = self._owned_task(repo, p, task_id)
            key = {"tenant_id": p.tenant_id, "task_id": task_id}
            binding = repo.get(SourceBinding, tenant_id=p.tenant_id, source_ref=rt.source_ref)
            trg = repo.get(TriggerRecord, tenant_id=p.tenant_id, trigger_id=task_cmd.main_trigger_id(task_id))
            followups = repo.find(TriggerRecord, {"tenant_id": p.tenant_id, "root_task_id": rt.root_task_id,
                                                  "status": TriggerStatus.ACTIVE},
                                  conds=[("followup_key", "not_null", None)], order_by=("due_at",))
            next_check = None
            if trg is not None and trg.status is TriggerStatus.ACTIVE:
                next_check = trg.due_at
            for f in followups:
                if f.task_id == task_id and f.due_at and (next_check is None or f.due_at < next_check):
                    next_check = f.due_at
            actions = repo.find(ActionRecord, key, order_by=("created_at", "action_id"))
            by_status: dict[str, int] = {}
            for a in actions:
                by_status[str(a.status)] = by_status.get(str(a.status), 0) + 1
            pending = []
            for ap in repo.find(Approval, {**key, "decision": ApprovalDecision.PENDING}, order_by=("created_at",)):
                a = next((x for x in actions if x.action_id == ap.action_id), None)
                pending.append({"approval_id": ap.approval_id, "action_id": ap.action_id,
                                "payload_digest": ap.payload_digest, "expires_at": ap.expires_at,
                                "tool_id": a.tool_id if a else None, "capability": a.capability if a else None,
                                "payload": a.canonical_payload if a else None})
            spec = repo.get(TaskSpecRecord, tenant_id=p.tenant_id, task_id=task_id,
                            spec_version=rt.active_spec_version)
            n = in_flight(repo, rt)
            return view({
                "task_id": rt.task_id, "root_task_id": rt.root_task_id, "parent_task_id": rt.parent_task_id,
                "lifecycle": rt.lifecycle, "lifecycle_reason": rt.lifecycle_reason, "version": rt.version,
                "revocation_epoch": rt.revocation_epoch, "spec_version": rt.active_spec_version,
                "spec_digest": spec.spec_digest if spec else None, "expires_at": rt.expires_at,
                "last_progress_at": rt.last_progress_at,
                "source": {"source_ref": rt.source_ref, "health": binding.health if binding else None,
                           "health_reason": binding.health_reason if binding else None,
                           "last_trusted_at": binding.last_trusted_at if binding else None,
                           "last_attempt_at": binding.last_attempt_at if binding else None},
                "next_check_at": next_check,
                "trigger": {"status": trg.status, "generation": trg.generation} if trg else None,
                "delivery": {"actions_by_status": by_status,
                             "outputs": [{"action_id": a.action_id, "logical_action": a.logical_action,
                                          "tool_id": a.tool_id, "status": a.status, "resolution": a.resolution}
                                         for a in actions]},
                "pending_approvals": pending,
                "in_flight": n,
                "explanation": _explain(rt, binding, n),
            })

    def timeline(self, p: Principal, task_id: str, *, limit: int = 500) -> dict[str, Any]:
        with tx(self.ctx.store) as repo:
            rt = self._owned_task(repo, p, task_id)
            key = {"tenant_id": p.tenant_id, "task_id": task_id}
            items: list[dict[str, Any]] = []
            event_ids = set()
            for d in repo.find(Delivery, key):
                event_ids.add(d.event_id)
                items.append({"at": d.created_at, "kind": "delivery", "event_id": d.event_id, "status": d.status,
                              "run_id": d.run_id, "decision_ref": d.decision_ref, "reason": d.reason})
            decisions = repo.find(DecisionRecord, key, order_by=("created_at", "decision_id"))
            event_ids |= {x.event_id for x in decisions if x.event_id}
            for eid in sorted(event_ids):
                ev = repo.get(EventRecord, tenant_id=p.tenant_id, event_id=eid)
                if ev is not None:
                    items.append({"at": ev.received_at, "kind": "event", "event_id": ev.event_id, "type": ev.type,
                                  "internal": ev.internal, "seq": ev.seq, "evidence_ref": ev.evidence_ref,
                                  "data": ev.data if ev.internal else {"keys": sorted(ev.data)}})
            for o in repo.find(ObservationRecord, key):
                items.append({"at": o.observed_at, "kind": "observation", "observation_id": o.observation_id,
                              "run_id": o.run_id, "outcome": o.outcome, "completeness": o.completeness,
                              "evidence_ref": o.evidence_ref, "error_code": o.error_code,
                              "coverage_gaps": o.coverage_gaps})
            for x in decisions:
                items.append({"at": x.created_at, "kind": "decision", "decision_id": x.decision_id,
                              "event_id": x.event_id, "route": x.route, "reason_code": x.reason_code,
                              "evidence_refs": x.evidence_refs, "uncertainty_flags": x.uncertainty_flags,
                              "model_call_ref": x.model_call_ref})
            for a in repo.find(ActionRecord, key):
                items.append({"at": a.created_at, "kind": "action", "action_id": a.action_id,
                              "logical_action": a.logical_action, "tool_id": a.tool_id, "status": a.status,
                              "payload_digest": a.payload_digest, "causal_event_id": a.causal_event_id,
                              "resolution": a.resolution})
            for au in repo.find(AuditEntry, key, order_by=("seq",)):
                items.append({"at": au.at, "kind": "audit", "seq": au.seq, "audit_kind": au.kind, "actor": au.actor,
                              "subject_type": au.subject_type, "subject_id": au.subject_id,
                              "from_state": au.from_state, "to_state": au.to_state, "reason": au.reason})
            items.sort(key=lambda i: (i["at"], i.get("seq", 0)))
            return view({"task_id": rt.task_id, "items": items[-limit:], "truncated": len(items) > limit})

    def get_run(self, p: Principal, run_id: str) -> dict[str, Any]:
        with tx(self.ctx.store) as repo:
            run = repo.get(Run, tenant_id=p.tenant_id, run_id=run_id)
            if run is None:
                raise NotFound(f"run {run_id}")
            self._owned_task(repo, p, run.task_id)
            key = {"tenant_id": p.tenant_id, "run_id": run_id}
            steps = repo.find(Step, key, order_by=("created_at", "step_id"))
            waits = repo.find(WaitRecord, key, order_by=("created_at",))
            actions = repo.find(ActionRecord, key, order_by=("created_at",))
            attempts = []
            for a in actions:
                for att in repo.find(ActionAttempt, {"tenant_id": p.tenant_id, "action_id": a.action_id},
                                     order_by=("started_at",)):
                    attempts.append({"attempt_id": att.attempt_id, "action_id": a.action_id, "status": att.status,
                                     "started_at": att.started_at, "finished_at": att.finished_at,
                                     "provider_ref": att.provider_ref, "error": att.error})
            calls = repo.find(ModelCall, key, order_by=("created_at",))
            reservations = []
            for mc in calls:
                if mc.reservation_id:
                    reservations += repo.find(BudgetReservation, {"tenant_id": p.tenant_id,
                                                                  "reservation_id": mc.reservation_id})
            cost = {"model_attempts": len(calls),
                    "usage_unknown": sum(1 for c in calls if str(c.status) == "UNKNOWN"),
                    "input_tokens": sum((c.usage or {}).get("input_tokens", 0) for c in calls),
                    "output_tokens": sum((c.usage or {}).get("output_tokens", 0) for c in calls),
                    "cost_minor_units": sum((c.usage or {}).get("cost_minor_units", 0) for c in calls)}
            current = next((s for s in steps if str(s.status) in ("RUNNING", "READY", "WAITING")), None)
            return view({
                "run_id": run.run_id, "task_id": run.task_id, "status": run.status, "reason": run.reason,
                "wait_reason": run.wait_reason, "created_at": run.created_at, "finished_at": run.finished_at,
                "current_step": current.logical_step_id if current else None,
                "steps": [{"step_id": s.step_id, "logical_step_id": s.logical_step_id, "kind": s.kind,
                           "status": s.status, "attempts": s.attempts, "max_attempts": s.max_attempts,
                           "wait_reason": s.wait_reason, "available_at": s.available_at, "last_error": s.last_error}
                          for s in steps],
                "waits": [{"wait_id": w.wait_id, "reason": w.reason, "status": w.status, "due_at": w.due_at,
                           "basis_watermark": w.basis_watermark, "matched_event_id": w.matched_event_id}
                          for w in waits],
                "action_attempts": attempts,
                "model_calls": [{"call_id": c.call_id, "kind": c.kind, "status": c.status, "model_ref": c.model_ref,
                                 "usage": c.usage, "error": c.error} for c in calls],
                "reservations": [{"reservation_id": r.reservation_id, "account_key": r.account_key,
                                  "reserved": r.reserved, "actual": r.actual, "status": r.status}
                                 for r in reservations],
                "cost": cost,
            })

    def get_evidence(self, p: Principal, evidence_ref: str) -> dict[str, Any]:
        with tx(self.ctx.store) as repo:
            ev = repo.get(Evidence, tenant_id=p.tenant_id, evidence_ref=evidence_ref)
            binding = repo.get(SourceBinding, tenant_id=p.tenant_id, source_ref=ev.source_ref) if ev else None
            if ev is None or binding is None or binding.owner != p.subject:
                raise NotFound(f"evidence {evidence_ref}")
            return view(ev)

    def list_tasks(self, p: Principal) -> list[dict[str, Any]]:
        with tx(self.ctx.store) as repo:
            out = []
            for rt in repo.find(TaskRuntime, {"tenant_id": p.tenant_id}, order_by=("task_id",)):
                if task_cmd._owner(repo, rt) == p.subject:
                    out.append(view({"task_id": rt.task_id, "lifecycle": rt.lifecycle, "version": rt.version,
                                     "expires_at": rt.expires_at}))
            return out

    # ================================================================ catalogue (read-only)

    def list_tools(self, p: Principal) -> list[dict[str, Any]]:
        """Installed tools with their MCP form. Installed is not granted: a task still needs a grant,
        and `enabled` only says whether the operator's SystemPolicy allows the capability at all."""
        allowed = self.ctx.system_policy.allowed_capabilities
        auto = self.ctx.system_policy.auto_approve_capabilities
        return [{**tool_view(t.descriptor), "enabled": t.descriptor.capability_type in allowed,
                 "auto_approve": t.descriptor.capability_type in auto, "mcp": to_mcp_tool(t.descriptor)}
                for _, t in sorted(self.ctx.tools.items())]

    def list_connectors(self, p: Principal) -> list[dict[str, Any]]:
        allowed = self.ctx.system_policy.allowed_capabilities
        return [{**connector_view(c.descriptor), "enabled": c.descriptor.read_capability in allowed}
                for _, c in sorted(self.ctx.connectors.items())]


def _binding_view(b: SourceBinding) -> dict[str, Any]:
    # secret_ref / ingress_secret_ref are omitted entirely: callers never need them back.
    return {"source_ref": b.source_ref, "connector_id": b.connector_id, "source_uri": b.source_uri,
            "resource_scope": b.resource_scope, "capabilities": b.capabilities, "health": b.health}


def _explain(rt: TaskRuntime, binding: Optional[SourceBinding], n_in_flight: int) -> str:
    life = str(rt.lifecycle)
    parts = {
        "DRAFT": "草稿，尚未激活",
        "ACTIVE": "运行中",
        "PAUSED": "已暂停，不会开始新的检查",
        "DRAINING": "目标已满足，正在等待最终结果投递确认",
        "COMPLETED": "已完成",
        "CANCELLED": "已取消",
        "EXPIRED": "已到期",
        "FAILED": "已失败",
    }
    msg = parts.get(life, life)
    if binding is not None and str(binding.health) not in ("HEALTHY", "UNKNOWN"):
        msg += f"；来源状态 {binding.health}（{binding.health_reason or '-'}），可信快照未被覆盖"
    if n_in_flight:
        msg += f"；{n_in_flight} 个动作在途或结果待确认"
    return msg

