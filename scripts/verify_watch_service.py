"""Real headless Watch acceptance using temporary files and a real local HTTP service."""
import argparse
import http.cookiejar
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from wakecore.adapters.sources.environment import EnvironmentSource

ROOT = Path(__file__).resolve().parents[1]


def wait_until(check, seconds=25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (OSError, ValueError):
            pass
        time.sleep(.25)
    raise AssertionError("Acceptance condition did not complete before deadline")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("/tmp/morning-watch-acceptance.json"))
    args = parser.parse_args()
    results = {}
    with tempfile.TemporaryDirectory(prefix="morning-headless-") as temp:
        root = Path(temp)
        state = root / "service"
        state.mkdir(mode=0o700)
        (state / "gateways.json").write_text(json.dumps({"version": 1, "installation_id": uuid.uuid4().hex, "profiles": [], "active_id": None, "credentials": {}}))
        file = root / "status.txt"
        file.write_text("pending")
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ready"}')
            def log_message(self, *args):
                pass
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=http_server.serve_forever, daemon=True).start()
        source = EnvironmentSource()
        endpoint = f"127.0.0.1:{http_server.server_port}"
        results["real_http"] = source.read({"kind": "http", "target": "http://" + endpoint, "json_pointer": "/status"})["value"] == "ready"
        results["real_network_service"] = source.read({"kind": "service", "target": endpoint})["value"]
        results["real_process"] = source.read({"kind": "process", "target": str(os.getpid())})["value"]
        results["real_folder"] = source.read({"kind": "folder", "target": str(root)})["count"] >= 2
        results["real_log"] = source.read({"kind": "log", "target": str(file)})["value"] == "pending"
        http_server.shutdown()
        http_server.server_close()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        def call(path, body=None):
            req = urllib.request.Request(base + "/api" + path, data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json", "Origin": base, "X-Pulse-Client": "1", "Idempotency-Key": uuid.uuid4().hex})
            with opener.open(req, timeout=15) as response:
                return json.load(response)
        def start():
            process = subprocess.Popen([sys.executable, str(ROOT / "scripts/watch.py"), "--state", str(state), "--port", str(port), "--no-computer"],
                                       cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                wait_until(lambda: call("/bootstrap"))
            except Exception:
                process.terminate()
                process.wait(timeout=10)
                raise
            return process
        def stop(process):
            process.send_signal(signal.SIGINT)
            process.wait(timeout=20)
        process = start()
        try:
            results["headless_pi_absent"] = call("/pulse/state")["pi"] is None
            results["headless_worker_running"] = call("/pulse/platform")["watch_service"]["running"]
            inspected = call("/pulse/platform", {"operation": "inspect_source", "source": {"kind": "file", "target": str(file)}})
            draft = call("/pulse/platform", {"operation": "prepare_watch", "inspection_id": inspected["inspection_id"], "label": "headless acceptance", "condition": "等文件变为 READY", "field": "value", "op": "eq", "value": "READY", "interval_seconds": 2})["draft"]
            key = draft["task_id"]
            call(f"/pulse/watches/{key}/start", {"spec_digest": draft["spec_digest"]})
            wait_until(lambda: call("/pulse/platform")["watches"][0]["state"] == "watching")
            file.write_text("READY")
            wait_until(lambda: len(call("/pulse/platform")["notifications"]) == 1)
            results["real_file_trigger_without_browser"] = True
            call(f"/tasks/{key}/pause", {})
            stop(process)
            process = start()
            results["paused_task_restored"] = call("/pulse/platform")["watches"][0]["lifecycle"] == "PAUSED"
            call(f"/tasks/{key}/resume", {})
            wait_until(lambda: call("/pulse/platform")["watches"][0]["state"] == "watching")
            time.sleep(3)
            results["notification_deduplicated_after_restart"] = len(call("/pulse/platform")["notifications"]) == 1
            results["events_persisted"] = bool(call("/pulse/events?after=0")["events"])
            call("/pulse/platform", {"operation": "module", "name": "watch", "enabled": False})
            results["watch_stops_independently"] = not call("/pulse/platform")["watch_service"]["running"]
        finally:
            if process.poll() is None:
                stop(process)
    assert all(results.values()), results
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
