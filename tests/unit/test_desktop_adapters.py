"""desktop.macos (eye) and desktop.cua (hand) against a scripted Desktop Runtime client.

Kernel-only: no sidecar, no bridge. The live stack is exercised in
tests/contract/test_desktop_acceptance.py.
"""
from datetime import timedelta

import pytest

from wakecore.adapters.clock import FakeClock
from wakecore.adapters.sources.desktop_app import DesktopAppSource
from wakecore.adapters.tools.browser_cua import MODEL_EGRESS
from wakecore.adapters.tools.desktop_cua import DesktopCuaTool
from wakecore.adapters.ui_runtime.client import (
    ProtocolMismatch,
    RuntimeRejected,
    RuntimeTransportError,
    RuntimeUnreachable,
)
from wakecore.adapters.ui_runtime.desktop_client import DESKTOP_PROTOCOL, DesktopRuntimeClient
from wakecore.app.bootstrap import build
from wakecore.kernel.domain.enums import ObservationOutcome
from wakecore.kernel.ports.action import AuthorizedAction, ReconcileRequest
from wakecore.kernel.ports.observation import ObservationRequest
from wakecore.testing.conformance import ConnectorScenario, ToolScenario, check_connector, check_tool

CALC = "com.apple.calculator"
EXTRACTOR = {"ready": {"id": "StandardInputView"}, "records": [{"id": "display", "columns": [
    {"field": "value", "node": {"head_regex": "^text "}, "pattern": r"(\S+)$", "type": "number"}]}]}
SCOPE = {"apps": [CALC], "app": CALC, "extractor": EXTRACTOR, "record_ids": ["display"]}
PAYLOAD = {"goal": "compute 12 x 3", "verify": {"record_id": "display",
                                                "conditions": [{"field": "value", "op": "eq", "value": 36}]}}


class FakeDesktop:
    """Answers like DesktopRuntimeClient; `act_effect` is what the model's act does to the app."""

    def __init__(self, display=0, *, act_effect=None, mutating=0, act_error=None, status_after=None):
        self.display, self.act_effect, self.mutating = display, act_effect, mutating
        self.act_error, self.status_after = act_error, status_after
        self.acts, self.reads, self.journal = 0, [], {}

    def observe(self, *, app, apps, extractor):
        self.reads.append(("observe", app, tuple(apps)))
        return {"outcome": "SUCCESS", "records": {"display": {"value": self.display}}, "app": app,
                "window": "Calculator", "content_digest": f"d{self.display}", "error_code": None}

    def verify(self, *, app, apps, extractor, record_id):
        self.reads.append(("verify", app, tuple(apps)))
        return {"outcome": "SUCCESS", "record": {"value": self.display}, "present": True, "app": app,
                "window": "Calculator", "content_digest": f"d{self.display}", "error_code": None}

    def act(self, *, key, attempt, app, apps, goal, timeout_s, max_steps, model_egress=None):
        self.acts += 1
        if key in self.journal:
            return {**self.journal[key], "deduplicated": True}
        if self.act_error:
            self.journal[key] = {"status": "error", "mutating_actions": self.mutating}
            raise self.act_error
        if self.act_effect is not None:
            self.display = self.act_effect
        r = {"status": "completed", "reason": "model_finished", "steps": 1, "actions_executed": self.mutating,
             "mutating_actions": self.mutating, "blocked": [], "artifacts_ref": "acts/x"}
        self.journal[key] = r
        return r

    def act_status(self, key):
        if self.status_after is not None:
            return self.status_after
        if key not in self.journal:
            return {"status": "never_received", "mutating_actions": 0}
        return {"status": "finished", "mutating_actions": self.journal[key]["mutating_actions"],
                "result_status": self.journal[key]["status"]}


