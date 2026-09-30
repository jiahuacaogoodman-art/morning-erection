"""Closed vocabularies used by the kernel (RFC §5, §8, §9, §10)."""
from enum import StrEnum


class TaskLifecycle(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    DRAINING = "DRAINING"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


TERMINAL_LIFECYCLES = frozenset(
    {TaskLifecycle.COMPLETED, TaskLifecycle.EXPIRED, TaskLifecycle.CANCELLED}
)


class SpecStatus(StrEnum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    SUPERSEDED = "SUPERSEDED"


class SourceHealth(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    UNAVAILABLE = "UNAVAILABLE"
    SCHEMA_INVALID = "SCHEMA_INVALID"


class ObservationOutcome(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    AUTH_REQUIRED = "auth_required"
    UNAVAILABLE = "unavailable"
    SCHEMA_INVALID = "schema_invalid"


class Completeness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class ObservationMode(StrEnum):
    SNAPSHOT = "snapshot"
    HISTORY = "history"
    PUSH_HINT = "push_hint"


class TriggerKind(StrEnum):
    INTERVAL = "interval"
    ONCE = "once"


class TriggerStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CONSUMED = "CONSUMED"
    REVOKED = "REVOKED"


class CatchupPolicy(StrEnum):
    COALESCE_LATEST = "coalesce_latest"
    SKIP_MISSED = "skip_missed"


class DeliveryStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSED = "PROCESSED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StepStatus(StrEnum):
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StepKind(StrEnum):
    PROBE = "probe"
    PROCESS_DELIVERY = "process_delivery"
    JUDGE = "judge"
    PLAN = "plan"
    WAIT_RESUME = "wait_resume"


class WaitReason(StrEnum):
    TIMER = "timer"
    SOURCE = "source"
    MODEL = "model"
    APPROVAL = "approval"
    BUDGET = "budget"
    RETRY = "retry"
    RECONCILIATION = "reconciliation"


class WaitStatus(StrEnum):
    WAITING = "WAITING"
    MATCHED = "MATCHED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class ActionStatus(StrEnum):
    PROPOSED = "PROPOSED"
    DENIED = "DENIED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    READY = "READY"
    DISPATCHING = "DISPATCHING"
    CONFIRMED = "CONFIRMED"
    FAILED_NO_EFFECT = "FAILED_NO_EFFECT"
    UNKNOWN = "UNKNOWN"
    RESOLVED_MANUALLY = "RESOLVED_MANUALLY"
    CANCELLED = "CANCELLED"


ACTION_IN_FLIGHT = frozenset({ActionStatus.DISPATCHING, ActionStatus.UNKNOWN})
ACTION_NOT_YET_DISPATCHED = frozenset(
    {ActionStatus.PROPOSED, ActionStatus.WAITING_APPROVAL, ActionStatus.READY}
)


class ApprovalDecision(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class Route(StrEnum):
    SOURCE_BLOCKED = "SOURCE_BLOCKED"
    REVIEW = "REVIEW"
    NOOP = "NOOP"
    TEMPLATE_ACTION = "TEMPLATE_ACTION"
    JUDGE = "JUDGE"
    PLANNER = "PLANNER"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class AuthzOutcome(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class SideEffectClass(StrEnum):
    READ = "read"
    EXTERNAL_WRITE = "external_write"
    LOCAL_WRITE = "local_write"


class ReservationStatus(StrEnum):
    RESERVED = "RESERVED"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"
    AWAITING_RECONCILIATION = "AWAITING_RECONCILIATION"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    DONE = "DONE"
    DEAD = "DEAD"


class ModelCallStatus(StrEnum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
