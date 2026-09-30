"""Desktop acceptance: "盯着计算器，出现 12 以后帮我乘 3，确认是 36 再结束。"

periodic accessibility read (no model) -> change -> wake -> planner (only authorised tools) ->
authority -> approval -> Computer Use over the one allowed app -> deterministic re-read ->
CONFIRMED -> goal observed -> COMPLETED; replay touches nothing.

The kernel, the DesktopRuntime (real HTTP), its journal and the stdio MCP bridge process are
real; the app (testing/fake_desktop.FakeDesktopMcp) and the model (FakeDesktopModel) are fakes.
"""
import pytest

pytest.importorskip("wakecore_ui_runtime")

from test_desktop_runtime import CALC_X, Live  # noqa: E402

from harness import TENANT, USER, Harness, approve, base_spec  # noqa: E402
from wakecore.adapters.sources.desktop_app import DesktopAppSource  # noqa: E402
from wakecore.adapters.tools.desktop_cua import DesktopCuaTool  # noqa: E402
from wakecore.adapters.ui_runtime.client import UiRuntimeClient  # noqa: E402
from wakecore.adapters.ui_runtime.desktop_client import DesktopRuntimeClient  # noqa: E402
from wakecore.kernel.commands import setup as setup_cmd  # noqa: E402
from wakecore.kernel.context import SystemPolicy  # noqa: E402
from wakecore.kernel.domain.enums import ActionStatus, TaskLifecycle  # noqa: E402
from wakecore.kernel.domain.model import ActionAttempt, ActionRecord, Approval, ObservationRecord  # noqa: E402
from wakecore.kernel.locks import tx  # noqa: E402
from wakecore.kernel.ports.reasoning import PlanProposal, ProposedAction, Usage  # noqa: E402
from wakecore.kernel.replay import replay_task  # noqa: E402
from wakecore_ui_runtime.testing.fake_desktop import CALC, write_state  # noqa: E402

EGRESS = "model:openai-compatible:127.0.0.1"          # the fake model listens on loopback
SCOPE = {"apps": [CALC], "app": CALC, "extractor": CALC_X, "record_ids": ["display"]}
TWELVE = {"field": "value", "op": "eq", "value": 12, "kind": "twelve_entered"}
THIRTY_SIX = {"field": "value", "op": "eq", "value": 36}
PAYLOAD = {"goal": "multiply the number on the display by 3 and press equals",
           "verify": {"record_id": "display", "conditions": [THIRTY_SIX]}}


class Desk:
    def __init__(self, tmp):
        (tmp / "rt").mkdir()
        self.live = Live(tmp / "rt", "calc", token="dtok")
        self.h = Harness(str(tmp / "wk.db"), courses={})
        client = DesktopRuntimeClient(f"http://127.0.0.1:{self.live.port}", token="dtok")
        self.eye, self.hand = DesktopAppSource(client), DesktopCuaTool(client, self.h.clock, model_egress=EGRESS)
        self.h.ctx.connectors["desktop.macos"] = self.eye
        self.h.ctx.tools["desktop.cua"] = self.hand
        base = SystemPolicy()
        self.h.ctx.system_policy = SystemPolicy(
            allowed_capabilities=base.allowed_capabilities | {"desktop.observe", "desktop.operate"},
            allowed_data_egress=base.allowed_data_egress | {EGRESS})

    def setup(self):
        h = self.h
        with tx(h.store) as repo:
            setup_cmd.register_grant(repo, h.ctx, tenant_id=TENANT, principal=USER, grant_ref="grant_confirmed_by_user",
                                     capabilities=["desktop.observe", "inbox.notify_self", "desktop.operate"],
                                     data_egress=["model:offline-scripted", EGRESS], resource_scope={"apps": [CALC]})
        with tx(h.store) as repo:
            setup_cmd.register_source_binding(
                repo, h.ctx, tenant_id=TENANT, owner=USER, source_ref="calculator", connector_id="desktop.macos",
                source_uri="macos-app://" + CALC, resource_scope={"apps": [CALC]}, capabilities=["desktop.observe"])
        h.create(base_spec(
            task_id="calc_watch", root_task_id="calc_watch", purpose="盯着计算器：出现 12 后乘 3，确认 36 后结束",
            source={"binding_ref": "calculator", "resource_scope": SCOPE},
            observation={"completeness_required": "full_target_scope", "first_snapshot": "baseline_only",
                         "ignore_fields": [], "target_key": "record_ids"},
            completion={"evaluator": "all_targets_match", "evaluator_version": 1, "required_outputs": []},
            authority={"read_capabilities": ["desktop.observe"], "write_capabilities": ["desktop.operate"],
                       "notify_capabilities": ["inbox.notify_self"], "model_data_egress": ["model:offline-scripted",
                                                                                           EGRESS]},
            decision={"profile": "generic_watch.v1", "params": {
                "conditions": [TWELVE], "watch_fields": ["value"], "label_field": "value", "on_met": "plan",
                "complete_when": [THIRTY_SIX]}},
            limits={"max_run_seconds": 180}))
        h.run()

    def script_plan(self):
        self.h.k.model.script_plan(PlanProposal(steps=(ProposedAction(
            logical_step_id="times3", tool_id="desktop.cua", capability="desktop.operate", payload=PAYLOAD,
            reason_code="twelve_entered"),), usage=Usage(1, 1, 1), model_ref="offline-scripted"))

    def action(self):
        return [a for a in self.h.actions() if a.tool_id == "desktop.cua"][-1]

    def current(self, action):
        return self.h.all(ActionRecord, {"action_id": action.action_id})[0]

    def close(self):
        self.live.close()
        self.h.store.close()


