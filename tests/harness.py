"""Shared test harness: a fully wired kernel on a FakeClock with deterministic ids."""
import copy
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from wakecore.adapters.clock import FakeClock, SequentialIds
from wakecore.adapters.sqlite_dev.store import SqliteStore
from wakecore.app.bootstrap import Kernel, build
from wakecore.app.worker.loop import run_until_idle
from wakecore.kernel.commands import setup as setup_cmd
from wakecore.kernel.commands import tasks as task_cmd
from wakecore.kernel.context import KernelConfig
from wakecore.kernel.domain.model import (
    ActionRecord,
    AuditEntry,
    EventRecord,
    InboxMessage,
    ModelCall,
    Step,
    TaskRuntime,
)
from wakecore.kernel.events.admission import sign
from wakecore.kernel.locks import tx
from wakecore.kernel.ports.faults import FaultPlan

TENANT = "local_user"
USER = "user:alice"
START = datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)

COURSES = {
    "PHARM": {"name": "药理学", "score": None},
    "PATHOPHYS": {"name": "病理生理学", "score": None},
}


def base_spec(**over: Any) -> dict[str, Any]:
    spec = {
        "schema_version": 1,
        "task_id": "grades_demo",
        "root_task_id": "grades_demo",
        "spec_version": 1,
        "purpose": "关注指定课程成绩，出分后通知本人，全部发布后结束",
        "source": {"binding_ref": "school_account_demo",
                   "resource_scope": {"semester": "fall_demo", "course_ids": ["PHARM", "PATHOPHYS"]}},
        "trigger": {"kind": "interval", "every_seconds": 300, "timezone": "Asia/Shanghai",
                    "catchup_policy": "coalesce_latest"},
        "observation": {"completeness_required": "full_target_scope", "first_snapshot": "notify_existing_results",
                        "ignore_fields": ["page_rendered_at"]},
        "completion": {"evaluator": "all_expected_courses_published", "evaluator_version": 1,
                       "required_outputs": ["internal_inbox_committed"]},
        "authority": {"grant_ref": "grant_confirmed_by_user", "read_capabilities": ["grades.read"],
                      "write_capabilities": [], "notify_capabilities": ["inbox.notify_self"],
                      "model_data_egress": []},
        "limits": {"max_steps_per_run": 8, "max_run_seconds": 120, "max_model_attempts_per_day": 4,
                   "min_probe_interval_seconds": 300, "max_followup_count": 2, "max_followup_depth": 1},
        "followup": {"enabled": False},
        "expires_at": "2026-10-06T00:00:00+08:00",
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(spec.get(key), dict):
            spec[key] = {**spec[key], **value}
        else:
            spec[key] = value
    return spec


class Harness:
    def __init__(self, path: str, *, with_model: bool = True, store: Any = None,
                 config: Optional[KernelConfig] = None, courses: Optional[dict] = None) -> None:
        self.clock = FakeClock(START)
        self.faults = FaultPlan()
        self.path = path
        # `store` may be a ready store or a factory `clock -> store` (e.g. PostgreSQL on this FakeClock).
        self._factory = store if callable(store) and not hasattr(store, "transaction") else None
        if self._factory is not None:
            store = self._factory(self.clock)
        self.store = store or SqliteStore(path, self.clock)
        if store is None or self._factory is not None:
            self.store.migrate()
        self.k: Kernel = build(store=self.store, clock=self.clock, ids=SequentialIds(), with_model=with_model,
                               faults=self.faults, config=config)
        self.k.grades.courses = copy.deepcopy(COURSES if courses is None else courses)
        self.ctx = self.k.ctx
        self.k.secrets.put(TENANT, "sec_school", "session-cookie-demo")
        self.k.secrets.put(TENANT, "sec_ingress", "ingress-hmac-demo")

    def restart(self) -> "Harness":
        """Simulate process death + restart: new connection, new id stream, same DB and clock.

        External fakes (grades source, inbox, e-mail provider) keep their state, as a real
        remote system would; only the kernel process is replaced.
        """
        old = self.k
        self.store.close()
        self.store = self._factory(self.clock) if self._factory else SqliteStore(self.path, self.clock)
        self.store.migrate()
        self._gen = getattr(self, "_gen", 0) + 1
        self.faults = FaultPlan()
        self.k = build(store=self.store, clock=self.clock, ids=_GenIds(self._gen), with_model=old.model is not None,
                       faults=self.faults, config=old.ctx.config)
        self.k.grades.courses = old.grades.courses
        self.k.inbox.executions = old.inbox.executions   # counts deliveries into the (persistent) inbox
        if old.email is not None and self.k.email is not None:
            self.k.email.__dict__.update(old.email.__dict__)
        self.ctx = self.k.ctx
        self.k.secrets.put(TENANT, "sec_school", "session-cookie-demo")
        self.k.secrets.put(TENANT, "sec_ingress", "ingress-hmac-demo")
        return self

    # ------------------------------------------------------------------ setup
    def grant(self, *, tenant: str = TENANT, principal: str = USER,
              capabilities: tuple[str, ...] = ("grades.read", "inbox.notify_self", "email.send"),
              egress: tuple[str, ...] = ("model:offline-scripted",), scope: Optional[dict] = None,
              grant_ref: str = "grant_confirmed_by_user") -> Any:
        with tx(self.store) as repo:
            return setup_cmd.register_grant(repo, self.ctx, tenant_id=tenant, principal=principal,
                                            grant_ref=grant_ref, capabilities=list(capabilities),
                                            data_egress=list(egress),
                                            resource_scope=scope if scope is not None else {"semester": "fall_demo"})

    def binding(self, *, tenant: str = TENANT, owner: str = USER, source_ref: str = "school_account_demo",
                uri: str = "urn:wakecore:source:school-demo", secret_ref: str = "sec_school") -> Any:
        with tx(self.store) as repo:
            return setup_cmd.register_source_binding(
                repo, self.ctx, tenant_id=tenant, owner=owner, source_ref=source_ref, connector_id="offline.grades",
                source_uri=uri, resource_scope={"semester": "fall_demo"}, capabilities=["grades.read"],
                secret_ref=secret_ref, ingress_secret_ref="sec_ingress")

    def create(self, spec: Optional[dict] = None, *, tenant: str = TENANT, principal: str = USER,
               activate: bool = True) -> Any:
        spec = spec or base_spec()
        with tx(self.store) as repo:
            rec = task_cmd.create_draft(repo, self.ctx, tenant_id=tenant, principal=principal, raw=spec)
        if not activate:
            return rec
        with tx(self.store) as repo:
            return task_cmd.activate(repo, self.ctx, tenant_id=tenant, principal=principal, task_id=rec.task_id,
                                     spec_version=rec.spec_version, expected_digest=rec.spec_digest)

    def standard(self, spec: Optional[dict] = None, **grant_kw: Any) -> Any:
        self.grant(**grant_kw)
        self.binding()
        return self.create(spec)

    # ------------------------------------------------------------------ running
    def run(self, worker: str = "w1", rounds: int = 50) -> list:
        return run_until_idle(self.ctx, worker, max_rounds=rounds)

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)

    def cycle(self, seconds: float = 300) -> list:
        self.advance(seconds)
        return self.run()

    # ------------------------------------------------------------------ ingress
    def envelope(self, eid: str, *, data: Optional[dict] = None, type_: str = "io.wakecore.grades.changed.v1",
                 source: str = "urn:wakecore:source:school-demo") -> bytes:
        return json.dumps({"specversion": "1.0", "id": eid, "source": source, "type": type_,
                           "subject": "semester/fall-demo", "time": "2026-09-29T02:00:00+08:00",
                           "datacontenttype": "application/json",
                           "data": data if data is not None else {"schema_version": 1, "revision": 1}}).encode()

    def signature(self, body: bytes, secret: str = "ingress-hmac-demo") -> str:
        return sign(secret, body)

    # ------------------------------------------------------------------ queries
    def all(self, cls: type, where: Optional[dict] = None, **kw: Any) -> list:
        with tx(self.store) as repo:
            return repo.find(cls, where or {}, **kw)

    def runtime(self, task_id: str = "grades_demo", tenant: str = TENANT) -> TaskRuntime:
        with tx(self.store) as repo:
            return repo.get(TaskRuntime, tenant_id=tenant, task_id=task_id)

    def inbox(self) -> list[InboxMessage]:
        return self.all(InboxMessage, order_by=("created_at", "message_id"))

    def actions(self) -> list[ActionRecord]:
        return self.all(ActionRecord, order_by=("created_at", "action_id"))

    def steps(self) -> list[Step]:
        return self.all(Step, order_by=("created_at", "step_id"))

    def events(self) -> list[EventRecord]:
        return self.all(EventRecord, order_by=("seq",))

    def model_calls(self) -> list[ModelCall]:
        return self.all(ModelCall, order_by=("created_at", "call_id"))

    def audit(self, kind: Optional[str] = None) -> list[AuditEntry]:
        return self.all(AuditEntry, {"kind": kind} if kind else {}, order_by=("seq",))


