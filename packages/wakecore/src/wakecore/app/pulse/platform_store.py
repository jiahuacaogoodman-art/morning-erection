"""Small durable console projection. Watch truth remains in WakeCore; Pi owns sessions."""
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class PlatformStore:
    def __init__(self, root):
        self.path = Path(root) / "platform.db"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        with self.db() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT, state TEXT, value TEXT, updated REAL);
                CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, entity TEXT, at REAL, payload TEXT);
                CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, at REAL);
            """)
        os.chmod(self.path, 0o600)
        for job in self.list("computer", states=("running",)):
            self.save("computer", job["id"], {**job, "state": "unknown", "error": "服务重启，请观察后核验；不会重发操作。"})
        for job in self.list('computer', states=('queued',)):
            self.save('computer', job['id'], {**job, 'state': 'cancelled', 'error': '服务已重启；尚未开始的操作没有执行。'})
        for job in self.list("computer", states=("approval",)):
            self.save("computer", job["id"], {**job, "state": "expired", "error": "服务已重启，请重新观察后确认。"})
        for job in self.list("wakeup", states=("dispatching",)):
            self.save("wakeup", job["id"], {**job, "state": "unknown", "error": "服务重启，Pi 的接收结果待核验；未自动重发。"})
        for note in self.list("notification"):
            if note.get("desktop_delivery") == "dispatching":
                self.save("notification", note["id"], {**note, "desktop_delivery": "unknown"})

    @contextmanager
    def db(self):
        with self.lock:
            conn = sqlite3.connect(self.path, timeout=15)
            conn.row_factory = sqlite3.Row
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

    def get(self, kind, key):
        with self.db() as conn:
            row = conn.execute("SELECT value FROM records WHERE id=? AND kind=?", (key, kind)).fetchone()
            return json.loads(row[0]) if row else None

    def list(self, kind, states=None, limit=200):
        with self.db() as conn:
            query, args = "SELECT value FROM records WHERE kind=?", [kind]
            if states:
                query += " AND state IN (" + ",".join("?" for _ in states) + ")"
                args.extend(states)
            rows = conn.execute(query + " ORDER BY updated DESC LIMIT ?", [*args, limit]).fetchall()
            return [json.loads(row[0]) for row in rows]

    def save(self, kind, key, value):
        value = {**value, "id": key, "updated": time.time()}
        with self.db() as conn:
            conn.execute("INSERT INTO records VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,value=excluded.value,updated=excluded.updated",
                         (key, kind, value.get("state", "ready"), json.dumps(value, ensure_ascii=False), value["updated"]))
        return value

    def remove(self, kind, key):
        with self.db() as conn:
            conn.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, key))

    def event(self, kind, entity, payload=None):
        # Callers provide bounded metadata, never credentials or screenshot/input payloads.
        with self.db() as conn:
            cursor = conn.execute("INSERT INTO events(type,entity,at,payload) VALUES (?,?,?,?)", (kind, entity, time.time(), json.dumps(payload or {}, ensure_ascii=False)))
            return cursor.lastrowid

    def events(self, after=0, limit=100):
        with self.db() as conn:
            rows = conn.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)).fetchall()
            return [{"version": 1, "seq": r["seq"], "type": r["type"], "entity": r["entity"], "at": r["at"], "payload": json.loads(r["payload"])} for r in rows]

    def once(self, key):
        with self.db() as conn:
            return conn.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (key, time.time())).rowcount == 1

    def new_id(self, prefix):
        return prefix + "_" + uuid.uuid4().hex[:20]

    def deliver_notification(self, key, note, wakeup=None):
        """Notification, event and optional wake intent commit atomically and only once."""
        at = time.time()
        value = {**note, "id": key, "updated": at}
        with self.db() as conn:
            inserted = conn.execute("INSERT OR IGNORE INTO records VALUES (?,?,?,?,?)", (key, "notification", "unread", json.dumps(value, ensure_ascii=False), at)).rowcount
            if not inserted:
                return False
            conn.execute("INSERT INTO events(type,entity,at,payload) VALUES (?,?,?,?)", ("watch.triggered", note["task_id"], at, json.dumps({"title": note["title"]}, ensure_ascii=False)))
            if wakeup:
                wake = {**wakeup, "id": "watch:" + key, "updated": at}
                conn.execute("INSERT OR IGNORE INTO records VALUES (?,?,?,?,?)", (wake["id"], "wakeup", "pending", json.dumps(wake, ensure_ascii=False), at))
        return True
