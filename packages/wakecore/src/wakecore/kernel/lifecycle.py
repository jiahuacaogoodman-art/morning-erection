"""Task-aggregate transitions shared by commands, scheduler, executor and recovery.

Callers must already hold the runtime row lock (Repo.get(..., for_update=True)) inside
the transaction in which they call these helpers.
"""
from datetime import datetime, timezone
from typing import Callable, Optional

from . import audit
from .domain.enums import (
    ACTION_IN_FLIGHT,
    ACTION_NOT_YET_DISPATCHED,
    ActionStatus,
    ApprovalDecision,
    DeliveryStatus,
    OutboxStatus,
    RunStatus,
    StepStatus,
    TaskLifecycle,
    TriggerStatus,
    WaitStatus,
)
from .domain.model import (
    ActionRecord,
    Approval,
    Delivery,
    OutboxEntry,
    Run,
    Step,
    TaskRuntime,
    TaskSpecRecord,
    TriggerRecord,
    WaitRecord,
)
from .domain.statemachines import ensure
from .ports.clock import IdGenerator
from .repo import Repo

# READY steps of a paused task are parked here and restored on resume.
PAUSED_UNTIL = datetime(9999, 1, 1, tzinfo=timezone.utc)
DISPATCH_OUTBOX = "action.dispatch"


def set_lifecycle(repo: Repo, ids: IdGenerator, rt: TaskRuntime, to: TaskLifecycle, *, actor: str,
                  reason: str, bump_epoch: bool = False) -> TaskRuntime:
    ensure("task", rt.lifecycle, to)
    now = repo.uow.db_now()
    changes = dict(lifecycle=to, version=rt.version + 1, lifecycle_reason=reason, updated_at=now)
    if bump_epoch:
        changes["revocation_epoch"] = rt.revocation_epoch + 1
    new = repo.change(rt, expect={"version": rt.version}, **changes)
    audit.record(repo, ids, tenant_id=rt.tenant_id, kind="task.lifecycle", actor=actor, subject_type="task",
                 subject_id=rt.task_id, task_id=rt.task_id, root_task_id=rt.root_task_id,
                 from_state=rt.lifecycle, to_state=to, reason=reason,
                 refs={"revocation_epoch": new.revocation_epoch})
    return new


def _cancel_future_work(repo: Repo, rt: TaskRuntime, *, reason: str, trigger_status: TriggerStatus) -> dict[str, int]:
    now = repo.uow.db_now()
    key = {"tenant_id": rt.tenant_id, "task_id": rt.task_id}
    counts = {"triggers": 0, "waits": 0, "steps": 0, "runs": 0, "deliveries": 0}
    for t in repo.find(TriggerRecord, key, conds=[("status", "in", [TriggerStatus.ACTIVE, TriggerStatus.PAUSED])]):
        repo.change(t, status=trigger_status, generation=t.generation + 1, reason=reason, updated_at=now)
        counts["triggers"] += 1
    for w in repo.find(WaitRecord, {**key, "status": WaitStatus.WAITING}):
        repo.change(w, status=WaitStatus.CANCELLED, resolved_at=now)
        counts["waits"] += 1
    for s in repo.find(Step, key, conds=[("status", "in", [StepStatus.READY, StepStatus.WAITING])]):
        repo.change(s, status=StepStatus.CANCELLED, last_error=reason, updated_at=now)
        counts["steps"] += 1
    for r in repo.find(Run, key, conds=[("status", "in", [RunStatus.QUEUED, RunStatus.WAITING])]):
        repo.change(r, status=RunStatus.CANCELLED, finished_at=now, updated_at=now)
        counts["runs"] += 1
    for d in repo.find(Delivery, {**key, "status": DeliveryStatus.PENDING}):
        repo.change(d, status=DeliveryStatus.SKIPPED, reason=reason, updated_at=now)
        counts["deliveries"] += 1
    return counts