class _GenIds(SequentialIds):
    def __init__(self, gen: int) -> None:
        super().__init__()
        self.gen = gen

    def new(self, prefix: str) -> str:
        return super().new(f"{prefix}_g{self.gen}")


def pg_dsn() -> Optional[str]:
    return os.environ.get("WAKECORE_PG_DSN")


# ---------------------------------------------------------------------- scripted flows

EMAIL_PAYLOAD = {"to": ["alice@example.edu"], "subject": "成绩备注变化", "body": "药理学备注已更新"}


def model_spec(**over: Any) -> dict[str, Any]:
    """Spec that lets the scripted model see observations (JUDGE/PLAN become reachable)."""
    authority = {"model_data_egress": ["model:offline-scripted"], **over.pop("authority", {})}
    return base_spec(authority=authority, **over)


def email_spec(**over: Any) -> dict[str, Any]:
    authority = {"write_capabilities": ["email.send"], **over.pop("authority", {})}
    return model_spec(authority=authority, **over)


def script_email_plan(h: "Harness", payload: Optional[dict] = None, **action_kw: Any) -> None:
    from wakecore.kernel.domain.enums import Route
    from wakecore.kernel.ports.reasoning import DecisionProposal, PlanProposal, ProposedAction, Usage

    h.k.model.script_classify(DecisionProposal(route=Route.PLANNER, reason_code="needs_email",
                                               usage=Usage(10, 5, 1), model_ref="offline-scripted"))
    h.k.model.script_plan(PlanProposal(steps=(ProposedAction(
        logical_step_id="mail_advisor", tool_id="email.send", capability="email.send",
        payload=dict(payload or EMAIL_PAYLOAD), reason_code="advisor_should_know", **action_kw),),
        usage=Usage(20, 10, 2), model_ref="offline-scripted"))


def trigger_ambiguous_change(h: "Harness", remark: str = "考试延期") -> list:
    """A non-score field change is `record_changed`: ambiguous, so it goes to JUDGE."""
    h.k.grades.set_field("PHARM", remark=remark)
    return h.cycle()


def email_waiting_approval(h: "Harness", payload: Optional[dict] = None) -> tuple[ActionRecord, Any]:
    from wakecore.kernel.domain.model import Approval

    h.standard(email_spec())
    h.run()
    script_email_plan(h, payload)
    trigger_ambiguous_change(h)
    action = [a for a in h.actions() if a.tool_id == "email.send"][-1]
    approval = h.all(Approval, {"action_id": action.action_id})[-1]
    return action, approval


def approve(h: "Harness", approval: Any, *, digest: Optional[str] = None, actor: str = USER) -> dict:
    from wakecore.kernel.commands import approvals

    with tx(h.store) as repo:
        return approvals.approve(repo, h.ctx, tenant_id=approval.tenant_id, actor=actor,
                                 approval_id=approval.approval_id, payload_digest=digest or approval.payload_digest)
