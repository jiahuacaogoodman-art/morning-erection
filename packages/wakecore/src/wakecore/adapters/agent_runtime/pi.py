"""Pi Agent Core JSON-line process bridge. Credentials travel over stdin, never argv."""
import json
import os
import queue
import shutil
import subprocess
import threading
import uuid
from pathlib import Path


class PiRuntimeError(RuntimeError):
    pass


class PiAgentRuntime:
    def __init__(self, *, runtime_path, workspace, state_dir, client):
        self.runtime_path = Path(runtime_path)
        self.workspace = Path(workspace).resolve()
        self.state_dir = Path(state_dir)
        self.client = client
        self.process = None
        self.events = queue.Queue()
        self.lock = threading.Lock()

    @property
    def configured(self):
        return bool(self.client.configured and (self.runtime_path.parent / "node_modules" / "@earendil-works" / "pi-agent-core").exists())

    def _start(self):
        if self.process and self.process.poll() is None:
            return
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.events = queue.Queue()
        node = shutil.which("node")
        if not node or not self.configured:
            raise PiRuntimeError("agent_runtime_not_configured")
        prepared = subprocess.run([node, str(self.runtime_path.parent / "prepare-runtime.mjs")],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        if prepared.returncode:
            raise PiRuntimeError("agent_runtime_dependency_patch_missing")
        self.process = subprocess.Popen([node, str(self.runtime_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            env={"PATH": os.environ.get("PATH", ""), "LANG": "en_US.UTF-8"}, cwd=self.workspace)
        process = self.process

        def read():
            for line in process.stdout:
                try:
                    self.events.put(json.loads(line))
                except ValueError:
                    pass
            self.events.put({"type": "process_ended"})
        threading.Thread(target=read, daemon=True).start()
        self._send({"type": "configure", "workspace": str(self.workspace), "baseUrl": self.client.base_url,
            "apiKey": self.client.api_key, "model": self.client.model, "sessionFile": str(self.state_dir / "pi-session.json")})
        ready = self._next(30)
        if ready.get("type") != "ready":
            self.close()
            raise PiRuntimeError("agent_runtime_start_failed")

    def _send(self, value):
        self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _next(self, timeout=90):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise PiRuntimeError("agent_runtime_timeout") from None

    def run(self, message, on_event):
        with self.lock:
            self._start()
            request_id = uuid.uuid4().hex
            self._send({"type": "prompt", "id": request_id, "message": message})
            while True:
                event = self._next()
                if event.get("type") == "process_ended":
                    raise PiRuntimeError("agent_runtime_stopped")
                if event.get("id") != request_id:
                    continue
                if event["type"] == "error":
                    raise PiRuntimeError("agent_execution_failed")
                on_event(event)
                if event["type"] == "complete":
                    return event

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
