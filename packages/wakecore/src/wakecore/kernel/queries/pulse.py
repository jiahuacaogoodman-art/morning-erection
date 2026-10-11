"""Source-neutral Pulse projection over durable records, not frontend timers."""
from ..commands.tasks import load_spec, scope_key_for
from ..decision.profiles import condition_holds
from ..domain.enums import ApprovalDecision
from ..domain.model import (
    ActionRecord,
    Approval,
    DecisionRecord,
    EventRecord,
    ObservationRecord,
    SourceCheckpoint,
    Step,
    TaskRuntime,
    TriggerRecord,
)


def project(repo, ctx, principal, task):
    key = {"tenant_id": principal.tenant_id, "task_id": task["task_id"]}
    rt = repo.get(TaskRuntime, **key)
    spec = load_spec(repo, rt)
    scope = spec.source.resource_scope
    cp = repo.get(SourceCheckpoint, tenant_id=principal.tenant_id, source_ref=rt.source_ref,
                  scope_key=scope_key_for(spec))
    observations = repo.find(ObservationRecord, key, order_by=("-observed_at", "-observation_id"), limit=1)
    latest = observations[0] if observations else None
    trigger = repo.get(TriggerRecord, tenant_id=principal.tenant_id, trigger_id=f"trg:{rt.task_id}:main")
    events = repo.find(EventRecord, {"tenant_id": principal.tenant_id, "source_ref": rt.source_ref,
                                   "internal": True}, order_by=("-seq",), limit=40)
    events = [e for e in events if e.data.get("task_id") == rt.task_id]
    decisions = repo.find(DecisionRecord, key, conds=[("event_id", "in", [e.event_id for e in events])]) if events else []
    final_decisions = {}
    for d in decisions:
        previous = final_decisions.get(d.event_id)
        if previous is None or (d.model_call_ref and not previous.model_call_ref):
            final_decisions[d.event_id] = d
    meaningful_events = {eid for eid, d in final_decisions.items() if d.route not in ("NOOP", "JUDGE")}
    actions = repo.find(ActionRecord, key, order_by=("-created_at", "-action_id"), limit=40)
    running = repo.find(Step, key, conds=[("status", "in", ["RUNNING", "READY"])], limit=8)
    deferred = repo.find(Step, {**key, "status": "WAITING", "wait_reason": "budget"}, limit=1)
    writes = [a for a in actions if a.capability != ctx.config.notify_capability]
    now = repo.uow.db_now()
    approvals = [a for a in task["pending_approvals"]
                 if not a["expires_at"] or _date(a["expires_at"]) > now]
    expired = any(a.resolution == "approval_expired" for a in actions) or bool(repo.find(
        Approval, {**key, "decision": ApprovalDecision.PENDING}, conds=[("expires_at", "<=", now)], limit=1))
    held = cp.trusted_snapshot if cp and cp.trusted_snapshot is not None else {}
    records = {k: v for k, v in held.items() if not spec.target_resources or k in spec.target_resources}
    params = spec.decision_params
    matched = any(any(condition_holds(c, r) for c in params.get("conditions", [])) for r in records.values())
    attention = None
    if rt.lifecycle in ("COMPLETED", "CANCELLED", "EXPIRED"):
        state = str(rt.lifecycle).lower()
    elif rt.lifecycle == "PAUSED":
        state = "paused"
    elif latest is None or (cp is None and task["source"]["health"] == "HEALTHY"):
        state = "connecting" if rt.lifecycle == "ACTIVE" else "draft"
    elif task["source"]["health"] in ("AUTH_REQUIRED", "SCHEMA_INVALID", "DEGRADED"):
        state = "attention"
        attention = {"AUTH_REQUIRED": "登录已失效，请重新登录后确认", "SCHEMA_INVALID": "页面内容无法读取，需要重新选择观察内容",
                     "DEGRADED": "没有完整读到目标内容，请检查页面"}[task["source"]["health"]]
    elif any(a.status == "UNKNOWN" for a in actions):
        state, attention = "attention", "提交结果尚未确认，正在核实；不会重复提交"
    elif approvals:
        state = "approval"
    elif expired:
        state, attention = "attention", "这次操作的审批已到期，没有提交。需要重新确认驻守计划"
    elif deferred:
        state, attention = "deferred", "检测到变化，本期操作预算已用完，正在等待下一个执行时段"
    elif writes and writes[0].status in ("FAILED_NO_EFFECT", "DENIED") and writes[0].resolution != "rejected_by_user":
        state, attention = "attention", "上一项操作没有完成，请确认页面或操作权限；结果尚未确认"
    elif any(s.kind in ("PLAN", "JUDGE", "DISPATCH") for s in running) or any(a.status in ("READY", "DISPATCHING") for a in writes):
        state = "working"
    elif task["source"]["health"] == "UNAVAILABLE":
        state = "reconnecting"
    else:
        state = "watching"
    # A successful historical value remains historical while the connection is blocked.
    age = max(0, int((now - cp.trusted_at).total_seconds())) if cp and cp.trusted_at else None
    stale = bool(age is not None and (state in ("attention", "reconnecting", "paused") or
                                    age > max(30, (trigger.every_seconds if trigger else 60) * 2)))
    changes = []
    for ev in reversed(events):
        if ev.event_id not in meaningful_events:
            continue
        changes.append({"id": ev.event_id, "at": ev.received_at, "changes": ev.data.get("changes", [])})
    history = [{"id": f"start:{rt.task_id}", "at": rt.created_at, "role": "user", "type": "request", "text": spec.purpose}]
    history += [{"id": e["id"], "at": e["at"], "role": "agent", "type": "change", "changes": e["changes"]} for e in changes]
    for action in reversed(actions):
        history.append({"id": action.action_id + ":" + str(action.status), "at": action.updated_at,
                        "role": "agent", "type": "action", "status": str(action.status),
                        "tool_id": action.tool_id, "text": action.canonical_payload.get("goal") or
                        action.canonical_payload.get("title") or "处理变化", "resolution": action.resolution})
    # A fixed/test clock or a coarse database clock can give a cause and result the same
    # timestamp. Show the observed change before its action result in that case.
    history.sort(key=lambda m: (m["at"], {"request": 0, "change": 1, "action": 2}[m["type"]], m["id"]))
    return {**task, "kind": scope.get("kind", "web"), "label": scope.get("label") or spec.purpose[:32], "url": scope.get("url"),
            "state": state, "attention": attention, "records": records, "condition_met": matched,
            "field": scope.get("pulse_field", "value"), "field_label": scope.get("pulse_field_label", "内容"),
            "condition_label": scope.get("condition_label", "内容发生变化"),
            "last_confirmed_at": cp.trusted_at if cp else None, "last_attempt_at": latest.observed_at if latest else None,
            "stale": stale, "age_seconds": age, "interval_seconds": trigger.every_seconds if trigger else None,
            "polling_reason": trigger.reason if trigger else None, "polling_suspended": bool(trigger and trigger.status == "PAUSED"),
            "pending_approvals": approvals, "approval_expired": bool(expired), "changes": changes,
            "next_action_at": deferred[0].available_at if deferred else None,
            "history": history[-60:], "configuration": {"extractor": scope.get("extractor"),
                                                         "polling": scope.get("polling"), "spec": spec.to_dict()}}


def _date(value):
    from ..domain.taskspec import parse_ts

    return parse_ts(value, "approval.expires_at")
