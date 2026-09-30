"""Worker loop: scheduler tick -> recovery -> steps -> dispatch (RFC §12).

Every unit of work is claimed with a lease in its own transaction, so any number of
loops may run against the same PostgreSQL database. `run_until_idle` is what tests and
the CLI use; `serve` is the long-running form.
"""
import logging
import time
from typing import Any, Optional

from wakecore.kernel.actions.coordinator import dispatch_once
from wakecore.kernel.context import KernelContext
from wakecore.kernel.execution.executor import run_next
from wakecore.kernel.execution.recovery import recover
from wakecore.kernel.scheduling.scheduler import tick

log = logging.getLogger("wakecore.worker")


def _busy(recovered: dict[str, Any]) -> bool:
    total = 0
    for v in recovered.values():
        total += sum(v.values()) if isinstance(v, dict) else int(v)
    return total > 0


def step_once(ctx: KernelContext, worker_id: str, *, max_steps: int = 100) -> dict[str, int]:
    stats = {"fired": 0, "steps": 0, "dispatched": 0, "recovered": 0}
    stats["fired"] = len(tick(ctx))
    rec = recover(ctx)
    stats["recovered"] = int(_busy({k: v for k, v in rec.items() if k != "reconcile"}) or
                             rec["reconcile"]["confirmed"] + rec["reconcile"]["no_effect"] > 0)
    for _ in range(max_steps):
        if run_next(ctx, worker_id) is None:
            break
        stats["steps"] += 1
    for _ in range(max_steps):
        if dispatch_once(ctx, worker_id) is None:
            break
        stats["dispatched"] += 1
    return stats


def run_until_idle(ctx: KernelContext, worker_id: str = "worker-1", *, max_rounds: int = 50) -> list[dict[str, int]]:
    rounds = []
    for _ in range(max_rounds):
        stats = step_once(ctx, worker_id)
        rounds.append(stats)
        if not any(stats.values()):
            break
    return rounds


def serve(ctx: KernelContext, worker_id: str, *, idle_sleep: float = 1.0, stop: Optional[Any] = None) -> None:
    while stop is None or not stop.is_set():
        try:
            stats = step_once(ctx, worker_id)
        except Exception:  # noqa: BLE001 - keep the worker alive; leases make work recoverable
            log.exception("worker round failed")
            stats = {}
        if not any(stats.values()):
            time.sleep(idle_sleep)
