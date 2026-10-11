"""Opt-in cadence extension; never observes or changes occurrence/lease semantics (ADR 0009)."""
import hashlib
import math
from datetime import timedelta
from typing import Any

from .. import audit
from ..domain.enums import ObservationOutcome, TaskLifecycle, TriggerKind, TriggerStatus
from ..domain.errors import SchemaMismatch
from ..domain.model import SourceBinding, TriggerRecord


def polling_policy(scope: dict[str, Any], *, minimum: int, rate_limit: int = 0) -> dict[str, int] | None:
    raw = scope.get("polling")
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"min_seconds", "max_seconds", "error_max_seconds", "jitter_percent"}:
        raise SchemaMismatch("source.resource_scope.polling has invalid fields")
    floor = max(1, minimum, math.ceil(60 / rate_limit) if rate_limit else 1)
    values = {"min_seconds": floor, "max_seconds": max(60, floor),
              "error_max_seconds": max(900, floor), "jitter_percent": 10, **raw}
    for key, value in values.items():
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 86400:
            raise SchemaMismatch(f"polling.{key} must be an integer in 0..86400")
    if not floor <= values["min_seconds"] <= values["max_seconds"] <= values["error_max_seconds"]:
        raise SchemaMismatch("polling intervals must respect the task and connector floor: min <= max <= error_max")
    if values["jitter_percent"] > 25:
        raise SchemaMismatch("polling.jitter_percent must be in 0..25")
    return values


def apply_polling(repo: Any, ctx: Any, rt: Any, spec: Any, step: Any, obs: Any, transition: Any,
                  previous_snapshot: Any) -> None:
    # Source lookup stays in this transaction; the task aggregate was locked by the caller.
    binding = repo.get(SourceBinding, tenant_id=rt.tenant_id, source_ref=rt.source_ref)
    connector = ctx.connectors.get(binding.connector_id) if binding else None
    policy = polling_policy(spec.source.resource_scope, minimum=spec.limits.min_probe_interval_seconds,
                            rate_limit=connector.descriptor.rate_limit_per_minute if connector else 0)
    if policy is None or rt.lifecycle is not TaskLifecycle.ACTIVE:
        return
    trigger = repo.get(TriggerRecord, for_update=True, tenant_id=rt.tenant_id,
                       trigger_id=f"trg:{rt.task_id}:main")
    if trigger is None or trigger.kind is not TriggerKind.INTERVAL or trigger.status is not TriggerStatus.ACTIVE:
        return
    now = repo.uow.db_now()
    if obs.outcome in (ObservationOutcome.AUTH_REQUIRED, ObservationOutcome.SCHEMA_INVALID):
        reason = f"adaptive:{str(obs.outcome).lower()}"
        repo.change(trigger, status=TriggerStatus.PAUSED, due_at=None, reason=reason, updated_at=now)
        audit.record(repo, ctx.ids, tenant_id=rt.tenant_id, kind="polling.suspended", actor="worker",
                     subject_type="trigger", subject_id=trigger.trigger_id, task_id=rt.task_id,
                     root_task_id=rt.root_task_id, reason=reason)
        return
    current = max(policy["min_seconds"], trigger.every_seconds or policy["min_seconds"])
    if transition.trusted:
        changed = previous_snapshot is None or previous_snapshot != transition.snapshot
        interval = policy["min_seconds"] if changed else min(policy["max_seconds"], math.ceil(current * 1.5))
        reason = "adaptive:changed" if changed else "adaptive:quiet"
    else:
        interval = min(policy["error_max_seconds"], current * 2)
        reason = "adaptive:" + (obs.error_code or "unavailable")
    # Retry-After can exceed the configured backoff cap; a server's lower bound wins.
    retry = obs.retry_after_seconds if isinstance(obs.retry_after_seconds, int) else 0
    if retry > 0:
        reason = "adaptive:" + (obs.error_code if (obs.error_code or "").startswith("http_") else "http_retry_after")
    interval = max(interval, retry or 0)
    seed = int(hashlib.sha256(f"{rt.task_id}:{step.step_id}".encode()).hexdigest()[:8], 16)
    jitter_cap = interval * policy["jitter_percent"] // 100
    delay = interval + seed % (jitter_cap + 1)
    repo.change(trigger, every_seconds=interval, due_at=now + timedelta(seconds=delay), reason=reason, updated_at=now)
    audit.record(repo, ctx.ids, tenant_id=rt.tenant_id, kind="polling.scheduled", actor="worker",
                 subject_type="trigger", subject_id=trigger.trigger_id, task_id=rt.task_id,
                 root_task_id=rt.root_task_id, reason=reason,
                 refs={"interval_seconds": interval, "delay_seconds": delay})
