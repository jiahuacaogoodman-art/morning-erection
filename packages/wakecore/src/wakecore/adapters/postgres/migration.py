"""Render adapters/postgres/sql/0001_init.sql from the logical table catalogue.

The file is a reviewable artefact for DBAs; the runtime still migrates from the same
catalogue (`PostgresStore.migrate`), and a test fails if the two ever drift.

    python -m wakecore.adapters.postgres.migration > packages/wakecore/src/wakecore/adapters/postgres/sql/0001_init.sql
"""
from wakecore.adapters.postgres.store import _TYPES
from wakecore.adapters.sqlcore import SqlBuilder

HEADER = """\
-- WakeCore Kernel schema v0.2 (WK-KERNEL-002), generated from wakecore/kernel/ports/tables.py.
-- Do not edit by hand: regenerate with
--   python -m wakecore.adapters.postgres.migration > packages/wakecore/src/wakecore/adapters/postgres/sql/0001_init.sql
--
-- Run as a migration role. The application role must be LOGIN NOSUPERUSER NOBYPASSRLS (RFC §9.4);
-- see docs/runbooks/postgres.md.
"""


def render() -> str:
    stmts = SqlBuilder("%s", _TYPES, row_locks=True, encode=lambda kind, value: value).ddl()
    return HEADER + "\nBEGIN;\n\n" + "".join(s + ";\n\n" for s in stmts) + "COMMIT;\n"


if __name__ == "__main__":
    print(render(), end="")
