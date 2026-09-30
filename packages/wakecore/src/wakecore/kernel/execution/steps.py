"""Run/Step creation and status helpers (RFC §5.3)."""
from datetime import datetime
from typing import Any, Optional

from ..domain.canonical import short_hash
from ..domain.enums import RunStatus, StepKind, StepStatus
from ..domain.model import Run, Step, TaskRuntime
from ..domain.statemachines import RUN, ensure
from ..repo import Repo


def run_id_for(tenant_id: str, *parts: str) -> str:
    return "run_" + short_hash(tenant_id, *parts)


def step_id_for(tenant_id: str, run_id: str, logical_step_id: str) -> str:
    return "stp_" + short_hash(tenant_id, run_id, logical_step_id)


def create_run(repo: Repo, rt: TaskRuntime, run_id: str, reason: str) -> bool:
    now = repo.uow.db_now()
    return repo.insert_if_absent(Run(
        tenant_id=rt.tenant_id, run_id=run_id, task_id=rt.task_id, root_task_id=rt.root_task_id, reason=reason,
        status=RunStatus.QUEUED, wait_reason=None, checkpoint={}, spec_version=rt.active_spec_version,
        revocation_epoch=rt.revocation_epoch, step_count=0, created_at=now, updated_at=now, finished_at=None))


def add_step(repo: Repo, run: Run, *, kind: StepKind, logical_step_id: str, input: dict[str, Any],
             available_at: Optional[datetime] = None, priority: int = 0, max_attempts: int = 5,
             max_steps: Optional[int] = None) -> Optional[Step]:
    """Idempotent on (run, logical_step_id). Returns None if the run's step limit is reached."""
    now = repo.uow.db_now()
    existing = repo.get(Step, tenant_id=run.tenant_id, step_id=step_id_for(run.tenant_id, run.run_id, logical_step_id))
    if existing is not None:
        return existing
    current = repo.get(Run, tenant_id=run.tenant_id, run_id=run.run_id) or run
    if max_steps is not None and current.step_count >= max_steps:
        return None
    step = Step(
        tenant_id=run.tenant_id, step_id=step_id_for(run.tenant_id, run.run_id, logical_step_id), run_id=run.run_id,
        task_id=run.task_id, logical_step_id=logical_step_id, kind=kind, status=StepStatus.READY, input=input,
        result=None, available_at=available_at or now, priority=priority, attempts=0, max_attempts=max_attempts,
        lease_owner=None, lease_epoch=0, lease_until=None, current_attempt_id=None, last_error=None,
        wait_reason=None, retry_owner="wakecore", created_at=now, updated_at=now)
    repo.insert(step)
    repo.change(current, step_count=current.step_count + 1, updated_at=now)
    return step


def set_step(repo: Repo, step: Step, to: StepStatus, **changes: Any) -> Step:
    ensure("step", step.status, to)
    return repo.change(step, expect={"status": step.status, "lease_epoch": step.lease_epoch}, status=to,
                       updated_at=repo.uow.db_now(), **changes)


def set_run(repo: Repo, run: Run, to: RunStatus, **changes: Any) -> Run:
    if run.status == to:
        return run
    ensure("run", run.status, to)
    now = repo.uow.db_now()
    if to in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
        changes.setdefault("finished_at", now)
    return repo.change(run, status=to, updated_at=now, **changes)


def settle_run_after_step(repo: Repo, run_id: str, tenant_id: str) -> Run:
    """After a step finishes: run succeeds when nothing is left, re-queues when READY work remains,
    waits when only WAITING work remains."""
    run = repo.get(Run, for_update=True, tenant_id=tenant_id, run_id=run_id)
    assert run is not None
    if run.status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
        return run
    steps = repo.find(Step, {"tenant_id": tenant_id, "run_id": run_id})
    statuses = {s.status for s in steps}
    if StepStatus.RUNNING in statuses:
        return run
    if StepStatus.READY in statuses:
        target = RunStatus.QUEUED
    elif StepStatus.WAITING in statuses:
        target = RunStatus.WAITING
    elif StepStatus.FAILED in statuses:
        target = RunStatus.FAILED
    elif statuses and statuses <= {StepStatus.CANCELLED}:
        target = RunStatus.CANCELLED
    else:
        target = RunStatus.SUCCEEDED
    for hop in _path(run.status, target):
        run = set_run(repo, run, hop, **({"wait_reason": None} if hop is RunStatus.QUEUED else {}))
    return run


def _path(src: RunStatus, dst: RunStatus) -> list[RunStatus]:
    """Shortest legal path through the Run state machine (e.g. QUEUED -> RUNNING -> SUCCEEDED)."""
    if src == dst:
        return []
    frontier, seen = [(src, [])], {src}
    while frontier:
        node, path = frontier.pop(0)
        for nxt in sorted(RUN[node]):
            if nxt in seen:
                continue
            if nxt == dst:
                return path + [nxt]
            seen.add(nxt)
            frontier.append((nxt, path + [nxt]))
    ensure("run", src, dst)
    return []
