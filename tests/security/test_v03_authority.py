"""V0.3 P0: an operating tool cannot leave the task's authorisation domain.

resource_scope and data_egress are persisted on the action, bound into the approval digest,
and re-validated (with the digest recomputed from the stored row) right before dispatch.
The tool gets a hard deadline derived from limits.max_run_seconds.
"""
import dataclasses
from datetime import timedelta
from typing import Any

import pytest

from harness import approve, base_spec
from wakecore.kernel.actions.intents import action_digest
from wakecore.kernel.context import SystemPolicy
from wakecore.kernel.domain.enums import ActionStatus, Route, SideEffectClass
from wakecore.kernel.domain.model import ActionAttempt, ActionRecord, Approval, AuditEntry
from wakecore.kernel.locks import tx
from wakecore.kernel.policy.origins import origin_of, url_in_scope
from wakecore.kernel.ports.action import ActionResult, ReconcileResult, ToolDescriptor
from wakecore.kernel.ports.reasoning import DecisionProposal, PlanProposal, ProposedAction, Usage

ORIGIN = "https://jw.example.edu"
EGRESS = "model:fake-cu"


class RecordingWebTool:
    descriptor = ToolDescriptor(
        tool_id="web.operate", tool_version="1.0.0",
        input_schema={"type": "object", "required": ["goal", "start_url"],
                      "properties": {"goal": {"type": "string", "maxLength": 2000},
                                     "start_url": {"type": "string", "maxLength": 2000}}},
        output_schema={"type": "object"}, capability_type="web.operate", allowed_resource_kinds=("origin",),
        side_effect_class=SideEffectClass.EXTERNAL_WRITE, supports_idempotency=False,
        idempotency_retention_seconds=0, supports_reconciliation=True, confirmation_semantics="re_observation",
        max_duration_seconds=600, url_fields=("start_url",), required_egress=(EGRESS,), credential="source_binding")

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def execute(self, request):
        self.requests.append(request)
        return ActionResult(status="confirmed", provider_ref="web-1")

    def reconcile(self, request):
        return ReconcileResult(status="still_unknown")


@pytest.fixture
def web(h):
    tool = RecordingWebTool()
    h.ctx.tools[tool.descriptor.tool_id] = tool
    h.ctx.system_policy = SystemPolicy(
        allowed_capabilities=SystemPolicy().allowed_capabilities | {"web.operate"},
        allowed_data_egress=SystemPolicy().allowed_data_egress | {EGRESS})
    return tool


def web_spec(*, egress=("model:offline-scripted", EGRESS), max_run_seconds=120, **over):
    return base_spec(
        source={"binding_ref": "school_account_demo",
                "resource_scope": {"semester": "fall_demo", "course_ids": ["PHARM", "PATHOPHYS"],
                                   "origins": [ORIGIN]}},
        authority={"write_capabilities": ["web.operate"], "model_data_egress": list(egress)},
        limits={"max_run_seconds": max_run_seconds}, **over)


def setup_web(h, **spec_kw):
    h.grant(capabilities=("grades.read", "inbox.notify_self", "web.operate"),
            egress=("model:offline-scripted", EGRESS), scope={"semester": "fall_demo", "origins": [ORIGIN]})
    h.binding()
    h.create(web_spec(**spec_kw))
    h.run()


def script_web_plan(h, start_url=f"{ORIGIN}/courses", *, classify=None):
    h.k.model.script_classify(classify or DecisionProposal(route=Route.PLANNER, reason_code="seat_opened",
                                                           usage=Usage(1, 1, 1), model_ref="offline-scripted"))
    h.k.model.script_plan(PlanProposal(steps=(ProposedAction(
        logical_step_id="enroll", tool_id="web.operate", capability="web.operate",
        payload={"goal": "选上 PHARM", "start_url": start_url}, reason_code="enroll"),),
        usage=Usage(1, 1, 1), model_ref="offline-scripted"))


