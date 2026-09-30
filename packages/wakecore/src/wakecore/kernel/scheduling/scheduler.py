"""Timer scheduler (RFC §6.2, §6.3, T17).

Each due trigger is handled in its own short transaction: lock task aggregate, re-read the
trigger and its generation, verify lifecycle and expiry, write a unique occurrence,
register the probe work item, advance due_at (or consume a one-shot trigger), commit.
The scheduler never calls a model or a connector.
"""
from datetime import timedelta
from typing import Any, Optional

from .. import audit
from ..context import KernelContext
from ..domain.enums import (
    TERMINAL_LIFECYCLES,
    CatchupPolicy,
    StepKind,
    StepStatus,
    TaskLifecycle,
    TriggerKind,
    TriggerStatus,
)
from ..domain.model import Run, Step, TriggerOccurrence, TriggerRecord
from ..execution.steps import add_step, create_run, run_id_for
from ..lifecycle import terminate
from ..locks import lock_task, tx
from ..repo import Repo
from .slots import latest_slot, next_slot


def due_trigger_ids(repo: Repo, limit: int) -> list[tuple[str, str, str]]:
    now = repo.uow.db_now()
    rows = repo.find(TriggerRecord, {"status": TriggerStatus.ACTIVE}, conds=[("due_at", "<=", now)],
                     order_by=("due_at", "trigger_id"), limit=limit)
    return [(t.tenant_id, t.task_id, t.trigger_id) for t in rows]


def tick(ctx: KernelContext, *, limit: Optional[int] = None) -> list[dict[str, Any]]:
    """Fire every trigger due now. Safe to run from several processes concurrently:
    the occurrence primary key makes a slot fire at most once (RFC §6.2)."""
    with tx(ctx.store) as repo:
        candidates = due_trigger_ids(repo, limit or ctx.config.scheduler_batch)
    out = []
    for tenant_id, task_id, trigger_id in candidates:
        with tx(ctx.store) as repo:
            res = fire(repo, ctx, tenant_id=tenant_id, task_id=task_id, trigger_id=trigger_id)
        if res is not None:
            out.append(res)
    return out


def fire(repo: Repo, ctx: KernelContext, *, tenant_id: str, task_id: str, trigger_id: str) -> Optional[dict[str, Any]]:
    root, rt = lock_task(repo, tenant_id, task_id)
    trg = repo.get(TriggerRecord, for_update=True, tenant_id=tenant_id, trigger_id=trigger_id)
    now = repo.uow.db_now()
    if trg is None or trg.status is not TriggerStatus.ACTIVE or trg.due_at is None or trg.due_at > now:
        return None  # somebody else handled it, or it was reconfigured

    def close(status: TriggerStatus, reason: str) -> dict[str, Any]:
        repo.change(trg, status=status, reason=reason, updated_at=now)
        return {"trigger_id": trigger_id, "fired": False, "reason": reason}

    if rt.lifecycle in TERMINAL_LIFECYCLES:
        return close(TriggerStatus.REVOKED, f"task_{rt.lifecycle.lower()}")
    if rt.expires_at <= now:
        terminate(repo, ctx.ids, rt, TaskLifecycle.EXPIRED, actor="scheduler", reason="expired")
        return {"trigger_id": trigger_id, "fired": False, "reason": "task_expired"}
    if rt.lifecycle is TaskLifecycle.DRAINING:
        return close(TriggerStatus.CONSUMED, "task_draining")
    if rt.lifecycle is TaskLifecycle.PAUSED:
        return close(TriggerStatus.PAUSED, "task_paused")
    if rt.lifecycle is not TaskLifecycle.ACTIVE:
        return None
    if trg.expiry is not None and trg.expiry <= now:
        return close(TriggerStatus.CONSUMED, "trigger_expired")
    if root.task_id != rt.task_id and root.lifecycle is not TaskLifecycle.ACTIVE:
        # Child work waits for the root; try again one interval later rather than spinning.
        repo.change(trg, due_at=now + timedelta(seconds=trg.every_seconds or 300), reason="root_not_active",
                    updated_at=now)
        return {"trigger_id": trigger_id, "fired": False, "reason": "root_not_active"}

    # --- which slot, and what comes next -------------------------------------------------
    if trg.kind is TriggerKind.INTERVAL:
        every = int(trg.every_seconds or 0)
        slot, missed = latest_slot(trg.due_at, every, now)
        if trg.catchup_policy is CatchupPolicy.SKIP_MISSED and missed > 0:
            nxt = next_slot(trg.due_at, every, now)
            repo.change(trg, due_at=nxt, reason=f"skipped_{missed}_missed", updated_at=now)
            audit.record(repo, ctx.ids, tenant_id=tenant_id, kind="trigger.skipped", actor="scheduler",
                         subject_type="trigger", subject_id=trigger_id, task_id=task_id,
                         root_task_id=rt.root_task_id, refs={"missed": missed, "next_due_at": nxt.isoformat()})
            return {"trigger_id": trigger_id, "fired": False, "reason": "skipped_missed", "missed": missed}
        coalesced = missed + 1  # snapshot polling: one current read stands for every missed slot (T17)
        changes: dict[str, Any] = {"due_at": slot + timedelta(seconds=every), "reason": "fired"}
    else:
        slot, coalesced = trg.due_at, 1
        changes = {"status": TriggerStatus.CONSUMED, "reason": "fired_once"}

    run_id = run_id_for(tenant_id, task_id, "trg", trigger_id, str(trg.generation), slot.isoformat())
    live = repo.find(Step, {"tenant_id": tenant_id, "task_id": task_id, "kind": StepKind.PROBE},
                     conds=[("status", "in", [StepStatus.READY, StepStatus.RUNNING])], limit=1)
    target_run = live[0].run_id if live else run_id
    inserted = repo.insert_if_absent(TriggerOccurrence(
        tenant_id=tenant_id, trigger_id=trigger_id, generation=trg.generation, scheduled_for=slot, task_id=task_id,
        occurred_at=now, coalesced_count=coalesced, run_id=target_run))
    repo.change(trg, updated_at=now, **changes)
    if not inserted:
        return {"trigger_id": trigger_id, "fired": False, "reason": "occurrence_exists"}
    if not live:
        create_run(repo, rt, run_id, reason=f"trigger:{trigger_id}")
        run = repo.get(Run, tenant_id=tenant_id, run_id=run_id)
        add_step(repo, run, kind=StepKind.PROBE, logical_step_id="probe",
                 input={"trigger_id": trigger_id, "generation": trg.generation, "scheduled_for": slot.isoformat(),
                        "coalesced_count": coalesced, "followup_key": trg.followup_key,
                        "predicate_ref": trg.predicate_ref},
                 max_attempts=ctx.config.max_step_attempts)
    audit.record(repo, ctx.ids, tenant_id=tenant_id, kind="trigger.fired", actor="scheduler",
                 subject_type="trigger", subject_id=trigger_id, task_id=task_id, root_task_id=rt.root_task_id,
                 refs={"scheduled_for": slot.isoformat(), "generation": trg.generation,
                       "coalesced_count": coalesced, "run_id": target_run, "merged_into_live_probe": bool(live)})
    return {"trigger_id": trigger_id, "fired": True, "run_id": target_run, "scheduled_for": slot,
            "coalesced_count": coalesced}
