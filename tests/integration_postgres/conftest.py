"""PostgreSQL gate (RFC M2). Skipped unless WAKECORE_PG_DSN points at a disposable database.

Every test gets its own schema (search_path), so tests can run against one database without
interfering, and the schema is dropped afterwards.
"""
import uuid

import pytest

from harness import Harness, pg_dsn


def pytest_collection_modifyitems(config, items):
    if pg_dsn():
        return
    skip = pytest.mark.skip(reason="WAKECORE_PG_DSN not set: PostgreSQL gate not run")
    for item in items:
        if "integration_postgres" in str(item.fspath):
            item.add_marker(skip)


@pytest.fixture
def pg_schema():
    import psycopg
    from psycopg.conninfo import make_conninfo

    schema = "wk_test_" + uuid.uuid4().hex[:12]
    with psycopg.connect(pg_dsn(), autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
    yield make_conninfo(pg_dsn(), options=f"-c search_path={schema}")
    with psycopg.connect(pg_dsn(), autocommit=True) as c:
        c.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def pg_harness(pg_schema, tmp_path):
    from wakecore.adapters.postgres.store import PostgresStore

    made = []

    def factory(**kw):
        hh = Harness(str(tmp_path / "unused.db"), store=lambda clock: PostgresStore(pg_schema, clock=clock), **kw)
        made.append(hh)
        return hh

    yield factory
    for hh in made:
        hh.store.close()


@pytest.fixture
def ph(pg_harness):
    return pg_harness()