def web_action(h, **plan_kw):
    script_web_plan(h, **plan_kw)
    h.k.grades.set_field("PHARM", remark="有余量")
    h.cycle()
    return [a for a in h.actions() if a.tool_id == "web.operate"][-1]


def approval_of(h, action):
    return h.all(Approval, {"action_id": action.action_id})[-1]


# ------------------------------------------------------------------ origin parsing

@pytest.mark.parametrize("url,origin", [
    ("https://JW.Example.edu:443/a?b#c", "https://jw.example.edu"),
    ("http://localhost:8123/x", "http://localhost:8123"),
    ("https://jw.example.edu@evil.example/", None),   # userinfo trick
    ("javascript:alert(1)", None),
    ("https://evil.example\\@jw.example.edu/", None),
    ("//jw.example.edu/", None),
    ("https://jw.example.edu:99999/", None),
    ("https://[::1]:8443/p", "https://[::1]:8443"),
])
def test_origin_parser_is_strict(url, origin):
    assert origin_of(url) == origin


def test_url_scope_check_compares_normalised_origins():
    assert url_in_scope("https://jw.example.edu/courses", ["https://JW.example.edu:443"])
    assert not url_in_scope("https://jw.example.edu.evil.example/", [ORIGIN])


# ------------------------------------------------------------------ binding into the digest

def test_scope_and_egress_are_persisted_and_bound_into_the_approval(h, web):
    setup_web(h)
    action = web_action(h)
    assert action.status is ActionStatus.WAITING_APPROVAL
    assert action.resource_scope["origins"] == [ORIGIN]
    assert EGRESS in action.data_egress      # the tool's own egress is added, not left to the model
    rt = h.runtime()
    wider = {**action.resource_scope, "origins": [ORIGIN, "https://evil.example"]}
    assert action_digest(rt, action.tool_id, action.tool_version, action.capability, action.canonical_payload,
                         action.preconditions, action.resource_scope, action.data_egress) == action.payload_digest
    assert action_digest(rt, action.tool_id, action.tool_version, action.capability, action.canonical_payload,
                         action.preconditions, wider, action.data_egress) != action.payload_digest
    assert approval_of(h, action).payload_digest == action.payload_digest
    approve(h, approval_of(h, action))
    h.run()
    [req] = web.requests
    assert req.resource_scope["origins"] == [ORIGIN] and EGRESS in req.data_egress
    assert req.secret == "session-cookie-demo"  # the source binding's handle, resolved at dispatch only


def test_scope_widened_in_the_database_after_approval_is_refused(h, web):
    setup_web(h)
    action = web_action(h)
    approve(h, approval_of(h, action))
    with tx(h.store) as repo:           # an attacker (or bug) rewrites the stored row
        cur = repo.get(ActionRecord, tenant_id=action.tenant_id, action_id=action.action_id)
        repo.change(cur, resource_scope={**cur.resource_scope, "origins": [ORIGIN, "https://evil.example"]})
    h.run()
    cur = h.all(ActionRecord, {"action_id": action.action_id})[0]
    assert cur.status is ActionStatus.DENIED and cur.resolution == "digest_mismatch"
    assert web.requests == []


def test_egress_widened_in_the_database_after_approval_is_refused(h, web):
    setup_web(h)
    action = web_action(h)
    approve(h, approval_of(h, action))
    with tx(h.store) as repo:
        cur = repo.get(ActionRecord, tenant_id=action.tenant_id, action_id=action.action_id)
        repo.change(cur, data_egress=[*cur.data_egress, "model:somewhere-else"])
    h.run()
    assert h.all(ActionRecord, {"action_id": action.action_id})[0].resolution == "digest_mismatch"
    assert web.requests == []


# ------------------------------------------------------------------ origins

