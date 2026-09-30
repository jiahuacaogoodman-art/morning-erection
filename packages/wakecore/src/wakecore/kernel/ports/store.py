"""Storage port (RFC §13.3 StateStore / UnitOfWork).

The kernel speaks to storage through logical tables (see `tables.py`) and a small set
of dialect-neutral operations. Adapters provide the physical schema, locking and
transaction semantics. All writes of one kernel transaction group (T1–T7, RFC §11.3)
happen inside a single `transaction()` block; network and model calls never do.
"""
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, Mapping, Optional, Protocol, Sequence

# (column, op, value); op in: = != < <= > >= in not_in is_null not_null
Cond = tuple[str, str, Any]


class DuplicateKey(Exception):
    def __init__(self, table: str, constraint: str = "") -> None:
        super().__init__(f"duplicate key in {table} {constraint}".strip())
        self.table = table
        self.constraint = constraint


class UnitOfWork(Protocol):
    def insert(self, table: str, row: Mapping[str, Any]) -> None:
        """Insert; raise DuplicateKey on unique/PK conflict (transaction stays usable)."""

    def insert_if_absent(self, table: str, row: Mapping[str, Any]) -> bool:
        """Insert unless a unique/PK conflict exists. Returns True if inserted."""

    def get(self, table: str, key: Mapping[str, Any], *, for_update: bool = False) -> Optional[dict[str, Any]]:
        ...

    def find(
        self,
        table: str,
        where: Optional[Mapping[str, Any]] = None,
        *,
        conds: Sequence[Cond] = (),
        order_by: Sequence[str] = (),
        limit: Optional[int] = None,
        for_update: bool = False,
        skip_locked: bool = False,
    ) -> list[dict[str, Any]]:
        """`order_by` entries are column names, prefix '-' for descending."""

    def count(self, table: str, where: Optional[Mapping[str, Any]] = None, *, conds: Sequence[Cond] = ()) -> int:
        ...

    def update(
        self,
        table: str,
        key: Mapping[str, Any],
        changes: Mapping[str, Any],
        *,
        expect: Optional[Mapping[str, Any]] = None,
    ) -> int:
        """Compare-and-set update. `expect` adds equality conditions. Returns rowcount."""

    def delete(self, table: str, key: Mapping[str, Any], *, expect: Optional[Mapping[str, Any]] = None) -> int:
        ...

    def next_seq(self, name: str) -> int:
        ...

    def db_now(self) -> datetime:
        """Authoritative time for due/lease decisions (database time in production)."""


class StateStore(Protocol):
    def transaction(self) -> AbstractContextManager[UnitOfWork]:
        ...

    def close(self) -> None:
        ...
