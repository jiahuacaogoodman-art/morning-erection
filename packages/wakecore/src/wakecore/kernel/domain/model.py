"""Persistent domain records (RFC §4, §11.1).

Each dataclass maps 1:1 to a logical table declared in `kernel.ports.tables`.
Field types are restricted to str/int/bool/datetime/StrEnum/JSON (dict|list) so that
the same record can be stored by every StateStore adapter.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from .enums import (
    ActionStatus,
    ApprovalDecision,
    CatchupPolicy,
    Completeness,
    DeliveryStatus,
    ModelCallStatus,
    ObservationOutcome,
    OutboxStatus,
    ReservationStatus,
    Route,
    RunStatus,
    SourceHealth,
    SpecStatus,
    StepKind,
    StepStatus,
    TaskLifecycle,
    TriggerKind,
    TriggerStatus,
    WaitReason,
    WaitStatus,
)

JSON = dict[str, Any]
JSONList = list[Any]


@dataclass(frozen=True, slots=True)
class Grant:
    """A user-confirmed authorisation. Never created from external content (K-02)."""

    tenant_id: str
    grant_ref: str
    principal: str
    version: int
    capabilities: JSONList
    data_egress: JSONList
    resource_scope: JSON
    expires_at: Optional[datetime]
    revoked: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TaskSpecRecord:
    tenant_id: str
    task_id: str
    spec_version: int
    root_task_id: str
    spec_digest: str
    body: JSON
    status: SpecStatus
    created_at: datetime
    created_by: str
    confirmed_at: Optional[datetime]
    confirmed_by: Optional[str]


@dataclass(frozen=True, slots=True)
class TaskRuntime:
    tenant_id: str
    task_id: str
    root_task_id: str
    parent_task_id: Optional[str]
    depth: int
    lifecycle: TaskLifecycle
    active_spec_version: int
    version: int
    revocation_epoch: int
    source_ref: str
    grant_ref: str
    grant_version: int
    effective_authority: JSON
    expires_at: datetime
    lifecycle_reason: Optional[str]
    last_progress_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SourceBinding:
    tenant_id: str
    source_ref: str
    owner: str
    connector_id: str
    source_uri: str
    resource_scope: JSON
    secret_ref: Optional[str]
    ingress_secret_ref: Optional[str]
    capabilities: JSONList
    health: SourceHealth
    health_reason: Optional[str]
    last_trusted_at: Optional[datetime]
    last_attempt_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SourceCheckpoint:
    """Trusted snapshot + cursor for one task scope over one source (RFC §7, §11.4)."""

    tenant_id: str
    source_ref: str
    scope_key: str
    task_id: str
    local_revision: int
    trusted_revision: Optional[str]
    trusted_snapshot: Optional[JSON]
    trusted_observation_id: Optional[str]
    trusted_at: Optional[datetime]
    cursor: Optional[str]
    version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TriggerRecord:
    tenant_id: str
    trigger_id: str
    task_id: str
    root_task_id: str
    kind: TriggerKind
    every_seconds: Optional[int]
    timezone: str
    catchup_policy: CatchupPolicy
    due_at: Optional[datetime]
    expiry: Optional[datetime]
    generation: int
    status: TriggerStatus
    predicate_ref: Optional[str]
    depth: int
    origin_run_id: Optional[str]
    followup_key: Optional[str]
    reason: Optional[str]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TriggerOccurrence:
    tenant_id: str
    trigger_id: str
    generation: int
    scheduled_for: datetime
    task_id: str
    occurred_at: datetime
    coalesced_count: int
    run_id: str


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    tenant_id: str
    observation_id: str
    source_ref: str
    task_id: str
    run_id: str
    scope: JSON
    outcome: ObservationOutcome
    completeness: Completeness
    source_revision: Optional[str]
    observed_at: datetime
    payload_digest: Optional[str]
    evidence_ref: Optional[str]
    coverage_gaps: JSONList
    history_coverage: Optional[JSON]
    error_code: Optional[str]
    connector_version: str


@dataclass(frozen=True, slots=True)
class Evidence:
    tenant_id: str
    evidence_ref: str
    source_ref: str
    kind: str
    digest: str
    content: JSON
    visibility: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EventRecord:
    """CloudEvents-shaped event plus kernel-registered envelope metadata (RFC §4.3)."""

    tenant_id: str
    event_id: str
    seq: int
    source_ref: str
    source_uri: str
    source_event_id: str
    specversion: str
    type: str
    subject: Optional[str]
    occurred_at: Optional[datetime]
    received_at: datetime
    payload_digest: str
    data: JSON
    verified: bool
    internal: bool
    ingress_principal: str
    causation_id: Optional[str]
    evidence_ref: Optional[str]


@dataclass(frozen=True, slots=True)
class Delivery:
    tenant_id: str
    event_id: str
    task_id: str
    status: DeliveryStatus
    run_id: Optional[str]
    decision_ref: Optional[str]
    reason: Optional[str]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    tenant_id: str
    decision_id: str
    task_id: str
    run_id: str
    event_id: Optional[str]
    route: Route
    reason_code: str
    evidence_refs: JSONList
    uncertainty_flags: JSONList
    suggested_capabilities: JSONList
    model_call_ref: Optional[str]
    policy_version: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Run:
    tenant_id: str
    run_id: str
    task_id: str
    root_task_id: str
    reason: str
    status: RunStatus
    wait_reason: Optional[WaitReason]
    checkpoint: JSON
    spec_version: int
    revocation_epoch: int
    step_count: int
    created_at: datetime
    updated_at: datetime
    finished_at: Optional[datetime]


@dataclass(frozen=True, slots=True)
class Step:
    tenant_id: str
    step_id: str
    run_id: str
    task_id: str
    logical_step_id: str
    kind: StepKind
    status: StepStatus
    input: JSON
    result: Optional[JSON]
    available_at: datetime
    priority: int
    attempts: int
    max_attempts: int
    lease_owner: Optional[str]
    lease_epoch: int
    lease_until: Optional[datetime]
    current_attempt_id: Optional[str]
    last_error: Optional[str]
    wait_reason: Optional[WaitReason]
    retry_owner: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WaitRecord:
    tenant_id: str
    wait_id: str
    run_id: str
    step_id: str
    task_id: str
    reason: WaitReason
    match: JSON
    basis_watermark: int
    due_at: Optional[datetime]
    status: WaitStatus
    matched_event_id: Optional[str]
    created_at: datetime
    resolved_at: Optional[datetime]


@dataclass(frozen=True, slots=True)
class ExecutionSlot:
    """At most one live compute lease per task aggregate (RFC §12.2)."""

    tenant_id: str
    task_id: str
    holder_step_id: str
    lease_owner: str
    lease_epoch: int
    lease_until: datetime


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """Immutable action intent. Changing content means a new action (RFC §4, §9.2)."""

    tenant_id: str
    action_id: str
    task_id: str
    root_task_id: str
    run_id: str
    effect_key: str
    causal_event_id: Optional[str]
    logical_action: str
    tool_id: str
    tool_version: str
    capability: str
    canonical_payload: JSON
    payload_digest: str
    preconditions: JSON
    resource_scope: JSON      # V0.3: authorisation domain, ⊆ task authority, bound by payload_digest
    data_egress: JSONList     # V0.3: sorted egress targets, bound by payload_digest
    status: ActionStatus
    requires_approval: bool
    revocation_epoch: int
    reason_code: Optional[str]
    resolution: Optional[str]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ActionAttempt:
    tenant_id: str
    attempt_id: str
    action_id: str
    status: ActionStatus
    lease_owner: str
    lease_epoch: int
    lease_until: datetime
    started_at: datetime
    finished_at: Optional[datetime]
    provider_ref: Optional[str]
    provider_request_id: Optional[str]
    receipt: Optional[JSON]
    late_receipt: Optional[JSON]
    error: Optional[str]


@dataclass(frozen=True, slots=True)
class Approval:
    tenant_id: str
    approval_id: str
    action_id: str
    task_id: str
    approval_revision: int
    payload_digest: str
    grant_ref: str
    grant_version: int
    expires_at: datetime
    decision: ApprovalDecision
    actor: Optional[str]
    decided_at: Optional[datetime]
    reason: Optional[str]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    tenant_id: str
    outbox_id: str
    kind: str
    ref_id: str
    dedupe_key: str
    status: OutboxStatus
    available_at: datetime
    attempts: int
    lease_owner: Optional[str]
    lease_epoch: int
    lease_until: Optional[datetime]
    last_error: Optional[str]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class BudgetAccount:
    tenant_id: str
    account_key: str
    scope: str
    subject_id: str
    metric: str
    period_key: str
    unit: str
    limit_amount: int
    reserved: int
    settled: int
    version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    tenant_id: str
    reservation_id: str
    account_key: str
    root_task_id: str
    attempt_id: str
    reserved: int
    actual: Optional[int]
    status: ReservationStatus
    period_key: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ModelCall:
    tenant_id: str
    call_id: str
    run_id: str
    step_id: str
    logical_step_id: str
    task_id: str
    kind: str
    model_ref: str
    prompt_version: str
    input_digest: str
    status: ModelCallStatus
    output: Optional[JSON]
    usage: Optional[JSON]
    reservation_id: Optional[str]
    provider_request_id: Optional[str]
    error: Optional[str]
    created_at: datetime
    finished_at: Optional[datetime]


@dataclass(frozen=True, slots=True)
class AuditEntry:
    tenant_id: str
    audit_id: str
    seq: int
    at: datetime
    actor: str
    kind: str
    task_id: Optional[str]
    root_task_id: Optional[str]
    subject_type: str
    subject_id: str
    from_state: Optional[str]
    to_state: Optional[str]
    reason: Optional[str]
    refs: JSON


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    tenant_id: str
    principal: str
    idem_key: str
    request_digest: str
    status_code: int
    response: JSON
    created_at: datetime


@dataclass(frozen=True, slots=True)
class InboxMessage:
    tenant_id: str
    message_id: str
    effect_key: str
    task_id: str
    title: str
    body: str
    data: JSON
    created_at: datetime


@dataclass(frozen=True, slots=True)
class QuarantineRecord:
    tenant_id: str
    quarantine_id: str
    kind: str
    ref: str
    reason: str
    payload: JSON
    created_at: datetime