def invalidate_actions(repo: Repo, rt: TaskRuntime, *, reason: str) -> dict[str, int]:
    now = repo.uow.db_now()
    key = {"tenant_id": rt.tenant_id, "task_id": rt.task_id}
    counts = {"actions": 0, "approvals": 0, "outbox": 0}
    for a in repo.find(ActionRecord, key, conds=[("status", "in", list(ACTION_NOT_YET_DISPATCHED))]):
        repo.change(a, status=ActionStatus.CANCELLED, resolution=reason, updated_at=now)
        counts["actions"] += 1
        for ap in repo.find(Approval, {"tenant_id": rt.tenant_id, "action_id": a.action_id},
                            conds=[("decision", "in", [ApprovalDecision.PENDING, ApprovalDecision.APPROVED])]):
            repo.change(ap, decision=ApprovalDecision.REVOKED, reason=reason, decided_at=now)
            counts["approvals"] += 1
        for ob in repo.find(OutboxEntry, {"tenant_id": rt.tenant_id, "ref_id": a.action_id},
                            conds=[("status", "in", [OutboxStatus.PENDING, OutboxStatus.CLAIMED])]):
            repo.change(ob, status=OutboxStatus.DEAD, last_error=reason, updated_at=now)
            counts["outbox"] += 1
    return counts


def in_flight(repo: Repo, rt: TaskRuntime) -> int:
    return repo.count(ActionRecord, {"tenant_id": rt.tenant_id, "task_id": rt.task_id},
                      conds=[("status", "in", list(ACTION_IN_FLIGHT))])


def terminate(repo: Repo, ids: IdGenerator, rt: TaskRuntime, to: TaskLifecycle, *, actor: str,
              reason: str) -> tuple[TaskRuntime, dict[str, int]]:
    """T7 core: new epoch + every successor work item and undispatched action invalidated."""
    new = set_lifecycle(repo, ids, rt, to, actor=actor, reason=reason, bump_epoch=True)
    counts = _cancel_future_work(repo, new, reason=reason, trigger_status=TriggerStatus.REVOKED)
    counts.update(invalidate_actions(repo, new, reason=reason))
    counts["in_flight"] = in_flight(repo, new)
    audit.record(repo, ids, tenant_id=rt.tenant_id, kind="task.invalidated", actor=actor, subject_type="task",
                 subject_id=rt.task_id, task_id=rt.task_id, root_task_id=rt.root_task_id, reason=reason,
                 refs=counts)
    return new, counts


def enter_draining(repo: Repo, ids: IdGenerator, rt: TaskRuntime, *, reason: str) -> TaskRuntime:
    """Goal satisfied: no new observations or open planning; registered outputs may finish."""
    new = set_lifecycle(repo, ids, rt, TaskLifecycle.DRAINING, actor="kernel", reason=reason)
    _cancel_future_work(repo, new, reason="draining", trigger_status=TriggerStatus.CONSUMED)
    return new


def base_effect_key(key: str) -> str:
    """Revisions (#revN) and retries (#retryN) of one logical effect share a base key."""
    return key.split("#", 1)[0]


def _internal_inbox_committed(repo: Repo, rt: TaskRuntime, notify_capability: str) -> bool:
    """Every logical notification has been committed to the inbox (CONFIRMED, or settled by
    an operator). A notify that ended without effect and was not successfully retried
    keeps the task DRAINING: completion must never claim a delivery that did not happen (T19)."""
    actions = repo.find(ActionRecord, {"tenant_id": rt.tenant_id, "task_id": rt.task_id,
                                       "capability": notify_capability})
    if any(a.status in (ACTION_NOT_YET_DISPATCHED | ACTION_IN_FLIGHT) for a in actions):
        return False
    groups: dict[str, set[ActionStatus]] = {}
    for a in actions:
        groups.setdefault(base_effect_key(a.effect_key), set()).add(a.status)
    done = {ActionStatus.CONFIRMED, ActionStatus.RESOLVED_MANUALLY}
    return all(statuses & done for statuses in groups.values())


OUTPUT_EVALUATORS: dict[str, Callable[[Repo, TaskRuntime, str], bool]] = {
    "internal_inbox_committed": _internal_inbox_committed,
}


def maybe_complete(repo: Repo, ids: IdGenerator, rt: TaskRuntime, *, notify_capability: str) -> Optional[TaskRuntime]:
    """DRAINING -> COMPLETED only when every declared required output is satisfied."""
    if rt.lifecycle is not TaskLifecycle.DRAINING:
        return None
    spec = repo.get(TaskSpecRecord, tenant_id=rt.tenant_id, task_id=rt.task_id, spec_version=rt.active_spec_version)
    outputs = (spec.body.get("completion", {}).get("required_outputs", []) if spec else [])
    for name in outputs:
        fn = OUTPUT_EVALUATORS.get(name)
        if fn is None or not fn(repo, rt, notify_capability):
            return None
    return set_lifecycle(repo, ids, rt, TaskLifecycle.COMPLETED, actor="kernel",
                         reason="required_outputs_satisfied")
