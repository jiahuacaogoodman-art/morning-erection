"""Append-only audit trail (RFC §11.3: every transaction group writes audit facts)."""
from typing import Any, Optional

from ..domain.model import AuditEntry
from ..ports.clock import IdGenerator
from ..repo import Repo


def record(
    repo: Repo,
    ids: IdGenerator,
    *,
    tenant_id: str,
    kind: str,
    actor: str,
    subject_type: str,
    subject_id: str,
    task_id: Optional[str] = None,
    root_task_id: Optional[str] = None,
    from_state: Optional[str] = None,
    to_state: Optional[str] = None,
    reason: Optional[str] = None,
    refs: Optional[dict[str, Any]] = None,
) -> AuditEntry:
    entry = AuditEntry(
        tenant_id=tenant_id,
        audit_id=ids.new("aud"),
        seq=repo.uow.next_seq("audit"),
        at=repo.uow.db_now(),
        actor=actor,
        kind=kind,
        task_id=task_id,
        root_task_id=root_task_id,
        subject_type=subject_type,
        subject_id=subject_id,
        from_state=None if from_state is None else str(from_state),
        to_state=None if to_state is None else str(to_state),
        reason=reason,
        refs=refs or {},
    )
    repo.insert(entry)
    return entry
