"""Fake external e-mail provider (external_write) for T09-T12.

Keeps its own record of delivered messages keyed by idempotency key, can lose the
response after a successful send, and supports reconciliation by effect_key.
"""
import threading
from typing import Any

from wakecore.kernel.domain.enums import SideEffectClass
from wakecore.kernel.ports.action import (
    ActionResult,
    AuthorizedAction,
    ReconcileRequest,
    ReconcileResult,
    ToolDescriptor,
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["to", "subject", "body"],
    "properties": {
        "to": {"type": "array", "items": {"type": "string", "maxLength": 320}},
        "subject": {"type": "string", "maxLength": 300},
        "body": {"type": "string", "maxLength": 20000},
        "attachments": {"type": "array", "items": {"type": "string", "maxLength": 200}},
    },
}


class LostResponse(ConnectionError):
    """The provider accepted the request but the response never reached us."""


class FakeEmailProvider:
    descriptor = ToolDescriptor(
        tool_id="email.send", tool_version="1.0.0", input_schema=INPUT_SCHEMA,
        output_schema={"type": "object"}, capability_type="email.send", allowed_resource_kinds=("mailbox",),
        side_effect_class=SideEffectClass.EXTERNAL_WRITE, supports_idempotency=True,
        idempotency_retention_seconds=86400, supports_reconciliation=True,
        confirmation_semantics="provider_message_id", max_duration_seconds=30, data_egress_policy="recipient")

    def __init__(self, *, supports_reconciliation: bool = True) -> None:
        self._lock = threading.Lock()
        self.sent: dict[str, dict[str, Any]] = {}  # effect_key -> message
        self.send_calls = 0
        self.lose_response_times = 0
        self.fail_no_effect_times = 0
        if not supports_reconciliation:
            import dataclasses

            self.descriptor = dataclasses.replace(self.descriptor, supports_reconciliation=False)

    def execute(self, request: AuthorizedAction) -> ActionResult:
        with self._lock:
            self.send_calls += 1
            if self.fail_no_effect_times > 0:
                self.fail_no_effect_times -= 1
                return ActionResult(status="failed_no_effect", error="rejected_by_provider")
            msg = self.sent.get(request.effect_key)
            if msg is None:  # provider-side idempotency on the effect key
                msg = {"provider_ref": f"prov-{len(self.sent) + 1}", "payload": dict(request.payload),
                       "digest": request.payload_digest}
                self.sent[request.effect_key] = msg
            if self.lose_response_times > 0:
                self.lose_response_times -= 1
                raise LostResponse("connection reset after provider accepted the message")
        return ActionResult(status="confirmed", provider_ref=msg["provider_ref"],
                            provider_request_id=request.effect_key, receipt={"provider_ref": msg["provider_ref"]})

    def reconcile(self, request: ReconcileRequest) -> ReconcileResult:
        with self._lock:
            msg = self.sent.get(request.effect_key)
        if msg is None:
            return ReconcileResult(status="no_effect", evidence={"effect_key": request.effect_key})
        return ReconcileResult(status="confirmed", evidence={"provider_ref": msg["provider_ref"]})