def _action(tool, clock, *, payload=PAYLOAD, scope=SCOPE, key="ek_1"):
    return AuthorizedAction(
        tenant_id="t", action_id="a", attempt_id="att_1", effect_key=key, task_id="task", tool_id="desktop.cua",
        tool_version="0.3.0", payload=payload, payload_digest="sha256:x", revocation_epoch=0, permit="p",
        secret=None, resource_scope=scope, data_egress=(MODEL_EGRESS,),
        deadline_at=clock.utc_now() + timedelta(seconds=600))


def _reconcile(clock, key="ek_1", scope=SCOPE):
    now = clock.utc_now()
    return ReconcileRequest(tenant_id="t", action_id="a", effect_key=key, tool_id="desktop.cua",
                            payload_digest="sha256:x", provider_request_id=key, payload=PAYLOAD, resource_scope=scope,
                            attempted_at=now, requested_at=now, secret=None)


# ------------------------------------------------------------------ conformance

def test_desktop_cua_conforms_when_the_act_reaches_the_goal():
    clock = FakeClock()
    report = check_tool(lambda: DesktopCuaTool(FakeDesktop(12, act_effect=36, mutating=6), clock),
                        ToolScenario(payload=PAYLOAD, resource_scope=SCOPE, data_egress=(MODEL_EGRESS,),
                                     now=clock.utc_now, expect_status="confirmed"))
    report.assert_ok()


def test_desktop_cua_conforms_on_a_proven_no_effect():
    clock = FakeClock()
    fakes = []

    def make():
        fakes.append(FakeDesktop(12))
        return DesktopCuaTool(fakes[-1], clock)
    report = check_tool(make, ToolScenario(payload=PAYLOAD, resource_scope=SCOPE, data_egress=(MODEL_EGRESS,),
                                           now=clock.utc_now, expect_status="failed_no_effect"))
    report.assert_ok()
    assert all(f.acts <= 1 for f in fakes)                        # reconcile never re-operated the app


def test_desktop_source_conforms():
    report = check_connector(lambda: DesktopAppSource(FakeDesktop(12)), ConnectorScenario(
        resource_scope=SCOPE, expect_outcome=ObservationOutcome.SUCCESS))
    report.assert_ok()
    assert "fetch.without_secret" not in report.names(failed=True)


def test_descriptors():
    t, s = DesktopCuaTool.descriptor, DesktopAppSource.descriptor
    assert (t.tool_id, t.capability_type, t.credential, t.url_fields) == ("desktop.cua", "desktop.operate", "none", ())
    assert t.required_egress == (MODEL_EGRESS,) and t.confirmation_semantics == "re_observation"
    assert (s.connector_id, s.read_capability, s.authentication_model) == ("desktop.macos", "desktop.observe", "none")
    relay = DesktopCuaTool(FakeDesktop(), FakeClock(), model_egress="model:openai-compatible:relay.example")
    assert relay.descriptor.required_egress == ("model:openai-compatible:relay.example",)
    with pytest.raises(ValueError):
        DesktopCuaTool(FakeDesktop(), FakeClock(), model_egress="model:somewhere-else")


# ------------------------------------------------------------------ the hand

def test_already_satisfied_never_acts():
    clock, fake = FakeClock(), FakeDesktop(36)
    r = DesktopCuaTool(fake, clock).execute(_action(None, clock))
    assert r.status == "confirmed" and r.receipt["already_satisfied"] and fake.acts == 0


def test_sent_but_not_verified_is_unknown_and_reconcile_never_settles_it():
    clock, fake = FakeClock(), FakeDesktop(12, act_effect=35, mutating=6)
    tool = DesktopCuaTool(fake, clock)
    r = tool.execute(_action(tool, clock))
    assert (r.status, r.error) == ("unknown", "sent_but_not_verified") and r.receipt["act"]["mutating_actions"] == 6
    for _ in range(3):                             # the app keeps its state locally: waiting proves nothing
        clock.advance(3600)
        assert tool.reconcile(_reconcile(clock)).status == "still_unknown"
    fake.display = 36                              # a human finished it, or it just showed up
    clock.advance(3600)
    assert tool.reconcile(_reconcile(clock)).status == "confirmed"
    assert fake.acts == 1


