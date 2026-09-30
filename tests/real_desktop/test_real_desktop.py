"""D1/D2 on the real Calculator: "盯着计算器，出现 12 以后帮我乘 3，确认是 36 再结束。"

D1 (no key needed): the kernel's watch reads the real Calculator's accessibility tree through the
real bridge; nothing is operated.
D2 (key needed): a human stand-in (the test, through the bridge directly, outside the kernel)
enters 12; the kernel sees it, plans (scripted planner), is approved, the real model operates the
real Calculator through the guarded desktop loop, and a deterministic re-read confirms 36.
"""
import os

import pytest
from real_desktop_env import RealDesktopRuntime, has_key, mcp_command

from harness import TENANT, USER, Harness, approve, base_spec
from wakecore.adapters.sources.desktop_app import DesktopAppSource
from wakecore.adapters.tools.desktop_cua import DesktopCuaTool
from wakecore.adapters.ui_runtime.desktop_client import DesktopRuntimeClient
from wakecore.kernel.commands import setup as setup_cmd
from wakecore.kernel.context import SystemPolicy
from wakecore.kernel.domain.enums import ActionStatus, ObservationOutcome, TaskLifecycle
from wakecore.kernel.domain.model import ActionAttempt, ActionRecord, Approval, ObservationRecord
from wakecore.kernel.locks import tx
from wakecore.kernel.ports.reasoning import PlanProposal, ProposedAction, Usage
from wakecore_ui_runtime.desktop.mcp import McpBridge
from wakecore_ui_runtime.desktop.tree import parse

CALC = "com.apple.calculator"
# locale-independent: the display's text node is "text …" in English and "文本 …" in Chinese
CALC_X = {"ready": {"id": "StandardInputView"},
          "records": [{"id": "display", "columns": [
              {"field": "value", "node": {"under": {"id": "StandardInputView"}, "head_regex": "^(text|文本) "},
               "pattern": r"(-?[0-9][0-9.,]*)\s*$", "group": 1, "type": "number"}]}]}
SCOPE = {"apps": [CALC], "app": CALC, "extractor": CALC_X, "record_ids": ["display"]}
TWELVE = {"field": "value", "op": "eq", "value": 12, "kind": "twelve_entered"}
THIRTY_SIX = {"field": "value", "op": "eq", "value": 36}
PAYLOAD = {"goal": "Multiply the number currently on the display by 3 and press equals, so the display shows "
                   "the result. Do not clear the display first.",
           "verify": {"record_id": "display", "conditions": [THIRTY_SIX]}}


def human_enters(*ids: str) -> None:
    """The human stand-in: clicks Calculator buttons by accessibility ID (a|b: whichever is shown),
    outside the kernel."""
    cmd = [os.path.expanduser(p) for p in mcp_command().split()]
    bridge = McpBridge(cmd, init_timeout=30)
    try:
        for ident in ids:
            r = bridge.call("get_app_state", {"app": CALC, "disableDiff": True}, timeout=60)
            node = next(n for n in parse(r["text"]).nodes if n.id in ident.split("|"))
            bridge.call("click", {"app": CALC, "element_index": str(node.index)}, timeout=60)
    finally:
        bridge.close()


class Desk:
    def __init__(self, tmp, *, with_key):
        tmp.mkdir(parents=True, exist_ok=True)
        state = tmp / "desktop-state"
        state.mkdir(mode=0o700)
        self.rt = RealDesktopRuntime(state, with_key=with_key).start()
        self.h = Harness(str(tmp / "wk.db"), courses={})
        self.client = DesktopRuntimeClient(self.rt.url, token=self.rt.token, timeout=120)
        self.egress = self.client.health()["model_egress"]          # who receives the tree and screenshots
        self.eye = DesktopAppSource(self.client)
        self.hand = DesktopCuaTool(self.client, self.h.clock, model_egress=self.egress)
        self.h.ctx.connectors["desktop.macos"] = self.eye
        self.h.ctx.tools["desktop.cua"] = self.hand
        base = SystemPolicy()
        self.h.ctx.system_policy = SystemPolicy(
            allowed_capabilities=base.allowed_capabilities | {"desktop.observe", "desktop.operate"},
            allowed_data_egress=base.allowed_data_egress | {self.egress})

    def setup(self):
        h, egress = self.h, self.egress
        with tx(h.store) as repo:
            setup_cmd.register_grant(repo, h.ctx, tenant_id=TENANT, principal=USER, grant_ref="grant_confirmed_by_user",
                                     capabilities=["desktop.observe", "inbox.notify_self", "desktop.operate"],
                                     data_egress=["model:offline-scripted", egress], resource_scope={"apps": [CALC]})
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
                       "notify_capabilities": ["inbox.notify_self"],
                       "model_data_egress": ["model:offline-scripted", egress]},
            decision={"profile": "generic_watch.v1", "params": {
                "conditions": [TWELVE], "watch_fields": ["value"], "label_field": "value", "on_met": "plan",
                "complete_when": [THIRTY_SIX]}},
            limits={"max_run_seconds": 600}))
        h.run()

    def observations(self):
        return self.h.all(ObservationRecord)

    def close(self):
        self.rt.stop()
        self.h.store.close()


