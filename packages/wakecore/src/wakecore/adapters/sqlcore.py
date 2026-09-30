"""Dialect-neutral SQL construction shared by the SQLite and PostgreSQL adapters.

DDL is generated from `kernel.ports.tables.TABLES`, so constraints are identical in
both backends. Identifiers are always quoted.
"""
from typing import Any, Callable, Mapping, Optional, Sequence

from ..kernel.ports.store import Cond
from ..kernel.ports.tables import TABLES, TableDef

COUNTERS_TABLE = "wk_counters"
_OPS = {"=", "!=", "<", "<=", ">", ">="}


def q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def unique_name(table: str, cols: Sequence[str]) -> str:
    return f"uq_{table}_{'_'.join(c for c in cols if c != 'tenant_id')}"[:63]


class SqlBuilder:
    def __init__(self, placeholder: str, types: Mapping[str, str], *, row_locks: bool,
                 encode: Callable[[str, Any], Any]) -> None:
        self.ph = placeholder
        self.types = types
        self.row_locks = row_locks
        self.encode = encode

    # ---- DDL -------------------------------------------------------------
    def ddl(self) -> list[str]:
        stmts = []
        for t in TABLES.values():
            stmts.extend(self._table_ddl(t))
        stmts.append(
            f"CREATE TABLE IF NOT EXISTS {q(COUNTERS_TABLE)} ("
            f"{q('name')} {self.types['text']} PRIMARY KEY, {q('value')} {self.types['int']} NOT NULL)"
        )
        return stmts

    def _table_ddl(self, t: TableDef) -> list[str]:
        parts = [f"{q(c.name)} {self.types[c.kind]}{'' if c.nullable else ' NOT NULL'}" for c in t.columns]
        parts.append(f"CONSTRAINT {q('pk_' + t.name)} PRIMARY KEY ({', '.join(q(c) for c in t.pk)})")
        for cols in t.unique:
            parts.append(f"CONSTRAINT {q(unique_name(t.name, cols))} UNIQUE ({', '.join(q(c) for c in cols)})")
        for fk in t.foreign_keys:
            parts.append(
                f"FOREIGN KEY ({', '.join(q(c) for c in fk.columns)}) REFERENCES {q(fk.ref_table)} "
                f"({', '.join(q(c) for c in fk.ref_columns)})"
            )
        out = [f"CREATE TABLE IF NOT EXISTS {q(t.name)} (\n  " + ",\n  ".join(parts) + "\n)"]
        for i, cols in enumerate(t.indexes):
            name = f"ix_{t.name}_{i}"
            out.append(f"CREATE INDEX IF NOT EXISTS {q(name)} ON {q(t.name)} ({', '.join(q(c) for c in cols)})")
        return out

    # ---- DML -------------------------------------------------------------
    def _kinds(self, table: str) -> dict[str, str]:
        return {c.name: c.kind for c in TABLES[table].columns}

    def _enc(self, kinds: dict[str, str], col: str, value: Any) -> Any:
        if col not in kinds:
            raise KeyError(f"unknown column {col}")
        return self.encode(kinds[col], value)

    def insert(self, table: str, row: Mapping[str, Any], *, ignore_conflict: bool) -> tuple[str, list[Any]]:
        kinds = self._kinds(table)
        missing = set(kinds) - set(row)
        if missing:
            raise KeyError(f"{table}: missing columns {sorted(missing)}")
        cols = list(kinds)
        sql = (
            f"INSERT INTO {q(table)} ({', '.join(q(c) for c in cols)}) "
            f"VALUES ({', '.join([self.ph] * len(cols))})"
        )
        if ignore_conflict:
            sql += " ON CONFLICT DO NOTHING"
        return sql, [self._enc(kinds, c, row[c]) for c in cols]

    def where(self, table: str, where: Optional[Mapping[str, Any]], conds: Sequence[Cond]) -> tuple[str, list[Any]]:
        kinds = self._kinds(table)
        clauses: list[str] = []
        params: list[Any] = []
        for col, value in (where or {}).items():
            if value is None:
                clauses.append(f"{q(col)} IS NULL")
            else:
                clauses.append(f"{q(col)} = {self.ph}")
                params.append(self._enc(kinds, col, value))
        for col, op, value in conds:
            if op in _OPS:
                clauses.append(f"{q(col)} {op} {self.ph}")
                params.append(self._enc(kinds, col, value))
            elif op in ("in", "not_in"):
                values = list(value)
                if not values:
                    clauses.append("1=0" if op == "in" else "1=1")
                    continue
                clauses.append(f"{q(col)} {'IN' if op == 'in' else 'NOT IN'} ({', '.join([self.ph] * len(values))})")
                params.extend(self._enc(kinds, col, v) for v in values)
            elif op == "is_null":
                clauses.append(f"{q(col)} IS NULL")
            elif op == "not_null":
                clauses.append(f"{q(col)} IS NOT NULL")
            else:
                raise ValueError(f"unsupported operator {op}")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def select(self, table: str, where: Optional[Mapping[str, Any]], conds: Sequence[Cond], order_by: Sequence[str],
               limit: Optional[int], for_update: bool, skip_locked: bool) -> tuple[str, list[Any]]:
        w, params = self.where(table, where, conds)
        sql = f"SELECT * FROM {q(table)}{w}"
        if order_by:
            sql += " ORDER BY " + ", ".join(
                f"{q(o[1:])} DESC" if o.startswith("-") else f"{q(o)} ASC" for o in order_by
            )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        if for_update and self.row_locks:
            sql += " FOR UPDATE" + (" SKIP LOCKED" if skip_locked else "")
        return sql, params

    def count(self, table: str, where: Optional[Mapping[str, Any]], conds: Sequence[Cond]) -> tuple[str, list[Any]]:
        w, params = self.where(table, where, conds)
        return f"SELECT COUNT(*) FROM {q(table)}{w}", params

    def update(self, table: str, key: Mapping[str, Any], changes: Mapping[str, Any],
               expect: Optional[Mapping[str, Any]]) -> tuple[str, list[Any]]:
        kinds = self._kinds(table)
        if not changes:
            raise ValueError("empty update")
        sets = [f"{q(c)} = {self.ph}" for c in changes]
        params = [self._enc(kinds, c, v) for c, v in changes.items()]
        w, wp = self.where(table, {**key, **(expect or {})}, ())
        return f"UPDATE {q(table)} SET {', '.join(sets)}{w}", params + wp

    def delete(self, table: str, key: Mapping[str, Any], expect: Optional[Mapping[str, Any]]) -> tuple[str, list[Any]]:
        w, wp = self.where(table, {**key, **(expect or {})}, ())
        return f"DELETE FROM {q(table)}{w}", wp

    def next_seq(self) -> str:
        return (
            f"INSERT INTO {q(COUNTERS_TABLE)} ({q('name')}, {q('value')}) VALUES ({self.ph}, 1) "
            f"ON CONFLICT ({q('name')}) DO UPDATE SET {q('value')} = {q(COUNTERS_TABLE)}.{q('value')} + 1 "
            f"RETURNING {q('value')}"
        )