def test_reconcile_is_throttled_and_no_effect_only_when_nothing_was_sent():
    clock, fake = FakeClock(), FakeDesktop(12)
    tool = DesktopCuaTool(fake, clock)
    assert tool.reconcile(_reconcile(clock, key="never")).status == "no_effect"
    assert tool.reconcile(_reconcile(clock, key="never")).evidence == {"throttled": True}
    fake.status_after = {"status": "in_progress", "mutating_actions": 0}
    assert tool.reconcile(_reconcile(clock, key="running")).status == "still_unknown"


@pytest.mark.parametrize("scope,payload,error", [
    ({**SCOPE, "app": "com.apple.finder"}, PAYLOAD, "app_outside_scope"),
    (SCOPE, {**PAYLOAD, "app": "com.apple.finder"}, "app_outside_scope"),
    ({**SCOPE, "apps": "com.apple.calculator"}, PAYLOAD, "app_outside_scope"),
    ({**SCOPE, "record_ids": ["other"]}, PAYLOAD, "record_outside_scope"),
    ({k: v for k, v in SCOPE.items() if k != "extractor"}, PAYLOAD, "scope_needs_extractor"),
])
def test_scope_is_enforced_before_anything(scope, payload, error):
    clock, fake = FakeClock(), FakeDesktop(12)
    r = DesktopCuaTool(fake, clock).execute(_action(None, clock, payload=payload, scope=scope))
    assert (r.status, r.error) == ("failed_no_effect", error) and fake.acts == 0 and fake.reads == []


def test_payload_app_selects_another_allowed_app():
    clock, fake = FakeClock(), FakeDesktop(12, act_effect=36, mutating=6)
    scope = {**SCOPE, "apps": [CALC, "com.apple.textedit"], "app": "com.apple.textedit"}
    r = DesktopCuaTool(fake, clock).execute(_action(None, clock, payload={**PAYLOAD, "app": CALC}, scope=scope))
    assert r.status == "confirmed" and {x[1] for x in fake.reads} == {CALC}


@pytest.mark.parametrize("exc,mutating,status,error", [
    (RuntimeUnreachable("refused"), 0, "failed_no_effect", "desktop_runtime_unreachable"),
    (ProtocolMismatch("wakecore.ui-runtime/1", DESKTOP_PROTOCOL), 0, "failed_no_effect",
     "desktop_runtime_protocol_mismatch"),
    (RuntimeRejected(403, '{"error": {"code": "app_outside_scope", "message": "x"}}'), 0, "failed_no_effect",
     "desktop_runtime_rejected_403"),
    (RuntimeRejected(503, '{"error": {"code": "desktop_busy", "message": "x", "retryable": true}}'), 0,
     "failed_no_effect", "act_error"),
    (RuntimeTransportError("timeout"), 0, "failed_no_effect", "act_error"),
    (RuntimeTransportError("timeout"), 2, "unknown", "desktop_runtime_lost"),
])
def test_act_transport_failures(exc, mutating, status, error):
    clock = FakeClock()
    fake = FakeDesktop(12, act_error=exc, mutating=mutating)
    r = DesktopCuaTool(fake, clock).execute(_action(None, clock))
    assert (r.status, r.error) == (status, error)


def test_egress_mismatch_is_no_effect():
    clock = FakeClock()
    body = '{"error": {"code": "model_egress_mismatch", "message": "x", "details": ' \
           '{"runtime_model_egress": "model:openai-compatible:relay"}}}'
    fake = FakeDesktop(12, act_error=RuntimeRejected(409, body))
    r = DesktopCuaTool(fake, clock).execute(_action(None, clock))
    assert (r.status, r.error) == ("failed_no_effect", "model_egress_mismatch")
    assert r.receipt["runtime_model_egress"] == "model:openai-compatible:relay"


