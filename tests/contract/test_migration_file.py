"""The checked-in PostgreSQL migration is exactly what the table catalogue generates."""
import pathlib

from wakecore.adapters.postgres.migration import render

MIGRATION = pathlib.Path(__file__).resolve().parents[2] / "packages/wakecore/src/wakecore/adapters/postgres/sql/0001_init.sql"


def test_migration_file_matches_catalogue():
    assert MIGRATION.read_text(encoding="utf-8") == render(), \
        "regenerate: python -m wakecore.adapters.postgres.migration > packages/wakecore/src/wakecore/adapters/postgres/sql/0001_init.sql"


def test_every_key_carries_tenant_except_the_ingress_address():
    from wakecore.kernel.ports.tables import TABLES

    for t in TABLES.values():
        assert t.pk[0] == "tenant_id", t.name
        for cols in t.unique:
            assert "tenant_id" in cols or (t.name, cols) == ("source_bindings", ("source_ref",)), (t.name, cols)
        for fk in t.foreign_keys:
            assert fk.columns[0] == "tenant_id" and fk.ref_columns[0] == "tenant_id", (t.name, fk)
