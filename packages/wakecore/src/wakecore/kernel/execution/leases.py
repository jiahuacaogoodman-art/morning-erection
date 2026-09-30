"""Step leases and the per-task execution slot (RFC §12.1–§12.3, T07, T08).

Claiming a step and taking its task's execution slot happen in one short transaction,
so two different events of the same task can never compute concurrently. Every grant
of execution increments lease_epoch; commits must present the current owner + epoch
while the lease is still valid, otherwise they are rejected with StaleLease.
"""
from datetime import timedelta
from typing import Optional

from ..context import KernelContext
from ..domain.enums import TERMINAL_LIFECYCLES, RunStatus, StepStatus, TaskLifecycle
from ..domain.errors import StaleLease
from ..domain.model import ExecutionSlot, Run, Step, TaskRuntime
from ..execution.steps import _path, set_run, set_step, settle_run_after_step
from ..lifecycle import PAUSED_UNTIL
from ..locks import tx
from ..repo import Repo


def claim_next_step(ctx: KernelContext, worker_id: str) -> Optional[Step]:
    with tx(ctx.store) as repo:
        return claim_in(repo, ctx, worker_id)


def claim_in(repo: Repo, ctx: KernelContext, worker_id: str) -> Optional[Step]:
    now = repo.uow.db_now()
    until = now + timedelta(seconds=ctx.config.lease_seconds)
    candidates = repo.find(Step, {"status": StepStatus.READY}, conds=[("available_at", "<=", now)],
                           order_by=("-priority", "available_at", "step_id"), limit=ctx.config.claim_candidates,
                           for_update=True, skip_locked=True)
    for step in candidates:
        rt = repo.get(TaskRuntime, tenant_id=step.tenant_id, task_id=step.task_id)
        run = repo.get(Run, tenant_id=step.tenant_id, run_id=step.run_id)
        if rt is None or run is None:
            continue
        if rt.lifecycle in TERMINAL_LIFECYCLES or run.status in (RunStatus.CANCELLED, RunStatus.FAILED,
                                                                  RunStatus.SUCCEEDED):
            set_step(repo, step, StepStatus.CANCELLED, last_error=f"task_{rt.lifecycle.lower()}")
            settle_run_after_step(repo, step.run_id, step.tenant_id)
            continue
        if rt.lifecycle is TaskLifecycle.PAUSED:
            repo.change(step, available_at=PAUSED_UNTIL, updated_at=now)
            continue
        slot = repo.get(ExecutionSlot, for_update=True, tenant_id=step.tenant_id, task_id=step.task_id)
        epoch = step.lease_epoch + 1
        if slot is None:
            if not repo.insert_if_absent(ExecutionSlot(tenant_id=step.tenant_id, task_id=step.task_id,
                                                       holder_step_id=step.step_id, lease_owner=worker_id,
                                                       lease_epoch=1, lease_until=until)):
                continue  # a concurrent claimer took the slot first
        elif slot.lease_until > now and slot.holder_step_id != step.step_id:
            continue  # the task is busy; other tasks may still run (T08)
        else:
            repo.change(slot, expect={"lease_epoch": slot.lease_epoch}, holder_step_id=step.step_id,
                        lease_owner=worker_id, lease_epoch=slot.lease_epoch + 1, lease_until=until)
        claimed = set_step(repo, step, StepStatus.RUNNING, lease_owner=worker_id, lease_epoch=epoch,
                           lease_until=until, attempts=step.attempts + 1,
                           current_attempt_id=ctx.ids.new("sat"))
        for hop in _path(run.status, RunStatus.RUNNING):
            run = set_run(repo, run, hop)
        return claimed
    return None


def verify_for_commit(repo: Repo, step: Step) -> Step:
    """Owner + epoch + unexpired lease + slot holder, under the row lock (RFC §12.3)."""
    now = repo.uow.db_now()
    cur = repo.get(Step, for_update=True, tenant_id=step.tenant_id, step_id=step.step_id)
    if (cur is None or cur.status is not StepStatus.RUNNING or cur.lease_owner != step.lease_owner
            or cur.lease_epoch != step.lease_epoch or cur.lease_until is None or cur.lease_until <= now):
        raise StaleLease("step lease is no longer held", details={"step_id": step.step_id,
                                                                 "epoch": step.lease_epoch})
    slot = repo.get(ExecutionSlot, for_update=True, tenant_id=step.tenant_id, task_id=step.task_id)
    if slot is None or slot.holder_step_id != step.step_id or slot.lease_owner != step.lease_owner:
        raise StaleLease("execution slot is held by another step", details={"step_id": step.step_id})
    return cur


def renew(ctx: KernelContext, step: Step) -> Step:
    """Only the current owner + epoch with an unexpired lease may extend it."""
    with tx(ctx.store) as repo:
        cur = verify_for_commit(repo, step)
        until = repo.uow.db_now() + timedelta(seconds=ctx.config.lease_seconds)
        slot = repo.get(ExecutionSlot, for_update=True, tenant_id=step.tenant_id, task_id=step.task_id)
        repo.change(slot, lease_until=until)
        return repo.change(cur, lease_until=until)


def release_slot(repo: Repo, step: Step) -> None:
    slot = repo.get(ExecutionSlot, for_update=True, tenant_id=step.tenant_id, task_id=step.task_id)
    if slot is not None and slot.holder_step_id == step.step_id:
        repo.delete(slot, expect={"lease_epoch": slot.lease_epoch})
