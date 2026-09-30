"""Browser "hand" (V0.3 P2): ActionPort over the UI Runtime's Computer Use loop.

The kernel sees an ordinary EXTERNAL_WRITE tool without provider idempotency. What makes it
safe is around the model, not inside it:

  * start_url / verify.url must lie inside resource_scope.origins (checked by the kernel's
    authority via url_fields, and again by the runtime's request guard on every request)
  * the model egress ("model:openai-computer-use") is a required_egress: bound into the
    approval digest, and the planner never sees this tool unless the task was granted it.
    With an OpenAI-compatible endpoint (relay, proxy, gateway) the screenshots go to someone
    else, so the egress names that host ("model:openai-compatible:<host>", `model_egress=`),
    and every act carries it: the runtime refuses to act if it would send them elsewhere
  * the result is decided by re-observation, never by the model saying "done" (constraint 9):
        pre-verify   already satisfied  -> confirmed without touching the page
        act          the runtime drives the page (at most once per effect_key)
        post-verify  condition holds    -> confirmed
                     not held, nothing mutating was sent -> failed_no_effect
                     not held, a mutating request went out -> unknown  -> reconcile
  * reconcile only re-observes and asks the runtime's journal; it never operates the page

resource_scope.state_location = "client" (default "server"): the site keeps its state in the
browser (localStorage, IndexedDB; e.g. a TodoMVC or a client-side cart). Then "no mutating
request went out" proves nothing, so once the model has touched the page an unverified result is
`unknown`, and reconcile never turns it into no_effect by itself (no settle window): the page is
re-observed until it shows the goal, or a human resolves it.
"""
import re
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

from ..ui_runtime.client import (
    ProtocolMismatch,
    RuntimeRejected,
    RuntimeTransportError,
    RuntimeUnreachable,
    UiRuntimeClient,
    UiRuntimeError,
)

MODEL_EGRESS = "model:openai-computer-use"
COMPATIBLE_EGRESS = re.compile(r"^model:openai-compatible:[a-z0-9.:\-\[\]]{1,253}$")


def check_model_egress(egress: str) -> str:
    """MODEL_EGRESS, or model:openai-compatible:<host> as the runtime reports it in /v1/health."""
    if egress != MODEL_EGRESS and not COMPATIBLE_EGRESS.match(egress):
        raise ValueError(f"model egress must be {MODEL_EGRESS!r} or 'model:openai-compatible:<host>', got {egress!r}")
    return egress

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["goal", "start_url", "verify"],
    "properties": {
        "goal": {"type": "string", "maxLength": 2000},
        "start_url": {"type": "string", "maxLength": 2000},
        "verify": {
            "type": "object", "required": ["record_id", "conditions"],
            "properties": {
                "record_id": {"type": "string", "maxLength": 200},
                "url": {"type": "string", "maxLength": 2000},
                "conditions": {"type": "array", "items": {
                    "type": "object", "required": ["field", "op"],
                    "properties": {"field": {"type": "string", "maxLength": 100},
                                   "op": {"type": "string", "enum": sorted(OPS)},
                                   "value": {"type": ["string", "integer", "number", "boolean", "null"]}}}},
            },
        },
    },
}