@pytest.mark.parametrize("url", ["https://evil.example/login", f"{ORIGIN}@evil.example/", "file:///etc/passwd"])
def test_planner_cannot_point_the_tool_outside_the_task_origins(h, web, url):
    setup_web(h)
    action = web_action(h, start_url=url)
    assert action.status is ActionStatus.DENIED
    assert action.resolution == "origin_outside_scope:start_url"
    assert h.all(Approval, {"action_id": action.action_id}) == []   # never even offered for approval
    h.run()
    assert web.requests == []


def test_task_without_origins_cannot_use_an_url_tool(h, web):
    h.grant(capabilities=("grades.read", "inbox.notify_self", "web.operate"), egress=("model:offline-scripted", EGRESS))
    h.binding()
    spec = web_spec()
    spec["source"]["resource_scope"].pop("origins")
    h.create(spec)
    h.run()
    action = web_action(h)
    assert action.status is ActionStatus.DENIED and action.resolution == "origin_scope_missing"


# ------------------------------------------------------------------ egress

def test_tool_egress_not_granted_to_the_task_is_denied_and_hidden_from_the_planner(h, web):
    setup_web(h, egress=("model:offline-scripted",))
    action = web_action(h)
    assert action.status is ActionStatus.DENIED and action.resolution == "data_egress_not_allowed"
    [bundle] = [b for kind, b in h.k.model.calls if kind == "plan"]
    assert "web.operate" not in {t["tool_id"] for t in bundle.allowed_tools}


def test_planner_sees_only_tools_the_task_is_authorised_for(h, web):
    setup_web(h)
    web_action(h)
    [bundle] = [b for kind, b in h.k.model.calls if kind == "plan"]
    tools = {t["tool_id"]: t for t in bundle.allowed_tools}
    assert set(tools) == {"inbox.notify", "web.operate"}   # email.send is installed but not granted
    assert tools["web.operate"]["allowed_origins"] == [ORIGIN]
    assert "secret" not in str(bundle) and "session-cookie-demo" not in str(bundle)


def test_system_policy_narrowed_after_approval_blocks_dispatch(h, web):
    setup_web(h)
    action = web_action(h)
    approve(h, approval_of(h, action))
    h.ctx.system_policy = dataclasses.replace(h.ctx.system_policy,
                                              allowed_data_egress=frozenset({"model:offline-scripted"}))
    h.run()
    cur = h.all(ActionRecord, {"action_id": action.action_id})[0]
    assert cur.status is ActionStatus.DENIED and cur.resolution == "system_policy:data_egress"
    assert web.requests == []


# ------------------------------------------------------------------ max_run_seconds

def test_tool_deadline_is_bounded_by_max_run_seconds(h, web):
    setup_web(h, max_run_seconds=90)
    action = web_action(h)
    approve(h, approval_of(h, action))
    now = h.clock.utc_now()
    h.run()
    [req] = web.requests
    assert req.deadline_at == now + timedelta(seconds=90)   # tool allows 600, the task only 90
    [att] = h.all(ActionAttempt, {"action_id": action.action_id})
    assert att.lease_until > req.deadline_at                 # overrun -> UNKNOWN via recovery, not retry


def test_run_over_its_time_limit_gets_no_more_model_steps(h, web):
    setup_web(h, max_run_seconds=10)

    def slow_classify(bundle):
        h.clock.advance(20)  # the judge call itself used up the run's time (lease is 30s)
        return DecisionProposal(route=Route.PLANNER, reason_code="seat_opened", usage=Usage(1, 1, 1),
                                model_ref="offline-scripted")

    script_web_plan(h, classify=slow_classify)
    h.k.grades.set_field("PHARM", remark="有余量")
    h.cycle()
    assert [k for k, _ in h.k.model.calls] == ["classify"]     # the planner was never called
    assert [a for a in h.actions() if a.tool_id == "web.operate"] == []
    review = [a for a in h.actions() if a.logical_action == "notify.review"]
    assert review and review[-1].reason_code == "run_time_limit"   # a human is told instead
    assert h.all(AuditEntry, {"kind": "run.time_limit"})
