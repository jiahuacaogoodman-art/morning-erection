"""Task control commands (RFC §5.1, §9.3, §15). All run inside the caller's transaction.

Lock order: root runtime -> task runtime -> children (see kernel.locks).
"""
from typing import Any, Optional

from .. import audit
from ..context import KernelContext
from ..decision.profiles import validate_decision
from ..domain.canonical import short_hash
from ..domain.enums import (
    TERMINAL_LIFECYCLES,
    SpecStatus,
    StepKind,
    StepStatus,
    TaskLifecycle,
    TriggerKind,
    TriggerStatus,
)
from ..domain.errors import (
    InvalidTransition,
    NotFound,
    PolicyDenied,
    SchemaMismatch,
    TaskExpired,
    TaskNotActive,
    VersionConflict,
)
from ..domain.model import Grant, SourceBinding, Step, TaskRuntime, TaskSpecRecord, TriggerRecord
from ..domain.taskspec import TaskSpec, parse_task_spec
from ..execution.steps import set_step, settle_run_after_step
from ..lifecycle import PAUSED_UNTIL, in_flight, set_lifecycle, terminate
from ..locks import lock_task
from ..policy.authority import check_grant, compute_effective_authority, scope_subset
from ..repo import Repo


def main_trigger_id(task_id: str) -> str:
    return f"trg:{task_id}:main"


def scope_key_for(spec: TaskSpec) -> str:
    from ..domain.canonical import canonical_json

    return f"{spec.task_id}:{short_hash(canonical_json(spec.source.resource_scope), length=12)}"


def load_spec(repo: Repo, rt: TaskRuntime, version: Optional[int] = None) -> TaskSpec:
    rec = repo.get(TaskSpecRecord, tenant_id=rt.tenant_id, task_id=rt.task_id,
                   spec_version=version or rt.active_spec_version)
    if rec is None:
        raise NotFound(f"spec {rt.task_id} v{version or rt.active_spec_version}")
    return parse_task_spec(rec.body, tenant_id=rt.tenant_id)


def _owner(repo: Repo, rt: TaskRuntime) -> str:
    rec = repo.get(TaskSpecRecord, tenant_id=rt.tenant_id, task_id=rt.task_id, spec_version=rt.active_spec_version)
    return rec.created_by if rec else ""


def require_owner(repo: Repo, rt: TaskRuntime, principal: str) -> None:
    if _owner(repo, rt) != principal:
        raise PolicyDenied("principal is not the task owner")


def _check_version(rt: TaskRuntime, expected_version: Optional[int]) -> None:
    if expected_version is not None and rt.version != expected_version:
        raise VersionConflict("task version changed", details={"expected": expected_version, "actual": rt.version})


def create_draft(repo: Repo, ctx: KernelContext, *, tenant_id: str, principal: str,
                 raw: dict[str, Any]) -> TaskSpecRecord:
    """POST /v1/tasks: store a DRAFT. Never activates anything by itself."""
    spec = parse_task_spec(raw, tenant_id=tenant_id)
    if spec.decision is not None:
        validate_decision(spec.decision_profile, spec.decision_params,
                          (spec.completion.evaluator, spec.completion.evaluator_version))
    now = repo.uow.db_now()
    binding = repo.get(SourceBinding, tenant_id=tenant_id, source_ref=spec.source.binding_ref)
    if binding is None:  # includes another tenant's binding id (T20)
        raise NotFound(f"source binding {spec.source.binding_ref}")
    if binding.owner != principal:
        raise PolicyDenied("source binding belongs to another principal")
    if spec.expires_at <= now:
        raise TaskExpired("expires_at is in the past")

    if spec.parent_task_id is None:
        if spec.root_task_id != spec.task_id or spec.depth != 0:
            raise SchemaMismatch("a root task must have root_task_id == task_id and depth 0")
    else:
        _check_child(repo, spec)

    existing = repo.get(TaskRuntime, for_update=True, tenant_id=tenant_id, task_id=spec.task_id)
    if existing is not None:
        if existing.lifecycle in TERMINAL_LIFECYCLES:
            raise TaskNotActive("task is terminal; create a new task id")
        if _owner(repo, existing) != principal:
            raise PolicyDenied("principal is not the task owner")
        if existing.root_task_id != spec.root_task_id or existing.parent_task_id != spec.parent_task_id:
            raise SchemaMismatch("task lineage cannot change between versions")
        latest = repo.find(TaskSpecRecord, {"tenant_id": tenant_id, "task_id": spec.task_id},
                           order_by=("-spec_version",), limit=1)
        if latest and spec.spec_version <= latest[0].spec_version:
            same = latest[0].spec_digest == spec.digest() and latest[0].spec_version == spec.spec_version
            if same:
                return latest[0]
            raise VersionConflict("spec_version must increase", details={"latest": latest[0].spec_version})

    rec = TaskSpecRecord(tenant_id=tenant_id, task_id=spec.task_id, spec_version=spec.spec_version,
                         root_task_id=spec.root_task_id, spec_digest=spec.digest(), body=spec.to_dict(),
                         status=SpecStatus.DRAFT, created_at=now, created_by=principal, confirmed_at=None,
                         confirmed_by=None)
    repo.insert(rec)
    if existing is None:
        repo.insert(TaskRuntime(
            tenant_id=tenant_id, task_id=spec.task_id, root_task_id=spec.root_task_id,
            parent_task_id=spec.parent_task_id, depth=spec.depth, lifecycle=TaskLifecycle.DRAFT,
            active_spec_version=spec.spec_version, version=1, revocation_epoch=0,
            source_ref=spec.source.binding_ref, grant_ref=spec.authority.grant_ref, grant_version=0,
            effective_authority={}, expires_at=spec.expires_at, lifecycle_reason="draft", last_progress_at=None,
            created_at=now, updated_at=now))
    audit.record(repo, ctx.ids, tenant_id=tenant_id, kind="task.draft", actor=principal, subject_type="task",
                 subject_id=spec.task_id, task_id=spec.task_id, root_task_id=spec.root_task_id,
                 to_state="DRAFT", refs={"spec_version": spec.spec_version, "spec_digest": rec.spec_digest})
    return rec


