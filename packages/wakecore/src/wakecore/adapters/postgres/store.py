"""PostgreSQL production adapter (RFC §1.3, §12).

- READ COMMITTED transactions; `for_update` maps to FOR UPDATE, `skip_locked` to SKIP LOCKED.
- `insert` runs inside a SAVEPOINT so a unique violation does not abort the transaction.
- `db_now()` is `statement_timestamp()` unless a test clock is injected.
- The database role used here must not be a superuser nor hold BYPASSRLS (RFC §9.4).

Requires `psycopg` (v3). Not exercised by the default offline test-suite; see
tests/integration_postgres (set WAKECORE_PG_DSN).
"""
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Mapping, Optional, Sequence

from ...kernel.ports.clock import Clock
from ...kernel.ports.store import Cond, DuplicateKey
from ...kernel.ports.tables import TABLES
from ..sqlcore import SqlBuilder

_TYPES = {"text": "TEXT", "int": "BIGINT", "bool": "BOOLEAN", "ts": "TIMESTAMPTZ", "json": "JSONB"}


def _load_psycopg():
    try:
        import psycopg
        from psycopg.types.json import Jsonb
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("install wakecore-kernel[postgres] to use the PostgreSQL adapter") from exc
    return psycopg, Jsonb


class PostgresUnitOfWork:
    def __init__(self, conn: Any, sql: SqlBuilder, clock: Optional[Clock], psycopg: Any) -> None:
        self._c = conn
        self._sql = sql
        self._clock = clock
        self._pg = psycopg
        self._now: Optional[datetime] = None

    def _rows(self, table: str, cur: Any) -> list[dict[str, Any]]:
        names = [d.name for d in cur.description]
        kinds = {c.name: c.kind for c in TABLES[table].columns}
        out = []
        for r in cur.fetchall():
            row = {}
            for n, v in zip(names, r):
                row[n] = int(v) if kinds[n] == "int" and v is not None else v
            out.append(row)
        return out

    def insert(self, table: str, row: Mapping[str, Any]) -> None:
        sql, params = self._sql.insert(table, row, ignore_conflict=False)
        try:
            with self._c.transaction():  # savepoint
                self._c.execute(sql, params)
        except self._pg.errors.UniqueViolation as exc:
            raise DuplicateKey(table, getattr(exc.diag, "constraint_name", "") or "") from exc

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
        if self._now is None:
            if self._clock is not None:
                self._now = self._clock.utc_now()
            else:
                self._now = self._c.execute("SELECT statement_timestamp()").fetchone()[0]
        return self._now


class PostgresStore:
    def __init__(self, dsn: str, *, clock: Optional[Clock] = None) -> None:
        self._pg, jsonb = _load_psycopg()
        self.dsn = dsn
        self.clock = clock
        self._local = threading.local()

        def encode(kind: str, value: Any) -> Any:
            if value is None:
                return None
            if kind == "json":
                return jsonb(value)
            if kind == "text":
                return str(value)
            return value

        self._sql = SqlBuilder("%s", _TYPES, row_locks=True, encode=encode)
        self._conns: list[Any] = []
        self._lock = threading.Lock()

    def _conn(self) -> Any:
        conn = getattr(self._local, "conn", None)
        if conn is None or conn.closed:
            conn = self._pg.connect(self.dsn, autocommit=True)  # explicit transaction() blocks only
            self._local.conn = conn
            with self._lock:
                self._conns.append(conn)
        return conn

    def ddl(self) -> list[str]:
        return self._sql.ddl()

    def migrate(self) -> None:
        conn = self._conn()
        with conn.transaction():
            for stmt in self._sql.ddl():
                conn.execute(stmt)

    @contextmanager
    def transaction(self) -> Iterator[PostgresUnitOfWork]:
        conn = self._conn()
        with conn.transaction():
            yield PostgresUnitOfWork(conn, self._sql, self.clock, self._pg)

    def close(self) -> None:
        with self._lock:
            for c in self._conns:
                try:
                    c.close()
                except Exception:
                    pass
            self._conns.clear()
