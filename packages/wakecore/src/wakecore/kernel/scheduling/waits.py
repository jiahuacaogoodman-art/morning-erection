"""Wait registration without lost wake-ups (RFC §6.5, T14).

A wait stores its match condition *and* an event watermark in the same short transaction
that parks the step. The watermark is the one the step captured when it *read* business
state (`basis_watermark`), not the one current at registration: an event that lands
between that read and the registration therefore still matches. Resolution scans events
after the watermark. Timeouts never assume the
awaited thing did not happen: they resume the step to re-check current business state.
"""
from datetime import datetime
from typing import Any, Optional

from ..context import KernelContext
from ..domain.enums import RunStatus, StepStatus, TaskLifecycle, WaitReason, WaitStatus
from ..domain.model import EventRecord, Run, Step, TaskRuntime, WaitRecord
from ..execution.steps import set_run, set_step
from ..repo import Repo


def event_watermark(repo: Repo, tenant_id: str) -> int:
    last = repo.find(EventRecord, {"tenant_id": tenant_id}, order_by=("-seq",), limit=1)
    return last[0].seq if last else 0


def register_wait(repo: Repo, ctx: KernelContext, step: Step, *, reason: WaitReason, match: dict[str, Any],
                  due_at: Optional[datetime], result: Optional[dict[str, Any]] = None,
                  basis_watermark: Optional[int] = None) -> WaitRecord:
    """Caller holds the step lease and the task lock; the step goes RUNNING -> WAITING.

    `basis_watermark` is the event watermark observed when the caller read the state it is
    waiting on; without it the registration-time watermark is used (fine for waits with no
    event match, e.g. budget).
    """
    now = repo.uow.db_now()
    current = event_watermark(repo, step.tenant_id)
    basis = current if basis_watermark is None else min(basis_watermark, current)
    wait = WaitRecord(
        tenant_id=step.tenant_id, wait_id=ctx.ids.new("wait"), run_id=step.run_id, step_id=step.step_id,
        task_id=step.task_id, reason=reason, match=match, basis_watermark=basis,
        due_at=due_at, status=WaitStatus.WAITING, matched_event_id=None, created_at=now, resolved_at=None)
    repo.insert(wait)
    set_step(repo, step, StepStatus.WAITING, wait_reason=reason, lease_owner=None, lease_until=None,
             result=result)
    return wait


def _matches(ev: EventRecord, match: dict[str, Any]) -> bool:
    if match.get("source_ref") and ev.source_ref != match["source_ref"]:
        return False
    if match.get("type") and ev.type != match["type"]:
        return False
    if match.get("subject") and ev.subject != match["subject"]:
        return False
    if match.get("exclude_causation_of") and ev.causation_id == match["exclude_causation_of"]:
        return False  # our own effects do not satisfy a wait for somebody else's reply
    return True


def _wake(repo: Repo, wait: WaitRecord, status: WaitStatus, *, event_id: Optional[str]) -> None:
    now = repo.uow.db_now()
    repo.change(wait, expect={"status": WaitStatus.WAITING}, status=status, matched_event_id=event_id,
                resolved_at=now)
    step = repo.get(Step, for_update=True, tenant_id=wait.tenant_id, step_id=wait.step_id)
    if step is None or step.status is not StepStatus.WAITING:
        return
    rt = repo.get(TaskRuntime, tenant_id=wait.tenant_id, task_id=wait.task_id)
    paused = rt is not None and rt.lifecycle is TaskLifecycle.PAUSED
    from ..lifecycle import PAUSED_UNTIL

    set_step(repo, step, StepStatus.READY, wait_reason=None, available_at=PAUSED_UNTIL if paused else now,
             input={**step.input, "wake": {"wait_id": wait.wait_id, "status": str(status),
                                           "reason": str(wait.reason), "event_id": event_id}})
    run = repo.get(Run, for_update=True, tenant_id=wait.tenant_id, run_id=wait.run_id)
    if run is not None and run.status is RunStatus.WAITING:
        # Time spent parked is not busy time: max_run_seconds counts from here again.
        set_run(repo, run, RunStatus.QUEUED, wait_reason=None,
                checkpoint={**(run.checkpoint or {}), "active_since": now.isoformat()})


def resolve_waits(repo: Repo, *, limit: int = 100) -> dict[str, int]:
    now = repo.uow.db_now()
    counts = {"matched": 0, "timed_out": 0}
    for wait in repo.find(WaitRecord, {"status": WaitStatus.WAITING}, order_by=("created_at", "wait_id"),
                          limit=limit):
        hit = None
        if wait.match:
            for ev in repo.find(EventRecord, {"tenant_id": wait.tenant_id}, conds=[("seq", ">", wait.basis_watermark)],
                                order_by=("seq",), limit=200):
                if _matches(ev, wait.match):
                    hit = ev
                    break
        if hit is not None:
            _wake(repo, wait, WaitStatus.MATCHED, event_id=hit.event_id)
            counts["matched"] += 1
        elif wait.due_at is not None and wait.due_at <= now:
            _wake(repo, wait, WaitStatus.TIMED_OUT, event_id=None)
            counts["timed_out"] += 1
    return counts
