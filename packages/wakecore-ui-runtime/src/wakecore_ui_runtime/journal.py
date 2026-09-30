"""Durable act journal: the sidecar's write-ahead log of what it did to a web page.

One JSON file per act key (the kernel's effect_key). Written with fsync + atomic rename:
  * `in_progress` before the first browser action;
  * every mutating request (POST/PUT/PATCH/DELETE) is appended *before* the browser is
    allowed to send it, so after a crash the kernel's reconcile can distinguish
    "nothing was ever sent" from "something may have been submitted";
  * the final result when the act ends.
On start-up every `in_progress` record is turned into `interrupted`. A key whose record
shows a possibly-sent mutation is never operated again: repeating it could double-submit.
"""
import hashlib
import json
import os
import threading
import time
from typing import Any, Optional


class Journal:
    def __init__(self, root: str) -> None:
        self.root = os.path.join(root, "journal")
        os.makedirs(self.root, exist_ok=True)
        self.lock = threading.RLock()

    def _path(self, key: str) -> str:
        return os.path.join(self.root, hashlib.sha256(key.encode()).hexdigest()[:40] + ".json")

    def get(self, key: str) -> Optional[dict[str, Any]]:
        with self.lock:
            try:
                with open(self._path(key), encoding="utf-8") as f:
                    return json.load(f)
            except FileNotFoundError:
                return None

    def put(self, rec: dict[str, Any]) -> None:
        with self.lock:
            path = self._path(rec["key"])
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            dfd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)

    def begin(self, key: str, attempt: Optional[str], session_ref: str, *,
              request_digest: Optional[str] = None) -> dict[str, Any]:
        with self.lock:
            prev = self.get(key)
            rec = {"key": key, "attempt": attempt, "session_ref": session_ref, "status": "in_progress",
                   "request_digest": request_digest, "started_at": time.time(), "finished_at": None,
                   "cancel_requested_at": None, "mutating": [], "blocked": [], "result": None,
                   "history": (prev or {}).get("history", []) + ([_summary(prev)] if prev else [])}
            self.put(rec)
            return rec

    def update(self, key: str, **changes: Any) -> dict[str, Any]:
        with self.lock:
            rec = self.get(key)
            rec.update(changes)
            self.put(rec)
            return rec

    def append(self, key: str, field: str, item: dict[str, Any]) -> None:
        with self.lock:
            rec = self.get(key)
            rec[field].append(item)
            self.put(rec)

    def mark_interrupted(self) -> int:
        n = 0
        with self.lock:
            for name in os.listdir(self.root):
                if not name.endswith(".json"):
                    continue
                with open(os.path.join(self.root, name), encoding="utf-8") as f:
                    rec = json.load(f)
                if rec.get("status") == "in_progress":
                    rec["status"] = "interrupted"
                    rec["finished_at"] = time.time()
                    self.put(rec)
                    n += 1
        return n


def _summary(rec: dict[str, Any]) -> dict[str, Any]:
    return {"attempt": rec.get("attempt"), "status": rec.get("status"), "mutating": len(rec.get("mutating", [])),
            "result_status": (rec.get("result") or {}).get("status")}


def sent_mutations(rec: Optional[dict[str, Any]]) -> int:
    """Mutations that may have reached the site (recorded before the browser sent them)."""
    return 0 if rec is None else len(rec.get("mutating", []))
