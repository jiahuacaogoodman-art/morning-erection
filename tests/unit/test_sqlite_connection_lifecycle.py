"""Exercise real SQLite handles under the local HTTP server's short-lived threads."""
import sqlite3
import threading

import pytest

from wakecore.adapters.clock import FakeClock
from wakecore.adapters.sqlite_dev.store import SqliteStore


def test_request_threads_release_database_handles_without_waiting_for_gc(tmp_path, monkeypatch):
    opened = []
    connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        released = False

        def close(self):
            super().close()
            self.released = True

    def tracked(*args, **kwargs):
        conn = connect(*args, factory=TrackedConnection, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, 'connect', tracked)
    store = SqliteStore(str(tmp_path / 'state.db'), FakeClock())
    store.migrate()
    errors = []

    def request():
        try:
            with store.transaction() as tx:
                tx.next_seq('requests')
        except Exception as exc:
            errors.append(exc)

    for _ in range(20):
        workers = [threading.Thread(target=request) for _ in range(6)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
    assert not errors
    assert len(opened) == 121
    assert all(conn.released for conn in opened)
    with store.transaction() as tx:
        assert tx.next_seq('requests') == 121
    assert not store._all
    store.close()


def test_failed_begin_and_rollback_leave_thread_reusable(tmp_path):
    path = str(tmp_path / 'state.db')
    store = SqliteStore(path, FakeClock(), busy_timeout_ms=1)
    store.migrate()
    lock = sqlite3.connect(path, isolation_level=None)
    lock.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            with store.transaction():
                pass
        assert not store._all
    finally:
        lock.execute('ROLLBACK')
        lock.close()
    with pytest.raises(ValueError, match='cancel'):
        with store.transaction() as tx:
            tx.next_seq('rolled-back')
            raise ValueError('cancel')
    with store.transaction() as tx:
        assert tx.next_seq('rolled-back') == 1
        with pytest.raises(RuntimeError, match='nested'):
            with store.transaction():
                pass
    assert not store._all
    store.close()


def test_memory_database_survives_transaction_boundaries():
    store = SqliteStore(':memory:', FakeClock())
    store.migrate()
    with store.transaction() as tx:
        assert tx.next_seq('retained') == 1
    with store.transaction() as tx:
        assert tx.next_seq('retained') == 2
    store.close()
