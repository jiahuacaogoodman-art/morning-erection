"""A minimal stdio MCP client (JSON-RPC 2.0, one JSON message per line), stdlib only.

It drives a desktop Computer Use bridge such as tmustier/codex-computer-use-mcp
(`node dist/mcp-server.js`): tools get_app_state, click, set_value, type_text, press_key,
scroll, select_text, perform_secondary_action, list_apps, computer_use_status.

The bridge itself has no permission model: whatever it is asked, it does. So this client is
deliberately dumb and the runtime decides what may be asked (desktop/loop.py). In particular:
  - the client declares *no* capabilities (no sampling, no roots, no elicitation), and any
    request the server sends us is answered with an error / a decline. A bridge can never make
    the runtime ask a model or a human anything;
  - one process, one call at a time (the bridge drives one desktop);
  - a call that times out kills the process: its effect is unknown, and the next call starts a
    fresh bridge instead of reading a stale answer.
"""
import json
import os
import queue
import subprocess
import threading
import time
from typing import Any, Optional

PROTOCOL_VERSION = "2025-06-18"
STATUS_KEYS = ("brokerVerified", "permissionMode", "brokerVersion", "clientBuild")


class McpError(RuntimeError):
    """code: mcp_unavailable (could not start / died), mcp_timeout (effect unknown),
    mcp_protocol (bad answer), mcp_error (the server returned a JSON-RPC error)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class McpBridge:
    def __init__(self, command: list[str], *, env: Optional[dict[str, str]] = None, init_timeout: float = 60.0,
                 call_timeout: float = 90.0, stderr_path: Optional[str] = None,
                 client_version: str = "0") -> None:
        if not command:
            raise ValueError("an MCP command is required")
        self.command, self.env = list(command), env
        self.init_timeout, self.call_timeout = init_timeout, call_timeout
        self.stderr_path, self.client_version = stderr_path, client_version
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._pending: dict[int, "queue.Queue[dict[str, Any]]"] = {}
        self._next = 0
        self._lock = threading.Lock()          # one call at a time
        self._io = threading.Lock()            # the pending table and stdin
        self.server_info: dict[str, Any] = {}
        self.tools: list[str] = []
        self.starts = 0

    # ------------------------------------------------------------ process
    def _spawn(self) -> None:
        err: Any = subprocess.DEVNULL
        if self.stderr_path:
            fd = os.open(self.stderr_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            err = os.fdopen(fd, "ab")
        try:
            self._proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                          env=self.env, bufsize=0, start_new_session=True)
        except OSError as e:
            raise McpError("mcp_unavailable", f"cannot start the bridge ({type(e).__name__})") from None
        finally:
            if err is not subprocess.DEVNULL:
                err.close()
        self.starts += 1
        proc = self._proc
        threading.Thread(target=self._read, args=(proc,), name="mcp-reader", daemon=True).start()
        try:
            init = self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "wakecore-desktop-runtime", "version": self.client_version}},
                self.init_timeout)
            self.server_info = init.get("serverInfo") or {}
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            listed = self._request("tools/list", {}, self.init_timeout)
            self.tools = sorted(t.get("name") for t in listed.get("tools") or [] if isinstance(t, dict))
        except McpError:
            self.close()
            raise

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def ensure(self) -> None:
        if not self._alive():
            self.close()
            self._spawn()

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except OSError:
                pass
        with self._io:
            for q in self._pending.values():
                q.put({"error": {"code": -32000, "message": "bridge closed"}})
            self._pending.clear()

    # ------------------------------------------------------------ wire
    def _send(self, msg: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise McpError("mcp_unavailable", "bridge not running")
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode()
        try:
            with self._io:
                proc.stdin.write(data)
                proc.stdin.flush()
        except (OSError, ValueError):
            raise McpError("mcp_unavailable", "bridge stdin closed") from None

    def _read(self, proc: "subprocess.Popen[bytes]") -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue                               # a stray log line on stdout
            if not isinstance(msg, dict):
                continue
            if "method" in msg and "id" in msg:        # a request from the server: always refused
                self._refuse(msg)
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                with self._io:
                    q = self._pending.pop(msg["id"], None)
                if q is not None:
                    q.put(msg)
        with self._io:                                 # EOF: the process is gone
            if self._proc is not proc:                 # an old process: close() already answered its calls
                return
            for q in self._pending.values():
                q.put({"error": {"code": -32000, "message": "bridge exited"}})
            self._pending.clear()

    def _refuse(self, msg: dict[str, Any]) -> None:
        if msg.get("method") == "elicitation/create":
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": msg["id"], "result": {"action": "decline"}}
        elif msg.get("method") == "ping":
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
        else:
            reply = {"jsonrpc": "2.0", "id": msg["id"],
                     "error": {"code": -32601, "message": "not supported by this client"}}
        try:
            self._send(reply)
        except McpError:
            pass

    def _request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        q: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=1)
        with self._io:
            self._next += 1
            rid = self._next
            self._pending[rid] = q
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            with self._io:
                self._pending.pop(rid, None)
            self.close()                               # its effect is unknown: never reuse this process
            raise McpError("mcp_timeout", f"{method} did not answer in {timeout:.0f}s") from None
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            if err.get("message") in ("bridge exited", "bridge closed"):
                raise McpError("mcp_unavailable", err["message"])
            raise McpError("mcp_error", str(err.get("message", ""))[:300])
        result = msg.get("result")
        if not isinstance(result, dict):
            raise McpError("mcp_protocol", "result is not an object")
        return result

    # ------------------------------------------------------------ API
    def call(self, tool: str, args: dict[str, Any], timeout: Optional[float] = None) -> dict[str, Any]:
        """tools/call. Returns {"text": str, "images": [(mime, base64)], "is_error": bool, "structured": ...}."""
        with self._lock:
            self.ensure()
            t0 = time.monotonic()
            r = self._request("tools/call", {"name": tool, "arguments": args}, timeout or self.call_timeout)
            texts, images = [], []
            for part in r.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    texts.append(str(part.get("text", "")))
                elif part.get("type") == "image" and isinstance(part.get("data"), str):
                    images.append((str(part.get("mimeType") or "image/png"), part["data"]))
            return {"text": "\n".join(texts), "images": images, "is_error": bool(r.get("isError")),
                    "structured": r.get("structuredContent"), "elapsed_s": round(time.monotonic() - t0, 3)}

    def status(self) -> dict[str, Any]:
        """Bridge health for GET /v1/health: never raises."""
        try:
            r = self.call("computer_use_status", {}, timeout=30) if self._has("computer_use_status") else None
        except McpError as e:
            return {"ok": False, "error": e.code}
        out: dict[str, Any] = {"ok": r is None or not r["is_error"], "server": self.server_info.get("name"),
                               "server_version": self.server_info.get("version")}
        if r is not None and isinstance(r.get("structured"), dict):
            out["detail"] = {k: v for k, v in r["structured"].items()     # no local paths
                             if k in STATUS_KEYS and isinstance(v, (bool, int, str))}
        return out

    def _has(self, tool: str) -> bool:
        with self._lock:
            self.ensure()
        return tool in self.tools
