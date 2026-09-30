"""Tool/action gateway port (RFC §13.2, §13.3)."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Protocol

from ..domain.enums import SideEffectClass


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    tool_id: str
    tool_version: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    capability_type: str
    allowed_resource_kinds: tuple[str, ...]
    side_effect_class: SideEffectClass
    supports_idempotency: bool
    idempotency_retention_seconds: int
    supports_reconciliation: bool
    confirmation_semantics: str
    max_duration_seconds: int
    retry_owner: str = "wakecore"
    data_egress_policy: str = "none"
    # V0.3 (additive): payload fields holding URLs whose origin must lie inside the action's
    # resource_scope["origins"]; egress targets the tool itself implies (e.g. a hosted
    # computer-use model sees screenshots); and whether the tool needs the task's source
    # credential handle (a browser session_ref, never a password or cookie).
    url_fields: tuple[str, ...] = ()
    required_egress: tuple[str, ...] = ()
    credential: str = "none"  # none | source_binding


@dataclass(frozen=True, slots=True)
class AuthorizedAction:
    """Built only by the kernel dispatch path after T5; never from model JSON (RFC §13.3)."""

    tenant_id: str
    action_id: str
    attempt_id: str
    effect_key: str
    task_id: str
    tool_id: str
    tool_version: str
    payload: dict[str, Any]
    payload_digest: str
    revocation_epoch: int
    permit: str
    secret: Optional[str] = None
    # V0.3 (additive): the authorisation domain the tool must stay inside, bound by the digest.
    resource_scope: dict[str, Any] = field(default_factory=dict)
    data_egress: tuple[str, ...] = ()
    deadline_at: Optional[datetime] = None


@dataclass(frozen=True, slots=True)
class ActionResult:
    status: str  # confirmed | failed_no_effect | unknown
    provider_ref: Optional[str] = None
    provider_request_id: Optional[str] = None
    receipt: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ReconcileRequest:
    tenant_id: str
    action_id: str
    effect_key: str
    tool_id: str
    payload_digest: str
    provider_request_id: Optional[str] = None
    # V0.3 (additive): what reconciliation needs to re-observe the effect instead of re-doing it.
    payload: dict[str, Any] = field(default_factory=dict)
    resource_scope: dict[str, Any] = field(default_factory=dict)
    attempted_at: Optional[datetime] = None
    requested_at: Optional[datetime] = None
    secret: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    status: str  # confirmed | no_effect | still_unknown
    evidence: dict[str, Any] = field(default_factory=dict)


class ActionPort(Protocol):
    descriptor: ToolDescriptor

    def execute(self, request: AuthorizedAction) -> ActionResult: ...

    def reconcile(self, request: ReconcileRequest) -> ReconcileResult: ...
