"""Explicit transition tables (RFC §5). Every status write goes through `ensure`."""
from .enums import (
    ActionStatus as A,
)
from .enums import (
    ApprovalDecision as AP,
)
from .enums import (
    RunStatus as R,
)
from .enums import (
    StepStatus as S,
)
from .enums import (
    TaskLifecycle as T,
)
from .errors import InvalidTransition

TASK = {
    T.DRAFT: {T.ACTIVE, T.CANCELLED},
    T.ACTIVE: {T.PAUSED, T.DRAINING, T.EXPIRED, T.CANCELLED},
    T.PAUSED: {T.ACTIVE, T.EXPIRED, T.CANCELLED},
    T.DRAINING: {T.COMPLETED, T.EXPIRED, T.CANCELLED},
    T.COMPLETED: set(),
    T.EXPIRED: set(),
    T.CANCELLED: set(),
}

# RUNNING -> QUEUED is the recovery path after an expired lease on a re-runnable step.
RUN = {
    R.QUEUED: {R.RUNNING, R.CANCELLED},
    R.RUNNING: {R.SUCCEEDED, R.WAITING, R.FAILED, R.CANCELLED, R.QUEUED},
    R.WAITING: {R.QUEUED, R.CANCELLED, R.FAILED},
    R.SUCCEEDED: set(),
    R.FAILED: set(),
    R.CANCELLED: set(),
}

STEP = {
    S.READY: {S.RUNNING, S.CANCELLED},
    S.RUNNING: {S.SUCCEEDED, S.FAILED, S.WAITING, S.READY, S.CANCELLED},
    S.WAITING: {S.READY, S.CANCELLED, S.FAILED},
    S.SUCCEEDED: set(),
    S.FAILED: set(),
    S.CANCELLED: set(),
}

ACTION = {
    A.PROPOSED: {A.DENIED, A.WAITING_APPROVAL, A.READY},
    A.WAITING_APPROVAL: {A.READY, A.CANCELLED, A.DENIED},
    A.READY: {A.DISPATCHING, A.CANCELLED, A.DENIED},
    A.DISPATCHING: {A.CONFIRMED, A.FAILED_NO_EFFECT, A.UNKNOWN},
    # FAILED_NO_EFFECT only with evidence that there was no effect; enforced by callers.
    A.UNKNOWN: {A.CONFIRMED, A.FAILED_NO_EFFECT, A.RESOLVED_MANUALLY},
    A.CONFIRMED: set(),
    A.FAILED_NO_EFFECT: set(),
    A.RESOLVED_MANUALLY: set(),
    A.DENIED: set(),
    A.CANCELLED: set(),
}

APPROVAL = {
    AP.PENDING: {AP.APPROVED, AP.REJECTED, AP.EXPIRED, AP.REVOKED},
    AP.APPROVED: {AP.REVOKED},
    AP.REJECTED: set(),
    AP.EXPIRED: set(),
    AP.REVOKED: set(),
}

_TABLES = {"task": TASK, "run": RUN, "step": STEP, "action": ACTION, "approval": APPROVAL}


def can(machine: str, src: str, dst: str) -> bool:
    table = _TABLES[machine]
    return dst in table.get(src, set())


def ensure(machine: str, src: str, dst: str) -> None:
    if not can(machine, src, dst):
        raise InvalidTransition(
            f"{machine}: {src} -> {dst} is not allowed",
            details={"machine": machine, "from": str(src), "to": str(dst)},
        )