@pytest.fixture
def desk_factory(tmp_path):
    made = []

    def make(**kw):
        d = Desk(tmp_path / f"d{len(made)}", **kw)
        made.append(d)
        return d
    yield make
    for d in made:
        d.close()


def test_d1_the_eye_reads_the_real_calculator(desk_factory, report):
    d = desk_factory(with_key=False)
    health = d.client.health()
    d.setup()
    d.h.cycle()
    obs = d.observations()
    entry = {"scenario": "D1 眼：真实计算器（无模型）", "bridge": health.get("bridge"),
             "outcomes": [o.outcome.value for o in obs], "fetches": d.eye.fetch_count}
    report.add({**entry, "verdict": "PASS" if obs and all(o.outcome is ObservationOutcome.SUCCESS for o in obs)
                else "FAIL"})
    assert health["bridge"]["ok"], health
    assert obs and all(o.outcome is ObservationOutcome.SUCCESS for o in obs), entry
    assert d.eye.fetch_count >= 1          # one read is enough if the display already shows 36 (goal met)


@pytest.mark.skipif(not has_key(), reason="no key in .secrets/openai.key")
def test_d2_watch_then_multiply_by_3_then_verify(desk_factory, report):
    d = desk_factory(with_key=True)
    h = d.h
    human_enters("Clear|AllClear", "Clear|AllClear")                           # a known baseline: 0
    d.setup()
    h.k.model.script_plan(PlanProposal(steps=(ProposedAction(
        logical_step_id="times3", tool_id="desktop.cua", capability="desktop.operate", payload=PAYLOAD,
        reason_code="twelve_entered"),), usage=Usage(1, 1, 1), model_ref="offline-scripted"))
    human_enters("One", "Two")                               # the human types 12
    h.cycle()
    [action] = [a for a in h.actions() if a.tool_id == "desktop.cua"]
    assert action.status is ActionStatus.WAITING_APPROVAL
    assert d.egress in action.data_egress and action.resource_scope["apps"] == [CALC]
    approve(h, h.all(Approval, {"action_id": action.action_id})[-1])
    h.run()
    done = h.all(ActionRecord, {"action_id": action.action_id})[0]
    attempts = h.all(ActionAttempt, {"action_id": action.action_id})
    receipt = attempts[-1].receipt.get("receipt", {}) if attempts else {}
    act = receipt.get("act") or {}
    h.cycle()
    lifecycle = h.runtime("calc_watch").lifecycle
    entry = {"scenario": "D2 手：12 → ×3 → 36（真实模型 + 真实计算器）", "action_status": done.status.value,
             "resolution": done.resolution, "verified_record": receipt.get("record"),
             "act_status": act.get("status"), "act_reason": act.get("reason"), "error_detail": act.get("error_detail"), "steps": act.get("steps"),
             "mutating_actions": act.get("mutating_actions"), "blocked": act.get("blocked"),
             "usage": act.get("usage"), "summary": act.get("summary"), "artifacts": act.get("artifacts_ref"),
             "task_lifecycle": lifecycle.value}
    ok = done.status is ActionStatus.CONFIRMED and lifecycle is TaskLifecycle.COMPLETED
    report.add({**entry, "verdict": "PASS" if ok else "FAIL"})
    assert done.status is ActionStatus.CONFIRMED, entry
    assert receipt["verified"] is True and receipt["record"]["value"] == 36     # re-read, not claimed
    assert lifecycle is TaskLifecycle.COMPLETED, entry
