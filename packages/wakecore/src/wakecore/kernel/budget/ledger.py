"""Budget reservations (RFC §10, T15, T16).

available = limit - settled - reserved. Reservations are created in a short
transaction that locks accounts in a fixed order (user before root task), so two
concurrent callers cannot both spend the last unit. A timed-out model request keeps its
reservation as AWAITING_RECONCILIATION instead of silently releasing it to zero.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from ..domain.enums import ReservationStatus
from ..domain.errors import BudgetDeferred, NotFound
from ..domain.model import BudgetAccount, BudgetReservation
from ..ports.clock import IdGenerator
from ..repo import Repo

SCOPE_ORDER = {"user": 0, "root_task": 1}


@dataclass(frozen=True)
class AccountSpec:
    scope: str
    subject_id: str
    metric: str
    unit: str
    limit_amount: int


def period_key(now: datetime, tz: str) -> str:
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = ZoneInfo("UTC")
    return now.astimezone(zone).strftime("%Y-%m-%d")


def account_key(spec: AccountSpec, period: str) -> str:
    return f"{spec.scope}:{spec.subject_id}:{spec.metric}:{period}"


def _ensure(repo: Repo, tenant_id: str, spec: AccountSpec, period: str, now: datetime) -> str:
    key = account_key(spec, period)
    repo.insert_if_absent(BudgetAccount(
        tenant_id=tenant_id, account_key=key, scope=spec.scope, subject_id=spec.subject_id, metric=spec.metric,
        period_key=period, unit=spec.unit, limit_amount=spec.limit_amount, reserved=0, settled=0, version=0,
        updated_at=now))
    return key


def reserve(repo: Repo, ids: IdGenerator, *, tenant_id: str, accounts: list[AccountSpec], amount: int,
            root_task_id: str, attempt_id: str, period: str) -> str:
    now = repo.uow.db_now()
    ordered = sorted(accounts, key=lambda a: (SCOPE_ORDER.get(a.scope, 99), a.subject_id))
    keys = [_ensure(repo, tenant_id, a, period, now) for a in ordered]
    locked = []
    for key in keys:  # fixed lock order
        acct = repo.get(BudgetAccount, for_update=True, tenant_id=tenant_id, account_key=key)
        assert acct is not None
        available = acct.limit_amount - acct.settled - acct.reserved
        if available < amount:
            raise BudgetDeferred(f"budget exhausted for {acct.scope}", details={
                "account": key, "available": available, "requested": amount})
        locked.append(acct)
    reservation_id = ids.new("rsv")
    for acct in locked:
        repo.change(acct, expect={"version": acct.version}, reserved=acct.reserved + amount,
                    version=acct.version + 1, updated_at=now)
        repo.insert(BudgetReservation(
            tenant_id=tenant_id, reservation_id=reservation_id, account_key=acct.account_key,
            root_task_id=root_task_id, attempt_id=attempt_id, reserved=amount, actual=None,
            status=ReservationStatus.RESERVED, period_key=period, created_at=now, updated_at=now))
    return reservation_id


def _rows(repo: Repo, tenant_id: str, reservation_id: str) -> list[BudgetReservation]:
    rows = repo.find(BudgetReservation, {"tenant_id": tenant_id, "reservation_id": reservation_id},
                     order_by=("account_key",))
    if not rows:
        raise NotFound(f"reservation {reservation_id}")
    return rows


def _close(repo: Repo, tenant_id: str, reservation_id: str, actual: Optional[int],
           status: ReservationStatus) -> None:
    now = repo.uow.db_now()
    rows = sorted(_rows(repo, tenant_id, reservation_id),
                  key=lambda r: (SCOPE_ORDER.get(r.account_key.split(":")[0], 99), r.account_key))
    for r in rows:
        if r.status in (ReservationStatus.SETTLED, ReservationStatus.RELEASED):
            continue
        acct = repo.get(BudgetAccount, for_update=True, tenant_id=tenant_id, account_key=r.account_key)
        assert acct is not None
        settled = acct.settled + (actual or 0)
        repo.change(acct, expect={"version": acct.version}, reserved=acct.reserved - r.reserved, settled=settled,
                    version=acct.version + 1, updated_at=now)
        repo.change(r, status=status, actual=actual, updated_at=now)


def settle(repo: Repo, *, tenant_id: str, reservation_id: str, actual: int) -> None:
    """Record the actual cost against the reservation's original period."""
    _close(repo, tenant_id, reservation_id, actual, ReservationStatus.SETTLED)


def release(repo: Repo, *, tenant_id: str, reservation_id: str) -> None:
    """Only when it is known that nothing was consumed (e.g. the call was never sent)."""
    _close(repo, tenant_id, reservation_id, 0, ReservationStatus.RELEASED)


def mark_awaiting_reconciliation(repo: Repo, *, tenant_id: str, reservation_id: str) -> None:
    now = repo.uow.db_now()
    for r in _rows(repo, tenant_id, reservation_id):
        if r.status is ReservationStatus.RESERVED:
            repo.change(r, status=ReservationStatus.AWAITING_RECONCILIATION, updated_at=now)


def available(repo: Repo, *, tenant_id: str, key: str) -> Optional[int]:
    acct = repo.get(BudgetAccount, tenant_id=tenant_id, account_key=key)
    return None if acct is None else acct.limit_amount - acct.settled - acct.reserved
