"""Self-registered follow-up triggers (RFC §6.4, T13).

A FollowupProposal is only a proposal. Admission re-checks every root limit inside the
caller's transaction; nothing a model says can extend expiry, depth, count, scope,
capabilities or budget ownership.
"""
from datetime import timedelta
from typing import Any

from .. import audit
from ..context import KernelContext
from ..domain.canonical import short_hash
from ..domain.enums import CatchupPolicy, TaskLifecycle, TriggerKind, TriggerStatus
from ..domain.errors import PolicyDenied, TaskNotActive
from ..domain.model import Run, TaskRuntime, TriggerOccurrence, TriggerRecord
from ..domain.taskspec import TaskSpec
from ..policy.authority import scope_subset
from ..ports.reasoning import FollowupProposal
from ..repo import Repo


def followup_trigger_id(root_task_id: str, followup_key: str) -> str:
    return "trg:fu:" + short_hash(root_task_id, followup_key)


def admit_followup(repo: Repo, ctx: KernelContext, *, root: TaskRuntime, rt: TaskRuntime, root_spec: TaskSpec,
                   spec: TaskSpec, proposal: FollowupProposal, actor: str = "planner") -> TriggerRecord:
    """Caller holds root and task locks (kernel.locks.lock_task)."""
    now = repo.uow.db_now()
    if not spec.followup.enabled:
        raise PolicyDenied("follow-ups are not enabled for this task")
    if root.lifecycle is not TaskLifecycle.ACTIVE or rt.lifecycle is not TaskLifecycle.ACTIVE:
        raise TaskNotActive("root task is not executable")
    if proposal.root_task_id != rt.root_task_id:
        raise PolicyDenied("follow-up names a different root task")
    run = repo.get(Run, tenant_id=rt.tenant_id, run_id=proposal.originating_run_id)
    if run is None or run.task_id != rt.task_id:
        raise PolicyDenied("originating run does not belong to this task")
    if not proposal.followup_key or len(proposal.followup_key) > 128:
        raise PolicyDenied("followup_key is required")
    if proposal.source_ref is not None and proposal.source_ref != rt.source_ref:
        raise PolicyDenied("follow-up may not change the source")
    scope = proposal.resource_scope if proposal.resource_scope is not None else rt.effective_authority.get(
        "resource_scope", {})
    if not scope_subset(scope, root.effective_authority.get("resource_scope", {})):
        raise PolicyDenied("follow-up resource scope exceeds the root authority")
    if not set(proposal.capabilities) <= set(root.effective_authority.get("capabilities", [])):
        raise PolicyDenied("follow-up requests stronger capabilities than the root")
    if proposal.due_at > root.expires_at:
        raise PolicyDenied("follow-up may not be due after the root task expires")
    min_gap = max(root_spec.limits.min_probe_interval_seconds, spec.limits.min_probe_interval_seconds)
    if proposal.due_at < now + timedelta(seconds=min_gap):
        raise PolicyDenied("follow-up violates the minimum check interval")
    # Depth follows the chain: a run started by a follow-up trigger is itself at that depth.
    base = rt.depth
    for occ in repo.find(TriggerOccurrence, {"tenant_id": rt.tenant_id, "run_id": run.run_id}):
        origin = repo.get(TriggerRecord, tenant_id=rt.tenant_id, trigger_id=occ.trigger_id)
        if origin is not None:
            base = max(base, origin.depth)
    depth = base + 1
    if depth > root_spec.limits.max_followup_depth:
        raise PolicyDenied("follow-up depth limit exceeded")

    tid = followup_trigger_id(root.task_id, proposal.followup_key)
    existing = repo.get(TriggerRecord, tenant_id=rt.tenant_id, trigger_id=tid)
    if existing is not None:
        return existing  # the same logical follow-up is registered only once
    total = repo.count(TriggerRecord, {"tenant_id": rt.tenant_id, "root_task_id": root.task_id},
                       conds=[("followup_key", "not_null", None)])
    if total >= root_spec.limits.max_followup_count:
        raise PolicyDenied("follow-up count limit exceeded")

    trg = TriggerRecord(
        tenant_id=rt.tenant_id, trigger_id=tid, task_id=rt.task_id, root_task_id=root.task_id,
        kind=TriggerKind.ONCE, every_seconds=None, timezone=spec.trigger.timezone,
        catchup_policy=CatchupPolicy.COALESCE_LATEST, due_at=proposal.due_at, expiry=root.expires_at, generation=1,
        status=TriggerStatus.ACTIVE, predicate_ref=proposal.predicate_ref, depth=depth,
        origin_run_id=proposal.originating_run_id, followup_key=proposal.followup_key,
        reason=proposal.reason[:200], created_at=now, updated_at=now)
    if not repo.insert_if_absent(trg):
        return repo.find(TriggerRecord, {"tenant_id": rt.tenant_id, "root_task_id": root.task_id,
                                         "followup_key": proposal.followup_key}, limit=1)[0]
    audit.record(repo, ctx.ids, tenant_id=rt.tenant_id, kind="followup.admitted", actor=actor,
                 subject_type="trigger", subject_id=tid, task_id=rt.task_id, root_task_id=root.task_id,
                 reason=proposal.reason[:200], refs=_refs(proposal, depth))
    return trg


def _refs(p: FollowupProposal, depth: int) -> dict[str, Any]:
    return {"followup_key": p.followup_key, "due_at": p.due_at.isoformat(), "depth": depth,
            "originating_run_id": p.originating_run_id, "evidence_refs": list(p.evidence_refs),
            "predicate_ref": p.predicate_ref}
