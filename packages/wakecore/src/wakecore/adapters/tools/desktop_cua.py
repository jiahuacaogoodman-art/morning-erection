"""macOS app "hand": ActionPort over the Desktop Runtime's guarded model loop.

The kernel sees an ordinary EXTERNAL_WRITE tool without provider idempotency, like browser.cua.
What makes it safe is around the model:

  * the app is a bundle ID that must be in resource_scope.apps (payload.app, default
    resource_scope.app); the scope is bound into the approval digest and the runtime checks it
    again, and refuses an observation that comes back from any other app
  * the model egress is a required_egress (bound into the approval, and the planner never sees
    this tool unless the task was granted it); the app's accessibility tree and screenshots go
    to that model, secure text field values never do
  * the runtime refuses credential fields, system-wide key combinations, pixel coordinates and
    anything outside the latest tree; it journals every action before sending it
  * the result is decided by re-observation, never by the model saying "done" (constraint 9):
        pre-verify   already satisfied -> confirmed without touching the app
        act          the runtime drives the app (at most once per effect_key)
        post-verify  condition holds   -> confirmed
                     not held, no action was sent -> failed_no_effect
                     not held, an action was sent -> unknown -> reconcile
  * reconcile only re-observes and asks the runtime's journal; it never operates the app

A desktop app keeps its state locally, so "an action was sent" cannot be settled by waiting:
an unknown effect stays unknown until a re-observation shows the goal, or a human resolves it.
"""
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Optional

from wakecore.kernel.decision.profiles import OPS, condition_holds
from wakecore.kernel.domain.enums import SideEffectClass
from wakecore.kernel.ports.action import (
    ActionResult,
    AuthorizedAction,
    ReconcileRequest,
    ReconcileResult,
    ToolDescriptor,
)

from ..sources.desktop_app import app_scope
from ..ui_runtime.client import (
    ProtocolMismatch,
    RuntimeRejected,
    RuntimeTransportError,
    RuntimeUnreachable,
    UiRuntimeError,
)
from ..ui_runtime.desktop_client import DesktopRuntimeClient
from .browser_cua import MODEL_EGRESS, check_model_egress

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["goal", "verify"],
    "properties": {
        "goal": {"type": "string", "maxLength": 2000},
        "app": {"type": "string", "maxLength": 255},
        "verify": {
            "type": "object", "required": ["record_id", "conditions"],
            "properties": {
                "record_id": {"type": "string", "maxLength": 200},
                "conditions": {"type": "array", "items": {
                    "type": "object", "required": ["field", "op"],
                    "properties": {"field": {"type": "string", "maxLength": 100},
                                   "op": {"type": "string", "enum": sorted(OPS)},
                                   "value": {"type": ["string", "integer", "number", "boolean", "null"]}}}},
            },
        },
    },
}


