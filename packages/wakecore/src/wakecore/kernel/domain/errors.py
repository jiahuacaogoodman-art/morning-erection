"""Kernel error taxonomy. Codes follow RFC §15."""
from typing import Any, Optional


class KernelError(Exception):
    code = "KERNEL_ERROR"
    retryable = False
    http_status = 400

    def __init__(
        self,
        message: str = "",
        *,
        code: Optional[str] = None,
        retryable: Optional[bool] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message or self.code)
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        # Never include tokens or full sensitive bodies here (RFC §15).
        return {"code": self.code, "message": str(self), "retryable": self.retryable, "details": self.details}


class NotFound(KernelError):
    code = "NOT_FOUND"
    http_status = 404


class TaskNotActive(KernelError):
    code = "TASK_NOT_ACTIVE"
    http_status = 409


class TaskExpired(KernelError):
    code = "TASK_EXPIRED"
    http_status = 409


class SourceAuthRequired(KernelError):
    code = "SOURCE_AUTH_REQUIRED"
    http_status = 409


class SourceCoverageInsufficient(KernelError):
    code = "SOURCE_COVERAGE_INSUFFICIENT"
    http_status = 409


class SchemaMismatch(KernelError):
    code = "SCHEMA_MISMATCH"
    http_status = 422


class PolicyDenied(KernelError):
    code = "POLICY_DENIED"
    http_status = 403


class ApprovalRequired(KernelError):
    code = "APPROVAL_REQUIRED"
    http_status = 409


class ApprovalStale(KernelError):
    code = "APPROVAL_STALE"
    http_status = 409


class BudgetDeferred(KernelError):
    code = "BUDGET_DEFERRED"
    retryable = True
    http_status = 409


class StaleLease(KernelError):
    code = "STALE_LEASE"
    http_status = 409


class SideEffectUnknown(KernelError):
    code = "SIDE_EFFECT_UNKNOWN"
    http_status = 409


class VersionConflict(KernelError):
    code = "VERSION_CONFLICT"
    retryable = True
    http_status = 409


class InvalidTransition(KernelError):
    code = "INVALID_TRANSITION"
    http_status = 409


class IdempotencyConflict(KernelError):
    code = "IDEMPOTENCY_CONFLICT"
    http_status = 409


class IdentityConflict(KernelError):
    """Same identity key, different payload digest: quarantined, never overwritten (RFC §11.2)."""

    code = "IDENTITY_CONFLICT"
    http_status = 409


class Unauthenticated(KernelError):
    code = "UNAUTHENTICATED"
    http_status = 401