# ------------------------------------------------------------------ the eye

class Raises(FakeDesktop):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def observe(self, **kw):
        raise self.exc


def _fetch(src, scope=SCOPE):
    return src.fetch(ObservationRequest(tenant_id="t", source_ref="s", resource_scope=scope, secret=None,
                                        cursor=None, requested_at=FakeClock().utc_now()))


@pytest.mark.parametrize("exc,outcome,code", [
    (RuntimeRejected(503, '{"error": {"code": "desktop_busy", "message": "x", "retryable": true}}'),
     ObservationOutcome.UNAVAILABLE, "desktop_busy"),
    (RuntimeRejected(403, '{"error": {"code": "app_outside_scope", "message": "x"}}'),
     ObservationOutcome.SCHEMA_INVALID, "app_outside_scope"),
    (ProtocolMismatch(None, DESKTOP_PROTOCOL), ObservationOutcome.UNAVAILABLE, "desktop_runtime_protocol_mismatch"),
    (RuntimeUnreachable("x"), ObservationOutcome.UNAVAILABLE, "desktop_runtime_unreachable"),
])
def test_eye_failures_are_never_empty_reads(exc, outcome, code):
    r = _fetch(DesktopAppSource(Raises(exc)))
    assert (r.outcome, r.error_code, r.records) == (outcome, code, {})


def test_eye_scope_checks_and_partial():
    fake = FakeDesktop(12)
    assert _fetch(DesktopAppSource(fake), {**SCOPE, "app": "com.apple.finder"}).error_code == \
        "scope_needs_app_apps_extractor"
    assert _fetch(DesktopAppSource(fake), {**SCOPE, "missing_targets": "absent",
                                           "extractor": {"records": []}}).error_code == \
        "missing_targets_absent_needs_ready"
    part = _fetch(DesktopAppSource(fake), {**SCOPE, "record_ids": ["display", "memory"]})
    assert part.outcome is ObservationOutcome.PARTIAL and part.coverage_gaps == ("memory",)
    absent = _fetch(DesktopAppSource(fake), {**SCOPE, "record_ids": ["display", "memory"],
                                             "missing_targets": "absent"})
    assert absent.outcome is ObservationOutcome.SUCCESS and absent.scope == {"record_ids": ["display", "memory"]}
    assert fake.reads == [("observe", CALC, (CALC,))] * 2


# ------------------------------------------------------------------ wiring

def test_bootstrap_wires_the_desktop_without_connecting(tmp_path, monkeypatch):
    monkeypatch.delenv("WAKECORE_UI_RUNTIME_URL", raising=False)
    k = build(db_url=f"sqlite:///{tmp_path / 'wk.db'}", desktop_runtime_url="http://127.0.0.1:9",
              desktop_runtime_token="t", desktop_model_egress="model:openai-compatible:relay.example")
    try:
        assert isinstance(k.desktop.client, DesktopRuntimeClient) and k.desktop.client.protocol == DESKTOP_PROTOCOL
        assert k.ctx.connectors["desktop.macos"] is k.desktop and k.ctx.tools["desktop.cua"] is k.desktop_hand
        pol = k.ctx.system_policy
        assert {"desktop.observe", "desktop.operate"} <= pol.allowed_capabilities
        assert "model:openai-compatible:relay.example" in pol.allowed_data_egress
        assert "web.observe" not in pol.allowed_capabilities and k.web is None
    finally:
        k.ctx.store.close()


def test_bootstrap_widens_both_default_policies(tmp_path, monkeypatch):
    k = build(db_url=f"sqlite:///{tmp_path / 'wk.db'}", ui_runtime_url="http://127.0.0.1:9",
              desktop_runtime_url="http://127.0.0.1:8")
    try:
        caps = k.ctx.system_policy.allowed_capabilities
        assert {"web.observe", "browser.operate", "desktop.observe", "desktop.operate"} <= caps
    finally:
        k.ctx.store.close()