class DesktopCuaTool:
    descriptor = ToolDescriptor(
        tool_id="desktop.cua", tool_version="0.3.0", input_schema=INPUT_SCHEMA, output_schema={"type": "object"},
        capability_type="desktop.operate", allowed_resource_kinds=("app",),
        side_effect_class=SideEffectClass.EXTERNAL_WRITE, supports_idempotency=False,
        idempotency_retention_seconds=0, supports_reconciliation=True, confirmation_semantics="re_observation",
        max_duration_seconds=600, required_egress=(MODEL_EGRESS,), credential="none")

    def __init__(self, client: DesktopRuntimeClient, clock: Any, *, max_steps: int = 15,
                 target_key: str = "record_ids", verify_reserve: int = 10, recheck_seconds: int = 60,
                 model_egress: str = MODEL_EGRESS) -> None:
        self.client, self.clock = client, clock
        self.model_egress = check_model_egress(model_egress)
        if model_egress != MODEL_EGRESS:
            self.descriptor = replace(DesktopCuaTool.descriptor, required_egress=(model_egress,))
        self.max_steps, self.target_key = max_steps, target_key
        self.verify_reserve, self.recheck_seconds = verify_reserve, recheck_seconds
        self._lock = threading.Lock()
        self._last_check: dict[str, datetime] = {}
        self.execute_calls = 0
        self.act_calls = 0

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _app(payload: dict[str, Any], scope: dict[str, Any]) -> tuple[Optional[str], list[str]]:
        app, apps = app_scope(scope)
        if app is None:
            return None, []
        wanted = payload.get("app") or app
        return (wanted, apps) if wanted in apps else (None, [])

    def _check(self, payload: dict[str, Any], scope: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """-> (held | not_held | auth | error, details). Deterministic accessibility read, no model."""
        app, apps = self._app(payload, scope)
        if app is None:
            return "error", {"error": "app_outside_scope"}
        v = payload["verify"]
        try:
            r = self.client.verify(app=app, apps=apps, extractor=scope.get("extractor") or {},
                                   record_id=v["record_id"])
        except UiRuntimeError as e:
            return "error", {"error": getattr(e, "code", type(e).__name__)}
        if r.get("outcome") == "AUTH_REQUIRED":
            return "auth", {"error_code": r.get("error_code")}
        if r.get("outcome") != "SUCCESS":
            return "error", {"outcome": r.get("outcome"), "error_code": r.get("error_code")}
        rec = r.get("record")
        held = r.get("present") and all(condition_holds(c, rec) for c in v["conditions"])
        return ("held" if held else "not_held"), {"record": rec, "content_digest": r.get("content_digest")}

    def _status_after_error(self, key: str) -> ActionResult:
        """The act call failed in a way that may or may not have reached the app: ask the journal."""
        try:
            st = self.client.act_status(key)
        except UiRuntimeError:
            return ActionResult(status="unknown", provider_request_id=key, error="desktop_runtime_lost")
        if st.get("status") == "never_received":
            return ActionResult(status="failed_no_effect", error="desktop_runtime_error_before_act")
        if st.get("status") != "in_progress" and not st.get("mutating_actions"):
            return ActionResult(status="failed_no_effect", provider_request_id=key,
                                error=f"act_{st.get('result_status') or st.get('status')}")
        return ActionResult(status="unknown", provider_request_id=key, error="desktop_runtime_lost")

    # ------------------------------------------------------------------ ActionPort
    def execute(self, request: AuthorizedAction) -> ActionResult:
        self.execute_calls += 1
        payload, scope, key = request.payload, request.resource_scope, request.effect_key
        v = payload["verify"]
        targets = scope.get(self.target_key)
        if isinstance(targets, list) and v["record_id"] not in targets:
            return ActionResult(status="failed_no_effect", error="record_outside_scope")
        if not isinstance(scope.get("extractor"), dict):
            return ActionResult(status="failed_no_effect", error="scope_needs_extractor")
        app, apps = self._app(payload, scope)
        if app is None:
            return ActionResult(status="failed_no_effect", error="app_outside_scope")
        budget = float(self.descriptor.max_duration_seconds)
        if request.deadline_at is not None:
            budget = min(budget, (request.deadline_at - self.clock.utc_now()).total_seconds())
        timeout_s = budget - self.verify_reserve
        if timeout_s < 5:
            return ActionResult(status="failed_no_effect", error="deadline_too_close")

        state, pre = self._check(payload, scope)
        if state == "held":
            return ActionResult(status="confirmed", provider_request_id=key,
                                receipt={"verified": True, "already_satisfied": True, **pre})
        if state == "auth":
            return ActionResult(status="failed_no_effect", error="auth_required")
        if state == "error":
            return ActionResult(status="failed_no_effect", error="pre_verify_failed", receipt=pre)

        self.act_calls += 1
        try:
            r = self.client.act(key=key, attempt=request.attempt_id, app=app, apps=apps, goal=payload["goal"],
                                timeout_s=timeout_s, max_steps=self.max_steps, model_egress=self.model_egress)
        except RuntimeUnreachable:
            return ActionResult(status="failed_no_effect", error="desktop_runtime_unreachable")
        except ProtocolMismatch:               # refused before anything was sent
            return ActionResult(status="failed_no_effect", error="desktop_runtime_protocol_mismatch")
        except RuntimeRejected as e:
            if e.code == "model_egress_mismatch":   # refused before the model or the app was touched
                return ActionResult(status="failed_no_effect", error="model_egress_mismatch",
                                    receipt={"runtime_model_egress": e.details.get("runtime_model_egress"),
                                             "approved_model_egress": self.model_egress})
            if e.status < 500:
                return ActionResult(status="failed_no_effect", error=f"desktop_runtime_rejected_{e.status}")
            return self._status_after_error(key)
        except RuntimeTransportError:
            return self._status_after_error(key)
        if r.get("status") == "in_progress":   # another attempt of this effect is running right now
            return ActionResult(status="unknown", provider_request_id=key, error="act_in_progress")

        mutating = int(r.get("mutating_actions") or 0)
        act = {k: r.get(k) for k in ("status", "reason", "steps", "actions_executed", "blocked_actions", "blocked",
                                     "final_window", "artifacts_ref", "model_ref", "deduplicated", "usage",
                                     "model_calls", "error_detail", "summary") if k in r}
        act["mutating_actions"] = mutating
        state, post = self._check(payload, scope)
        if state == "held":
            return ActionResult(status="confirmed", provider_ref=r.get("artifacts_ref"), provider_request_id=key,
                                receipt={"verified": True, "act": act, **post})
        if mutating == 0:
            return ActionResult(status="failed_no_effect", provider_request_id=key,
                                error=f"act_{r.get('status')}:{r.get('reason') or 'goal_not_reached'}",
                                receipt={"act": act, **post})
        return ActionResult(status="unknown", provider_request_id=key,
                            error="sent_but_not_verified" if state == "not_held" else "post_verify_failed",
                            receipt={"act": act, **post})

    def reconcile(self, request: ReconcileRequest) -> ReconcileResult:
        key = request.effect_key
        now = request.requested_at or self.clock.utc_now()
        with self._lock:
            last = self._last_check.get(key)
            if last is not None and now - last < timedelta(seconds=self.recheck_seconds):
                return ReconcileResult(status="still_unknown", evidence={"throttled": True})
            self._last_check[key] = now
        payload, scope = request.payload, request.resource_scope
        if not payload:
            return ReconcileResult(status="still_unknown", evidence={"error": "cannot_re_observe"})
        state, obs = self._check(payload, scope)
        if state == "held":
            return ReconcileResult(status="confirmed", evidence={"verified": True, **obs})
        if state != "not_held":
            return ReconcileResult(status="still_unknown", evidence=obs)
        try:
            st = self.client.act_status(key)
        except UiRuntimeError:
            return ReconcileResult(status="still_unknown", evidence={"error": "desktop_runtime_unreachable"})
        journal = {"journal": st.get("status"), "mutating_actions": st.get("mutating_actions", 0)}
        if st.get("status") == "in_progress":
            return ReconcileResult(status="still_unknown", evidence=journal)
        if st.get("status") == "never_received" or not st.get("mutating_actions"):
            return ReconcileResult(status="no_effect", evidence={**journal, **obs})
        # an action reached the app and its state is local: only the app can tell, and it does not yet
        return ReconcileResult(status="still_unknown", evidence={**journal, **obs})
