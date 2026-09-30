"""SQLite development adapter (RFC §1.3).

Every transaction is `BEGIN IMMEDIATE`, so writers are fully serialised; row locks and
SKIP LOCKED are therefore no-ops. Passing tests on this adapter does NOT substitute for
PostgreSQL concurrency tests (RFC §1.3, M2).
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping, Optional, Sequence

from ...kernel.ports.clock import Clock
from ...kernel.ports.store import Cond, DuplicateKey
from ...kernel.ports.tables import TABLES
from ..sqlcore import SqlBuilder

_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"
_TYPES = {"text": "TEXT", "int": "INTEGER", "bool": "INTEGER", "ts": "TEXT", "json": "TEXT"}


def _encode(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind == "ts":
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise TypeError("timestamps must be timezone-aware datetimes")
        return value.astimezone(timezone.utc).strftime(_TS_FMT)
    if kind == "json":
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if kind == "bool":
        return 1 if value else 0
    if kind == "text":
        return str(value)
    return value


def _decode(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind == "ts":
        return datetime.strptime(value, _TS_FMT).replace(tzinfo=timezone.utc)
    if kind == "json":
        return json.loads(value)
    if kind == "bool":
        return bool(value)
    return value


class SqliteUnitOfWork:
    def __init__(self, conn: sqlite3.Connection, sql: SqlBuilder, clock: Clock) -> None:
        self._c = conn
        self._sql = sql
        self._clock = clock
        self._now: Optional[datetime] = None

    def _rows(self, table: str, cur: sqlite3.Cursor) -> list[dict[str, Any]]:
        kinds = {c.name: c.kind for c in TABLES[table].columns}
        names = [d[0] for d in cur.description]
        return [{n: _decode(kinds[n], v) for n, v in zip(names, r)} for r in cur.fetchall()]

    def insert(self, table: str, row: Mapping[str, Any]) -> None:
        sql, params = self._sql.insert(table, row, ignore_conflict=False)
        try:
            self._c.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            msg = str(exc)
            if "UNIQUE" in msg or "PRIMARY KEY" in msg:
                raise DuplicateKey(table, msg) from exc
            raise

    def insert_if_absent(self, table: str, row: Mapping[str, Any]) -> bool:
        sql, params = self._sql.insert(table, row, ignore_conflict=True)
        return self._c.execute(sql, params).rowcount == 1

    def get(self, table: str, key: Mapping[str, Any], *, for_update: bool = False) -> Optional[dict[str, Any]]:
        rows = self.find(table, key, limit=2, for_update=for_update)
        return rows[0] if rows else None

    def find(self, table: str, where: Optional[Mapping[str, Any]] = None, *, conds: Sequence[Cond] = (),
             order_by: Sequence[str] = (), limit: Optional[int] = None, for_update: bool = False,
             skip_locked: bool = False) -> list[dict[str, Any]]:
        sql, params = self._sql.select(table, where, conds, order_by, limit, for_update, skip_locked)
        return self._rows(table, self._c.execute(sql, params))

    def count(self, table: str, where: Optional[Mapping[str, Any]] = None, *, conds: Sequence[Cond] = ()) -> int:
        sql, params = self._sql.count(table, where, conds)
        return int(self._c.execute(sql, params).fetchone()[0])

    def update(self, table: str, key: Mapping[str, Any], changes: Mapping[str, Any], *,
               expect: Optional[Mapping[str, Any]] = None) -> int:
        sql, params = self._sql.update(table, key, changes, expect)
        return self._c.execute(sql, params).rowcount

    def delete(self, table: str, key: Mapping[str, Any], *, expect: Optional[Mapping[str, Any]] = None) -> int:
        sql, params = self._sql.delete(table, key, expect)
        return self._c.execute(sql, params).rowcount

    def next_seq(self, name: str) -> int:
        return int(self._c.execute(self._sql.next_seq(), [name]).fetchone()[0])

    def db_now(self) -> datetime:
        # Stable within one transaction, like PostgreSQL's transaction timestamp.
        if self._now is None:
            self._now = self._clock.utc_now()
        return self._now


class SqliteStore:
    def __init__(self, path: str, clock: Clock, *, busy_timeout_ms: int = 30000) -> None:
        self.path = path
        self.clock = clock
        self._busy = busy_timeout_ms
        self._local = threading.local()
        self._sql = SqlBuilder("?", _TYPES, row_locks=False, encode=_encode)
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False,
                                   timeout=self._busy / 1000)
            conn.execute(f"PRAGMA busy_timeout = {self._busy}")
            conn.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
            self._local.conn = conn
            with self._lock:
                self._all.append(conn)
        return conn

    def migrate(self) -> None:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in self._sql.ddl():
                conn.execute(stmt)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def ddl(self) -> list[str]:
        return self._sql.ddl()

    @contextmanager
    def transaction(self) -> Iterator[SqliteUnitOfWork]:
        conn = self._conn()
        if getattr(self._local, "in_tx", False):
            raise RuntimeError("nested transactions are not supported")
        self._local.in_tx = True
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield SqliteUnitOfWork(conn, self._sql, self.clock)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
        finally:
            self._local.in_tx = False

    def close(self) -> None:
        with self._lock:
            for conn in self._all:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._all.clear()
        self._local = threading.local()
