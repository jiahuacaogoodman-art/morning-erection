"""OS filesystem events accelerate durable probes; interval probes remain the fallback."""
import threading
import time
from pathlib import Path


class FileEvents:
    def __init__(self, api, principal):
        self.api, self.principal = api, principal
        self.observer = None
        self.targets = {}
        self.debounce = {}
        self.pending = set()
        self.lock = threading.RLock()
        self.error = None

    def sync(self, enabled):
        targets = {}
        if enabled:
            for task in self.api.service.list_tasks(self.principal):
                if task["lifecycle"] != "ACTIVE":
                    continue
                source = self.api.service.pulse(self.principal, task["task_id"])["configuration"]["spec"]["source"]["resource_scope"].get("environment", {})
                if source.get("kind") in ("file", "folder", "log"):
                    targets[task["task_id"]] = (source["target"], source["kind"])
        if targets == self.targets:
            return
        self.close()
        self.targets = targets
        if not targets:
            return
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
            owner = self
            class Handler(FileSystemEventHandler):
                def on_any_event(self, event):
                    if event.event_type in ("opened", "closed_no_write"):
                        return
                    owner.changed(event.src_path)
                    if getattr(event, "dest_path", None):
                        owner.changed(event.dest_path)
            self.observer = Observer()
            roots = {str(Path(target).parent) for target, kind in targets.values()}
            for root in roots:
                if Path(root).is_dir():
                    self.observer.schedule(Handler(), root, recursive=True)
            self.observer.start()
            self.error = None
        except Exception:
            self.error = "系统文件事件不可用，持续使用定时观察。"

    def changed(self, filename):
        with self.lock:
            for key, (target, kind) in self.targets.items():
                if filename != target and not (kind == "folder" and Path(filename).parent == Path(target)):
                    continue
                now = time.monotonic()
                if now - self.debounce.get(key, 0) < 1:
                    continue
                self.debounce[key] = now
                try:
                    response = self.api.service.check_task(self.principal, key).body
                    if not response.get("queued"):
                        self.pending.add(key)
                except Exception:
                    pass  # kernel backoff/authority and periodic probes remain authoritative

    def flush(self):
        with self.lock:
            for key in tuple(self.pending):
                if key not in self.targets:
                    self.pending.discard(key)
                    continue
                try:
                    result = self.api.service.check_task(self.principal, key).body
                    if result.get("queued") or result.get("reason") == "already_checking":
                        self.pending.discard(key)
                except Exception:
                    self.pending.discard(key)

    def close(self):
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=3)
            self.observer = None