@pytest.fixture
def desk(tmp_path):
    d = Desk(tmp_path)
    yield d
    d.close()


def test_watch_then_operate_then_verify_then_complete(desk):
    d, h, lv = desk, desk.h, desk.live
    d.setup()
    assert d.eye.fetch_count == 1 and lv.model.requests == [] and lv.app()["calls"] == []
    for _ in range(2):                                     # nothing changes: nothing happens
        h.cycle()
    assert d.eye.fetch_count == 3 and lv.model.requests == [] and h.k.model.calls == []

    d.script_plan()
    write_state(lv.state, display="12", entry="12")        # the human typed 12
    h.cycle()
    action = d.action()
    [bundle] = [b for kind, b in h.k.model.calls if kind == "plan"]
    tools = {t["tool_id"] for t in bundle.allowed_tools}
    assert "desktop.cua" in tools and "email.send" not in tools and "browser.cua" not in tools
    assert action.status is ActionStatus.WAITING_APPROVAL and lv.model.requests == [] and lv.app()["calls"] == []
    approval = h.all(Approval, {"action_id": action.action_id})[-1]
    assert approval.payload_digest == action.payload_digest
    assert EGRESS in action.data_egress and action.resource_scope["apps"] == [CALC]

    approve(h, approval)
    h.run()
    done = d.current(action)
    assert done.status is ActionStatus.CONFIRMED, done.resolution
    assert lv.app()["display"] == "36" and len(lv.app()["calls"]) == 6
    [att] = h.all(ActionAttempt, {"action_id": action.action_id})
    receipt = att.receipt["receipt"]
    assert receipt["verified"] is True and receipt["record"] == {"value": 36}      # re-read, not claimed
    assert receipt["act"]["mutating_actions"] == 6 and receipt["act"]["status"] == "completed"

    h.cycle()                                              # the eye sees the goal reached
    assert h.runtime("calc_watch").lifecycle is TaskLifecycle.COMPLETED
    h.cycle()
    assert len(lv.app()["calls"]) == 6                     # and nothing is ever re-done

    # replay recomputes from the record; with the runtime gone it touches nothing
    lv.httpd.shutdown()
    calls = len(lv.app()["calls"])
    s = replay_task(h.store, TENANT, "calc_watch").summary()
    assert s["mismatches"] == [] and len(lv.app()["calls"]) == calls
    assert len(h.all(ObservationRecord)) >= 5


def test_the_browser_client_refuses_the_desktop_runtime(desk):
    from wakecore.adapters.ui_runtime.client import ProtocolMismatch
    with pytest.raises(ProtocolMismatch) as e:
        UiRuntimeClient(f"http://127.0.0.1:{desk.live.port}", token="dtok").capabilities()
    assert e.value.got == "wakecore.desktop-runtime/1"
    assert desk.eye.client.capabilities()["act"]["action_space"] == "wakecore.desktop-ax/1"


def test_operating_an_app_outside_the_grant_is_denied(desk):
    d, h = desk, desk.h
    d.setup()
    h.k.model.script_plan(PlanProposal(steps=(ProposedAction(
        logical_step_id="finder", tool_id="desktop.cua", capability="desktop.operate",
        payload={**PAYLOAD, "app": "com.apple.finder"}, reason_code="twelve_entered"),), usage=Usage(1, 1, 1),
        model_ref="offline-scripted"))
    write_state(d.live.state, display="12", entry="12")
    h.cycle()
    for a in [a for a in h.actions() if a.tool_id == "desktop.cua"]:
        if a.status is ActionStatus.WAITING_APPROVAL:
            approve(h, h.all(Approval, {"action_id": a.action_id})[-1])
    h.run()
    assert d.live.app()["calls"] == [] and d.live.model.requests == []
    assert all(d.current(a).status is not ActionStatus.CONFIRMED for a in h.actions() if a.tool_id == "desktop.cua")
