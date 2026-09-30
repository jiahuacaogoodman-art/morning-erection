"""Shared fixtures.

WAKECORE_TEST_BACKEND=postgres (with WAKECORE_PG_DSN) runs every harness-based test on
PostgreSQL, each in a fresh schema; the default is the SQLite dev store.
"""
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from harness import Harness, pg_dsn  # noqa: E402

PG_BACKEND = os.environ.get("WAKECORE_TEST_BACKEND") == "postgres"


def _pg_store_factory(schemas: list):
    import psycopg
    from psycopg.conninfo import make_conninfo

    from wakecore.adapters.postgres.store import PostgresStore

    schema = "wk_t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(pg_dsn(), autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
    schemas.append(schema)
    dsn = make_conninfo(pg_dsn(), options=f"-c search_path={schema}")
    return lambda clock: PostgresStore(dsn, clock=clock)


def _drop(schemas: list) -> None:
    if not schemas:
        return
    import psycopg

    with psycopg.connect(pg_dsn(), autocommit=True) as c:
        for s in schemas:
            c.execute(f'DROP SCHEMA "{s}" CASCADE')


def _new(path: str, schemas: list, **kw) -> Harness:
    if PG_BACKEND:
        if not pg_dsn():
            pytest.skip("WAKECORE_TEST_BACKEND=postgres needs WAKECORE_PG_DSN")
        kw.setdefault("store", _pg_store_factory(schemas))
    return Harness(path, **kw)


@pytest.fixture
def h(tmp_path):
    schemas: list = []
    harness = _new(str(tmp_path / "wk.db"), schemas)
    yield harness
    harness.store.close()
    _drop(schemas)


@pytest.fixture
def make_harness(tmp_path):
    made, schemas = [], []

    def factory(**kw):
        harness = _new(str(tmp_path / f"wk{len(made)}.db"), schemas, **kw)
        made.append(harness)
        return harness

    yield factory
    for harness in made:
        harness.store.close()
    _drop(schemas)
