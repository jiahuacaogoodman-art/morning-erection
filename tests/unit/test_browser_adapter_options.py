"""Scope options of the browser adapters that real sites need (stdlib, fake runtime client):

  missing_targets = "absent"   a target not on a fully rendered page is absent, not a coverage gap
  state_location  = "client"   the site keeps state in the browser: zero mutating requests prove nothing
"""
from datetime import timedelta
from typing import Any

import pytest

from wakecore.adapters.clock import FakeClock
from wakecore.adapters.sources.playwright_web import PlaywrightWebSource
from wakecore.adapters.tools.browser_cua import BrowserCuaTool
from wakecore.kernel.domain.enums import Completeness, ObservationOutcome
from wakecore.kernel.ports.action import AuthorizedAction, ReconcileRequest
from wakecore.kernel.ports.observation import ObservationRequest

ORIGIN = "https://demo.example"
LIST = {"list": {"item": "li.todo"}, "ready": "ul.todo-list", "key_field": "title",
        "columns": [{"field": "title", "selector": "label"}]}


class FakeRuntime:
    def __init__(self, records: dict | None = None, act: dict | None = None, status: dict | None = None) -> None:
        self.records = records or {}
        self.act_result = act or {"status": "done", "actions_executed": 3, "mutating_requests": 0}
        self.status = status or {"status": "finished", "mutating_requests": 0, "result_status": "done"}
        self.acts = 0

    def observe(self, **kw: Any) -> dict:
        return {"outcome": "SUCCESS", "records": self.records, "content_digest": "d", "title": "t", "url": kw["url"]}

    def verify(self, *, record_id: str, **kw: Any) -> dict:
        rec = self.records.get(record_id)
        return {"outcome": "SUCCESS", "present": rec is not None, "record": rec, "content_digest": "d"}

    def act(self, **kw: Any) -> dict:
        self.acts += 1
        return self.act_result

    def act_status(self, key: str) -> dict:
        return self.status


def observe(src, **scope):
    clock = FakeClock()
    return src.fetch(ObservationRequest(tenant_id="t", source_ref="s", secret="sess", cursor=None,
                                        requested_at=clock.utc_now(), resource_scope={
                                            "origins": [ORIGIN], "url": ORIGIN + "/", "extractor": LIST, **scope}))


def test_missing_target_is_a_gap_by_default():
    r = observe(PlaywrightWebSource(FakeRuntime({"买菜": {"title": "买菜"}})), record_ids=["交房租"])
    assert r.outcome is ObservationOutcome.PARTIAL and r.coverage_gaps == ("交房租",)


def test_missing_target_can_be_declared_absent():
    r = observe(PlaywrightWebSource(FakeRuntime({"买菜": {"title": "买菜"}})), record_ids=["交房租", "买菜"],
                missing_targets="absent")
    assert r.outcome is ObservationOutcome.SUCCESS and r.completeness is Completeness.COMPLETE
    assert r.scope == {"record_ids": ["交房租", "买菜"]} and set(r.records) == {"买菜"}


def test_absent_needs_a_ready_marker():
    src = PlaywrightWebSource(FakeRuntime())
    clock = FakeClock()
    r = src.fetch(ObservationRequest(tenant_id="t", source_ref="s", secret="sess", cursor=None,
                                     requested_at=clock.utc_now(), resource_scope={
                                         "origins": [ORIGIN], "url": ORIGIN + "/", "missing_targets": "absent",
                                         "extractor": {k: v for k, v in LIST.items() if k != "ready"},
                                         "record_ids": ["x"]}))
    assert r.outcome is ObservationOutcome.SCHEMA_INVALID and r.error_code == "missing_targets_absent_needs_ready"


# ------------------------------------------------------------------ state_location

GOAL = {"field": "done", "op": "eq", "value": True}


def action(clock, **scope):
    return AuthorizedAction(
        tenant_id="t", action_id="a", attempt_id="att", effect_key="ek", task_id="task", tool_id="browser.cua",
        tool_version="0.3.0", payload={"goal": "g", "start_url": ORIGIN + "/",
                                       "verify": {"record_id": "交房租", "conditions": [GOAL]}},
        payload_digest="p", revocation_epoch=0, permit="x", secret="sess",
        resource_scope={"origins": [ORIGIN], "url": ORIGIN + "/", "extractor": LIST, "record_ids": ["交房租"], **scope},
        deadline_at=clock.utc_now() + timedelta(seconds=120))


def reconcile(tool, clock, req, after):
    clock.advance(after)
    return tool.reconcile(ReconcileRequest(
        tenant_id="t", action_id="a", effect_key="ek", tool_id="browser.cua", payload_digest="p",
        payload=req.payload, resource_scope=req.resource_scope, attempted_at=req.deadline_at - timedelta(seconds=120),
        requested_at=clock.utc_now(), secret="sess"))


NOT_DONE = {"交房租": {"title": "交房租", "done": False}}


def test_server_state_zero_mutations_is_a_proven_no_effect():
    clock = FakeClock()
    tool = BrowserCuaTool(FakeRuntime(NOT_DONE), clock)
    assert tool.execute(action(clock)).status == "failed_no_effect"


@pytest.mark.parametrize("settle", [61, 500])
def test_client_state_touched_page_stays_unknown_until_the_page_shows_it(settle):
    clock = FakeClock()
    rt = FakeRuntime(NOT_DONE)
    tool = BrowserCuaTool(rt, clock, settle_seconds=120)
    req = action(clock, state_location="client")
    r = tool.execute(req)
    assert r.status == "unknown" and r.error == "submitted_but_not_verified"
    rec = reconcile(tool, clock, req, settle)          # past the settle window it still does not guess
    assert rec.status == "still_unknown" and rec.evidence["client_state"] is True
    rt.records = {"交房租": {"title": "交房租", "done": True}}
    assert reconcile(tool, clock, req, 61).status == "confirmed"
    assert rt.acts == 1                                 # never re-operated


def test_client_state_untouched_page_is_still_a_no_effect():
    clock = FakeClock()
    rt = FakeRuntime(NOT_DONE, act={"status": "failed", "reason": "model_error", "actions_executed": 0,
                                    "mutating_requests": 0})
    assert BrowserCuaTool(rt, clock).execute(action(clock, state_location="client")).status == "failed_no_effect"


def test_client_state_never_received_is_no_effect_on_reconcile():
    clock = FakeClock()
    rt = FakeRuntime(NOT_DONE, status={"status": "never_received"})
    tool = BrowserCuaTool(rt, clock)
    assert reconcile(tool, clock, action(clock, state_location="client"), 61).status == "no_effect"


def test_a_canceled_act_that_sent_nothing_is_a_no_effect():
    clock = FakeClock()
    rt = FakeRuntime(NOT_DONE, act={"status": "canceled", "reason": "cancel_requested", "actions_executed": 2,
                                    "mutating_requests": 0})
    r = BrowserCuaTool(rt, clock).execute(action(clock))
    assert r.status == "failed_no_effect" and r.error == "act_canceled:cancel_requested"


def test_a_canceled_act_that_may_have_submitted_stays_unknown():
    clock = FakeClock()
    rt = FakeRuntime(NOT_DONE, act={"status": "canceled", "reason": "cancel_requested", "actions_executed": 2,
                                    "mutating_requests": 1})
    assert BrowserCuaTool(rt, clock).execute(action(clock)).status == "unknown"
