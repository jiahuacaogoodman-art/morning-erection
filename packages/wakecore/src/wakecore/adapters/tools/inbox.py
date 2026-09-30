"""Local inbox tool: the only notification channel of the first release (RFC §1.3).

Idempotent by effect_key: the message id is derived from (tenant, effect_key) and inserted
with insert-if-absent, so a re-dispatch after a lost response cannot notify twice.
"""
from typing import Any, Optional

from wakecore.kernel.domain.canonical import short_hash
from wakecore.kernel.domain.enums import SideEffectClass
from wakecore.kernel.domain.model import InboxMessage
from wakecore.kernel.locks import tx
from wakecore.kernel.ports.action import (
    ActionResult,
    AuthorizedAction,
    ReconcileRequest,
    ReconcileResult,
    ToolDescriptor,
)
from wakecore.kernel.ports.store import StateStore

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["recipient", "title", "body"],
    "properties": {
        "recipient": {"type": "string", "enum": ["self"]},
        "title": {"type": "string", "maxLength": 200},
        "body": {"type": "string", "maxLength": 4000},
        "data": {"type": "object", "additionalProperties": True},
    },
}


def message_id(tenant_id: str, effect_key: str) -> str:
    return "msg_" + short_hash(tenant_id, effect_key)


class InboxTool:
    descriptor = ToolDescriptor(
        tool_id="inbox.notify", tool_version="1.0.0", input_schema=INPUT_SCHEMA,
        output_schema={"type": "object", "properties": {"message_id": {"type": "string"}}},
        capability_type="inbox.notify_self", allowed_resource_kinds=("inbox",),
        side_effect_class=SideEffectClass.LOCAL_WRITE, supports_idempotency=True,
        idempotency_retention_seconds=365 * 86400, supports_reconciliation=True,
        confirmation_semantics="committed_row", max_duration_seconds=5)

    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.executions = 0

    def execute(self, request: AuthorizedAction) -> ActionResult:
        self.executions += 1
        mid = message_id(request.tenant_id, request.effect_key)
        with tx(self.store) as repo:
            repo.insert_if_absent(InboxMessage(
                tenant_id=request.tenant_id, message_id=mid, effect_key=request.effect_key,
                task_id=request.task_id, title=request.payload["title"], body=request.payload["body"],
                data=request.payload.get("data", {}), created_at=repo.uow.db_now()))
        return ActionResult(status="confirmed", provider_ref=mid, provider_request_id=mid,
                            receipt={"message_id": mid})

    def reconcile(self, request: ReconcileRequest) -> ReconcileResult:
        mid = message_id(request.tenant_id, request.effect_key)
        with tx(self.store) as repo:
            found: Optional[InboxMessage] = repo.get(InboxMessage, tenant_id=request.tenant_id, message_id=mid)
        if found is None:
            return ReconcileResult(status="no_effect", evidence={"message_id": mid, "found": False})
        return ReconcileResult(status="confirmed", evidence={"message_id": mid, "found": True})