class BrowserCuaTool:
    descriptor = ToolDescriptor(
        tool_id="browser.cua", tool_version="0.3.0", input_schema=INPUT_SCHEMA, output_schema={"type": "object"},
        capability_type="browser.operate", allowed_resource_kinds=("origin",),
        side_effect_class=SideEffectClass.EXTERNAL_WRITE, supports_idempotency=False,
        idempotency_retention_seconds=0, supports_reconciliation=True, confirmation_semantics="re_observation",
        max_duration_seconds=600, url_fields=("start_url", "verify.url"), required_egress=(MODEL_EGRESS,),
        credential="source_binding")

    def __init__(self, client: UiRuntimeClient, clock: Any, *, settle_seconds: int = 120, max_steps: int = 15,
                 target_key: str = "record_ids", verify_reserve: int = 10, recheck_seconds: int = 60,
                 model_egress: str = MODEL_EGRESS) -> None:
        self.client, self.clock = client, clock
        self.model_egress = check_model_egress(model_egress)
        if model_egress != MODEL_EGRESS:
            self.descriptor = replace(BrowserCuaTool.descriptor, required_egress=(model_egress,))
        self.settle_seconds, self.max_steps, self.target_key = settle_seconds, max_steps, target_key
        self.verify_reserve, self.recheck_seconds = verify_reserve, recheck_seconds
        self._lock = threading.Lock()
        self._last_check: dict[str, datetime] = {}
        self.execute_calls = 0
        self.act_calls = 0

    # ------------------------------------------------------------------ helpers
    def _target(self, payload: dict[str, Any], scope: dict[str, Any]) -> tuple[str, dict[str, Any], list[str]]:
        v = payload["verify"]
        return v.get("url") or scope.get("url") or payload["start_url"], scope.get("extractor") or {}, \
            list(scope.get("origins") or [])

    def _check(self, session: str, payload: dict[str, Any], scope: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """-> (held | not_held | auth | error, details). Deterministic Playwright read, no model."""
        url, extractor, origins = self._target(payload, scope)
        v = payload["verify"]
        try:
            r = self.client.verify(session_ref=session, url=url, origins=origins, extractor=extractor,
                                   record_id=v["record_id"])
        except UiRuntimeError as e:
            return "error", {"error": type(e).__name__}
        if r.get("outcome") == "AUTH_REQUIRED":
            return "auth", {"error_code": r.get("error_code")}
        if r.get("outcome") != "SUCCESS":
            return "error", {"outcome": r.get("outcome"), "error_code": r.get("error_code")}
        rec = r.get("record")
        held = r.get("present") and all(condition_holds(c, rec) for c in v["conditions"])
        return ("held" if held else "not_held"), {"record": rec, "content_digest": r.get("content_digest")}

    @staticmethod
    def _login(scope: dict[str, Any]) -> Optional[dict[str, Any]]:
        login = (scope.get("extractor") or {}).get("login")
        return login if isinstance(login, dict) else None

    @staticmethod
    def _client_state(scope: dict[str, Any]) -> bool:
        return scope.get("state_location") == "client"

    def _status_after_error(self, key: str, client_state: bool = False) -> ActionResult:
        """The act call failed in a way that may or may not have reached the page: ask the journal."""
        try:
            st = self.client.act_status(key)
        except UiRuntimeError:
            return ActionResult(status="unknown", provider_request_id=key, error="ui_runtime_lost")
        if st.get("status") == "never_received":
            return ActionResult(status="failed_no_effect", error="ui_runtime_error_before_act")
        if st.get("status") != "in_progress" and not st.get("mutating_requests") and not client_state:
            return ActionResult(status="failed_no_effect", provider_request_id=key,
                                error=f"act_{st.get('result_status') or st.get('status')}")
        return ActionResult(status="unknown", provider_request_id=key, error="ui_runtime_lost")

    # ------------------------------------------------------------------ ActionPort
    def execute(self, request: AuthorizedAction) -> ActionResult:
        self.execute_calls += 1
        payload, scope, key = request.payload, request.resource_scope, request.effect_key
        v = payload["verify"]
        targets = scope.get(self.target_key)
        if isinstance(targets, list) and v["record_id"] not in targets:
            return ActionResult(status="failed_no_effect", error="record_outside_scope")
        if not scope.get("extractor") or not scope.get("origins"):
            return ActionResult(status="failed_no_effect", error="scope_needs_origins_extractor")
        if not request.secret:
            return ActionResult(status="failed_no_effect", error="missing_session")
        budget = float(self.descriptor.max_duration_seconds)
        if request.deadline_at is not None:
            budget = min(budget, (request.deadline_at - self.clock.utc_now()).total_seconds())
        timeout_s = budget - self.verify_reserve
        if timeout_s < 5:
            return ActionResult(status="failed_no_effect", error="deadline_too_close")

        state, pre = self._check(request.secret, payload, scope)
        if state == "held":
            return ActionResult(status="confirmed", provider_request_id=key,
                                receipt={"verified": True, "already_satisfied": True, **pre})
        if state == "auth":
            return ActionResult(status="failed_no_effect", error="auth_required")
        if state == "error":
            return ActionResult(status="failed_no_effect", error="pre_verify_failed")

        self.act_calls += 1
        try:
            r = self.client.act(key=key, attempt=request.attempt_id, session_ref=request.secret,
                                goal=payload["goal"], start_url=payload["start_url"], origins=list(scope["origins"]),
                                login=self._login(scope), timeout_s=timeout_s, max_steps=self.max_steps,
                                model_egress=self.model_egress)
        except RuntimeUnreachable:
            return ActionResult(status="failed_no_effect", error="ui_runtime_unreachable")
        except ProtocolMismatch:               # refused before anything was sent
            return ActionResult(status="failed_no_effect", error="ui_runtime_protocol_mismatch")
        except RuntimeRejected as e:
            if e.code == "model_egress_mismatch":   # refused before the model or the page was touched
                return ActionResult(status="failed_no_effect", error="model_egress_mismatch",
                                    receipt={"runtime_model_egress": e.details.get("runtime_model_egress"),
                                             "approved_model_egress": self.model_egress})
            if e.status < 500:
                return ActionResult(status="failed_no_effect", error=f"ui_runtime_rejected_{e.status}")
            return self._status_after_error(key, self._client_state(scope))
        except RuntimeTransportError:
            return self._status_after_error(key, self._client_state(scope))
        if r.get("status") == "in_progress":   # another attempt of this effect is running right now
            return ActionResult(status="unknown", provider_request_id=key, error="act_in_progress")

        mutating = int(r.get("mutating_requests") or 0)
        act = {k: r.get(k) for k in ("status", "reason", "steps", "actions_executed", "blocked_requests",
                                     "final_url", "artifacts_ref", "model_ref", "deduplicated", "usage", "model_calls",
                                     "error_detail", "summary") if k in r}
        act["mutating_requests"] = mutating
        state, post = self._check(request.secret, payload, scope)
        if state == "held":
            return ActionResult(status="confirmed", provider_ref=r.get("artifacts_ref"), provider_request_id=key,
                                receipt={"verified": True, "act": act, **post})
        touched = int(r.get("actions_executed") or 0) > 0
        if mutating == 0 and not (self._client_state(scope) and touched):
            return ActionResult(status="failed_no_effect", provider_request_id=key,
                                error=f"act_{r.get('status')}:{r.get('reason') or 'goal_not_reached'}",
                                receipt={"act": act, **post})
        return ActionResult(status="unknown", provider_request_id=key,
                            error="submitted_but_not_verified" if state == "not_held" else "post_verify_failed",
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
        if not payload or not request.secret:
            return ReconcileResult(status="still_unknown", evidence={"error": "cannot_re_observe"})
        state, obs = self._check(request.secret, payload, scope)
        if state == "held":
            return ReconcileResult(status="confirmed", evidence={"verified": True, **obs})
        if state != "not_held":
            return ReconcileResult(status="still_unknown", evidence=obs)
        try:
            st = self.client.act_status(key)
        except UiRuntimeError:
            return ReconcileResult(status="still_unknown", evidence={"error": "ui_runtime_unreachable"})
        journal = {"journal": st.get("status"), "mutating_requests": st.get("mutating_requests", 0)}
        if st.get("status") == "in_progress":
            return ReconcileResult(status="still_unknown", evidence=journal)
        if st.get("status") == "never_received":
            return ReconcileResult(status="no_effect", evidence={**journal, **obs})
        if self._client_state(scope):   # the effect may live in the browser: only the page can tell
            return ReconcileResult(status="still_unknown", evidence={**journal, **obs, "client_state": True})
        if not st.get("mutating_requests"):
            return ReconcileResult(status="no_effect", evidence={**journal, **obs})
        attempted = request.attempted_at
        if attempted is not None and now - attempted >= timedelta(seconds=self.settle_seconds):
            # a submit went out but the page still shows the goal unmet after the settle window:
            # the effect did not happen (e.g. someone else took the last seat)
            return ReconcileResult(status="no_effect", evidence={**journal, **obs, "settled": True})
        return ReconcileResult(status="still_unknown", evidence={**journal, **obs})