def _check_child(repo: Repo, spec: TaskSpec) -> None:
    """Child tasks can never escape the root's expiry, depth, count or authority (T13)."""
    parent = repo.get(TaskRuntime, tenant_id=spec.tenant_id, task_id=spec.parent_task_id)
    if parent is None:
        raise NotFound(f"parent task {spec.parent_task_id}")
    root = repo.get(TaskRuntime, for_update=True, tenant_id=spec.tenant_id, task_id=parent.root_task_id)
    if root is None or spec.root_task_id != parent.root_task_id:
        raise PolicyDenied("child must share the parent's root task")
    if root.lifecycle not in (TaskLifecycle.ACTIVE, TaskLifecycle.PAUSED):
        raise TaskNotActive("root task is not active")
    root_spec = load_spec(repo, root)
    if spec.depth != parent.depth + 1 or spec.depth > root_spec.limits.max_followup_depth:
        raise PolicyDenied("followup depth limit exceeded")
    if spec.expires_at > root.expires_at:
        raise PolicyDenied("child may not outlive the root task")
    children = repo.count(TaskRuntime, {"tenant_id": spec.tenant_id, "root_task_id": root.task_id},
                          conds=[("depth", ">", 0)])
    if children >= root_spec.limits.max_followup_count:
        raise PolicyDenied("followup count limit exceeded")
    requested = spec.authority.requested_capabilities()
    if not requested <= set(parent.effective_authority.get("capabilities", [])):
        raise PolicyDenied("child requests capabilities its parent does not hold")
    if not set(spec.authority.model_data_egress) <= set(parent.effective_authority.get("data_egress", [])):
        raise PolicyDenied("child requests data egress its parent does not hold")
    if not scope_subset(spec.source.resource_scope, parent.effective_authority.get("resource_scope", {})):
        raise PolicyDenied("child resource scope exceeds the parent")


def activate(repo: Repo, ctx: KernelContext, *, tenant_id: str, principal: str, task_id: str,
             spec_version: Optional[int] = None, expected_digest: Optional[str] = None,
             expected_version: Optional[int] = None) -> TaskRuntime:
    """T1: confirmed spec + grant version + initial trigger + audit, atomically."""
    root, rt = lock_task(repo, tenant_id, task_id)
    _check_version(rt, expected_version)
    if rt.lifecycle in TERMINAL_LIFECYCLES or rt.lifecycle is TaskLifecycle.DRAINING:
        raise TaskNotActive(f"task is {rt.lifecycle}")
    if spec_version is None:
        latest = repo.find(TaskSpecRecord, {"tenant_id": tenant_id, "task_id": task_id},
                           order_by=("-spec_version",), limit=1)
        spec_version = latest[0].spec_version
    rec = repo.get(TaskSpecRecord, for_update=True, tenant_id=tenant_id, task_id=task_id, spec_version=spec_version)
    if rec is None:
        raise NotFound(f"spec version {spec_version}")
    if rec.created_by != principal:
        raise PolicyDenied("principal is not the task owner")
    if expected_digest is not None and expected_digest != rec.spec_digest:
        raise VersionConflict("spec digest does not match the confirmed content")
    if rec.status is SpecStatus.CONFIRMED and rt.active_spec_version == spec_version and \
            rt.lifecycle is not TaskLifecycle.DRAFT:
        return rt  # already active with this version
    if rec.status is not SpecStatus.DRAFT:
        raise InvalidTransition(f"spec is {rec.status}")
    spec = parse_task_spec(rec.body, tenant_id=tenant_id)
    now = repo.uow.db_now()
    if spec.expires_at <= now:
        raise TaskExpired("task expired before activation")

    grant = check_grant(repo.get(Grant, tenant_id=tenant_id, grant_ref=spec.authority.grant_ref),
                        tenant_id=tenant_id, now=now)
    if grant.principal != principal:
        raise PolicyDenied("grant belongs to another principal")
    binding = repo.get(SourceBinding, tenant_id=tenant_id, source_ref=spec.source.binding_ref)
    if binding is None:
        raise NotFound("source binding")
    connector = ctx.connectors.get(binding.connector_id)
    if connector is None:
        raise PolicyDenied("connector is not installed")
    if binding.resource_scope and not scope_subset(spec.source.resource_scope, binding.resource_scope):
        raise PolicyDenied("resource scope exceeds the source binding")
    connector_caps = set(binding.capabilities) & {connector.descriptor.read_capability}
    authority = compute_effective_authority(
        grant=grant, requested_capabilities=spec.authority.requested_capabilities(),
        requested_egress=spec.authority.model_data_egress, resource_scope=spec.source.resource_scope,
        connector_capabilities=connector_caps, system_capabilities=ctx.system_policy.allowed_capabilities,
        system_egress=ctx.system_policy.allowed_data_egress,
        root_authority=None if root.task_id == rt.task_id else root.effective_authority)
    missing = set(spec.authority.read_capabilities) - set(authority["capabilities"])
    if missing:
        raise PolicyDenied("required read capabilities are not authorised", details={"missing": sorted(missing)})

    for old in repo.find(TaskSpecRecord, {"tenant_id": tenant_id, "task_id": task_id,
                                          "status": SpecStatus.CONFIRMED}):
        repo.change(old, status=SpecStatus.SUPERSEDED)
    repo.change(rec, status=SpecStatus.CONFIRMED, confirmed_at=now, confirmed_by=principal)

    was = rt.lifecycle
    rt = repo.change(rt, expect={"version": rt.version}, version=rt.version + 1, active_spec_version=spec_version,
                     grant_ref=grant.grant_ref, grant_version=grant.version, effective_authority=authority,
                     expires_at=spec.expires_at, updated_at=now)
    if was is TaskLifecycle.DRAFT:
        rt = set_lifecycle(repo, ctx.ids, rt, TaskLifecycle.ACTIVE, actor=principal, reason="activated")

    _install_main_trigger(repo, rt, spec, now, paused=rt.lifecycle is TaskLifecycle.PAUSED)
    audit.record(repo, ctx.ids, tenant_id=tenant_id, kind="task.activated", actor=principal, subject_type="task",
                 subject_id=task_id, task_id=task_id, root_task_id=rt.root_task_id, from_state=was,
                 to_state=rt.lifecycle, refs={"spec_version": spec_version, "spec_digest": rec.spec_digest,
                                              "grant_version": grant.version, "authority": authority})
    return rt


def _install_main_trigger(repo: Repo, rt: TaskRuntime, spec: TaskSpec, now, *, paused: bool) -> TriggerRecord:
    tid = main_trigger_id(rt.task_id)
    due = now if spec.trigger.kind is TriggerKind.INTERVAL else spec.trigger.due_at
    status = TriggerStatus.PAUSED if paused else TriggerStatus.ACTIVE
    existing = repo.get(TriggerRecord, for_update=True, tenant_id=rt.tenant_id, trigger_id=tid)
    fields = dict(kind=spec.trigger.kind, every_seconds=spec.trigger.every_seconds, timezone=spec.trigger.timezone,
                  catchup_policy=spec.trigger.catchup_policy, due_at=due, expiry=spec.expires_at, status=status,
                  reason="configured", updated_at=now)
    if existing is not None:
        # Reconfiguration: old-generation work stays auditable but can no longer start.
        return repo.change(existing, generation=existing.generation + 1, **fields)
    trg = TriggerRecord(tenant_id=rt.tenant_id, trigger_id=tid, task_id=rt.task_id, root_task_id=rt.root_task_id,
                        generation=1, predicate_ref=None, depth=rt.depth, origin_run_id=None, followup_key=None,
                        created_at=now, **fields)
    repo.insert(trg)
    return trg


def pause(repo: Repo, ctx: KernelContext, *, tenant_id: str, principal: str, task_id: str,
          expected_version: Optional[int] = None) -> dict[str, Any]:
    _, rt = lock_task(repo, tenant_id, task_id)
    require_owner(repo, rt, principal)
    _check_version(rt, expected_version)
    if rt.lifecycle is TaskLifecycle.PAUSED:
        return _control_result(repo, rt)
    rt = set_lifecycle(repo, ctx.ids, rt, TaskLifecycle.PAUSED, actor=principal, reason="paused_by_user")
    now = repo.uow.db_now()
    key = {"tenant_id": tenant_id, "task_id": task_id}
    for t in repo.find(TriggerRecord, {**key, "status": TriggerStatus.ACTIVE}):
        repo.change(t, status=TriggerStatus.PAUSED, reason="task_paused", updated_at=now)
    runs = set()
    for s in repo.find(Step, {**key, "status": StepStatus.READY}):
        if s.kind is StepKind.PROBE:  # a fresh probe is scheduled on resume
            set_step(repo, s, StepStatus.CANCELLED, last_error="task_paused")
            runs.add(s.run_id)
        else:  # event work is kept, never dropped
            repo.change(s, available_at=PAUSED_UNTIL, updated_at=now)
    for run_id in sorted(runs):
        settle_run_after_step(repo, run_id, tenant_id)
    return _control_result(repo, rt)


def resume(repo: Repo, ctx: KernelContext, *, tenant_id: str, principal: str, task_id: str,
           expected_version: Optional[int] = None) -> dict[str, Any]:
    root, rt = lock_task(repo, tenant_id, task_id)
    require_owner(repo, rt, principal)
    _check_version(rt, expected_version)
    if rt.lifecycle is not TaskLifecycle.PAUSED:
        raise InvalidTransition(f"task is {rt.lifecycle}")
    now = repo.uow.db_now()
    if rt.expires_at <= now:
        terminate(repo, ctx.ids, rt, TaskLifecycle.EXPIRED, actor="kernel", reason="expired")
        raise TaskExpired("task expired while paused")
    grant = repo.get(Grant, tenant_id=tenant_id, grant_ref=rt.grant_ref)
    check_grant(grant, tenant_id=tenant_id, now=now)
    if grant.version != rt.grant_version:
        raise PolicyDenied("grant changed while paused; re-activate to confirm the new authority")
    if root.task_id != rt.task_id and root.lifecycle is not TaskLifecycle.ACTIVE:
        raise TaskNotActive("root task is not active")
    rt = set_lifecycle(repo, ctx.ids, rt, TaskLifecycle.ACTIVE, actor=principal, reason="resumed_by_user")
    key = {"tenant_id": tenant_id, "task_id": task_id}
    for t in repo.find(TriggerRecord, {**key, "status": TriggerStatus.PAUSED}):
        # Missed slots are handled by the scheduler according to catchup_policy (RFC §6.3).
        repo.change(t, status=TriggerStatus.ACTIVE, reason="task_resumed", updated_at=now)
    for s in repo.find(Step, {**key, "status": StepStatus.READY, "available_at": PAUSED_UNTIL}):
        repo.change(s, available_at=now, updated_at=now)
    return _control_result(repo, rt)


def cancel(repo: Repo, ctx: KernelContext, *, tenant_id: str, principal: str, task_id: str,
           reason: str = "cancelled_by_user", expected_version: Optional[int] = None) -> dict[str, Any]:
    """T7. The response states how many effects are still in flight (RFC §9.3)."""
    _, rt = lock_task(repo, tenant_id, task_id)
    require_owner(repo, rt, principal)
    _check_version(rt, expected_version)
    if rt.lifecycle is TaskLifecycle.CANCELLED:
        return _control_result(repo, rt)
    if rt.lifecycle in TERMINAL_LIFECYCLES:
        raise InvalidTransition(f"task is already {rt.lifecycle}")
    rt, counts = terminate(repo, ctx.ids, rt, TaskLifecycle.CANCELLED, actor=principal, reason=reason)
    cascaded = []
    frontier = [rt.task_id]
    while frontier:
        parent_id = frontier.pop(0)
        for child in repo.find(TaskRuntime, {"tenant_id": tenant_id, "parent_task_id": parent_id},
                               order_by=("task_id",), for_update=True):
            frontier.append(child.task_id)
            if child.lifecycle in TERMINAL_LIFECYCLES:
                continue
            _, c = terminate(repo, ctx.ids, child, TaskLifecycle.CANCELLED, actor=principal,
                             reason="parent_cancelled")
            counts["in_flight"] += c["in_flight"]
            cascaded.append(child.task_id)
    out = _control_result(repo, rt)
    out["in_flight"] = counts["in_flight"]
    out["invalidated"] = counts
    out["cascaded"] = cascaded
    return out


def _control_result(repo: Repo, rt: TaskRuntime) -> dict[str, Any]:
    n = in_flight(repo, rt)
    msg = "已停止新动作" + (f"；仍有 {n} 个在途/结果待确认" if n else "")
    return {"task_id": rt.task_id, "lifecycle": str(rt.lifecycle), "version": rt.version,
            "revocation_epoch": rt.revocation_epoch, "in_flight": n, "message": msg}
