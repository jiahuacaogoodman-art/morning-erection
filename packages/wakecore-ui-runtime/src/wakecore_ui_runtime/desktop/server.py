"""Desktop Runtime HTTP service, protocol `wakecore.desktop-runtime/1` (localhost only, macOS).

The eye and hand for native macOS apps, through a Computer Use MCP bridge (e.g.
tmustier/codex-computer-use-mcp, `--mcp-command "node .../dist/mcp-server.js"`). Same shape as
the browser runtime (`wakecore.ui-runtime/1`): the same error envelope, journal, send-once
semantics, 409 on a reused key, model egress check, cooperative cancel. Apps are named by
bundle ID and every request carries the allow-list (`apps`) the kernel's grant covers.

  GET  /v1/health                 liveness, protocol, the bridge's status
  GET  /v1/capabilities
  POST /v1/extractors/validate    {extractor}
  POST /v1/observe                {app, apps, extractor}                 deterministic read, no model
  POST /v1/verify                 {app, apps, extractor, record_id}
  POST /v1/act                    {key, attempt?, app, apps, goal, timeout_s?, max_steps?, model_egress?}
  GET  /v1/acts/{key}             the journal record
  POST /v1/acts/{key}/cancel
  GET  /v1/act/status?key=

One desktop, one operation at a time: an observe that arrives while an act drives the app
waits (bounded) and then answers 503 desktop_busy (retryable). The bridge never sees a
request the runtime has not checked; it has no permission model of its own.

Run:  wakecore-desktop-runtime --state DIR --mcp-command "node ~/src/codex-computer-use-mcp/dist/mcp-server.js"
                               [--port 8766] [--openai-key-file F] [--openai-base-url URL] [--model M]
                               [--history auto|server|client] [--token T]
Env:  WAKECORE_DESKTOP_MCP_COMMAND, WAKECORE_DESKTOP_RUNTIME_TOKEN, OPENAI_API_KEY(_FILE), OPENAI_BASE_URL
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from typing import Any, Optional

from .._version import __version__
from ..cua.openai import HISTORY_MODES
from ..journal import Journal, sent_mutations
from ..server import ApiError, Route, _status_key, dispatch, make_handler, read_key_file
from . import extractor as ex
from .loop import BUNDLE_ID, DesktopActLoop, Stop, normalise_apps, read_state
from .mcp import McpBridge, McpError
from .model import ACTION_TYPES, DesktopModelClient
from .tree import TreeError

PROTOCOL = "wakecore.desktop-runtime/1"
ACTION_SPACE = "wakecore.desktop-ax/1"
SCHEMAS = "desktop_runtime"
MAX_STEPS_LIMIT = 200
BUSY_WAIT_S = 30.0
GUARDS = ("app_allow_list", "bundle_id_check", "element_index_only", "write_ahead_journal", "send_once",
          "credential_field_refusal", "credential_values_hidden", "system_key_refusal", "no_elicitation")


def request_digest(b: dict[str, Any]) -> str:
    canon = {"app": b["app"], "apps": sorted(b["apps"]), "goal": b["goal"]}
    return hashlib.sha256(json.dumps(canon, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def fail(outcome: str, code: str, **extra: Any) -> dict[str, Any]:
    return {"outcome": outcome, "error_code": code, "records": {}, **extra}


class DesktopRuntime:
    def __init__(self, state_dir: str, bridge: McpBridge, *, client: Optional[DesktopModelClient] = None,
                 token: str = "") -> None:
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        self.state_dir, self.bridge, self.token = state_dir, bridge, token
        self.client = client or DesktopModelClient()
        self.journal = Journal(state_dir)
        self.interrupted_on_start = self.journal.mark_interrupted()
        self.artifacts = os.path.join(state_dir, "artifacts")
        self.allow_faults, self.faults = False, {}          # the shared dispatcher looks for these
        self._desktop = threading.Lock()                     # one operation on the desktop at a time
        self._act_lock = threading.Lock()
        self._cancels: dict[str, threading.Event] = {}
        self._running: set[str] = set()

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _scope(b: dict[str, Any]) -> None:
        if b["app"] not in normalise_apps(b["apps"]):
            raise ApiError(403, "app_outside_scope", "`app` is not in the `apps` allow-list")

    def _hold(self, timeout: float = BUSY_WAIT_S) -> None:
        if not self._desktop.acquire(timeout=timeout):
            raise ApiError(503, "desktop_busy", "another operation is driving the desktop", retryable=True)

    # -------------------------------------------------------------- the eye
    def _read(self, b: dict[str, Any]) -> dict[str, Any]:
        try:
            cfg = ex.validate(b["extractor"])
        except ex.ExtractorError as e:
            return fail("SCHEMA_INVALID", f"extractor_invalid:{e}")
        self._scope(b)
        app = b["app"]
        self._hold()
        try:
            try:
                state, _image = read_state(self.bridge, app, timeout=60)
            except McpError as e:
                return fail("UNAVAILABLE", e.code, detail=e.detail[:200])
            except TreeError:
                return fail("UNAVAILABLE", "app_state_unparsable")
            except Stop as s:
                return fail("UNAVAILABLE", s.reason, detail=s.detail[:200])
        finally:
            self._desktop.release()
        meta = {"app": app, "window": state.window}
        if ex.is_login(cfg, state):
            return fail("AUTH_REQUIRED", "login_screen", **meta)
        if not ex.is_ready(cfg, state):
            return fail("SCHEMA_INVALID", "not_ready", **meta)
        try:
            records = ex.convert(cfg, state)
        except ex.ExtractorError as e:
            return fail("SCHEMA_INVALID", str(e), **meta)
        digest = hashlib.sha256(json.dumps(records, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {"outcome": "SUCCESS", "records": records, "content_digest": digest, "error_code": None, **meta}

    def observe(self, b: dict[str, Any]) -> dict[str, Any]:
        return self._read(b)

    def verify(self, b: dict[str, Any]) -> dict[str, Any]:
        obs = self._read(b)
        if obs["outcome"] != "SUCCESS":
            return {**obs, "record": None, "present": False}
        rec = obs["records"].get(b["record_id"])
        return {"outcome": "SUCCESS", "record": rec, "present": rec is not None, "app": obs["app"],
                "window": obs["window"], "content_digest": obs["content_digest"], "error_code": None}

    # -------------------------------------------------------------- the hand
    def _dedupe(self, key: str, attempt: Optional[str], digest: str) -> Optional[dict[str, Any]]:
        rec = self.journal.get(key)
        if rec is None:
            return None
        if rec.get("request_digest") != digest:
            raise ApiError(409, "idempotency_key_reused",
                           "this act key was already used for a different request (app, apps or goal differ); "
                           "use a new key",
                           details={"journal_status": rec["status"], "mutating_actions": sent_mutations(rec)})
        if rec["status"] == "in_progress":
            return {"status": "in_progress", "deduplicated": True, "mutating_actions": sent_mutations(rec),
                    "action_space": ACTION_SPACE}
        if sent_mutations(rec) == 0 and attempt is not None and rec.get("attempt") != attempt:
            return None   # a new kernel attempt, and the previous one provably sent nothing
        result = rec.get("result") or {"status": "interrupted", "reason": "runtime_restarted"}
        return {**result, "mutating_actions": sent_mutations(rec), "deduplicated": True,
                "journal_status": rec["status"], "action_space": ACTION_SPACE}

    def act(self, b: dict[str, Any]) -> dict[str, Any]:
        key, attempt, digest = b["key"], b.get("attempt"), request_digest(b)
        with self._act_lock:
            dup = self._dedupe(key, attempt, digest)
            if dup is not None:
                return dup
            self._scope(b)
            expected = b.get("model_egress")
            if expected is not None and expected != self.client.model_egress:
                raise ApiError(409, "model_egress_mismatch",
                               f"the approved model egress is {expected!r} but this runtime sends the app's "
                               f"accessibility tree and screenshots to {self.client.model_egress!r}; nothing was sent",
                               details={"runtime_model_egress": self.client.model_egress})
            cancel = self._cancels.setdefault(key, threading.Event())
        timeout_s = float(b.get("timeout_s", 120))
        try:
            self._hold(timeout=timeout_s)
            try:
                with self._act_lock:            # a concurrent duplicate may have run while we waited
                    dup = self._dedupe(key, attempt, digest)
                    if dup is not None:
                        return dup
                    self._running.add(key)
                loop = DesktopActLoop(self.bridge, self.journal, self.client, self.artifacts)
                r = loop.run(key=key, attempt=attempt, app=b["app"], apps=b["apps"], goal=b["goal"],
                             timeout_s=timeout_s, max_steps=int(b.get("max_steps", 15)), cancel=cancel,
                             request_digest=digest)
            finally:
                self._desktop.release()
        finally:
            with self._act_lock:
                self._running.discard(key)
                if self._cancels.get(key) is cancel:
                    del self._cancels[key]
        return {**r, "action_space": ACTION_SPACE}

    def act_status(self, key: str) -> dict[str, Any]:
        rec = self.journal.get(key)
        if rec is None:
            return {"status": "never_received", "mutating_actions": 0}
        return {"status": rec["status"], "mutating_actions": sent_mutations(rec), "attempt": rec.get("attempt"),
                "result_status": (rec.get("result") or {}).get("status")}

    def act_record(self, key: str) -> dict[str, Any]:
        rec = self.journal.get(key)
        if rec is None:
            raise ApiError(404, "act_not_found", "no act was ever journalled under this key")
        ref = os.path.join("artifacts", hashlib.sha256(key.encode()).hexdigest()[:24])
        adir = os.path.join(self.state_dir, ref)
        files = sorted(os.listdir(adir)) if os.path.isdir(adir) else []
        shots = [f for f in files if re.fullmatch(r"step-\d{3}\.(png|jpg)", f)]
        out = {"session_ref": None, "finished_at": None, "cancel_requested_at": None, "request_digest": None,
               "history": [], **rec}
        out["artifacts"] = {"ref": ref if files else None, "screenshots": shots}
        return out

    def cancel_act(self, key: str) -> dict[str, Any]:
        with self._act_lock:
            ev = self._cancels.get(key)
            running = key in self._running
        rec = self.journal.get(key)
        if ev is None:
            if rec is None:
                raise ApiError(404, "act_not_found", "no act was ever received under this key")
            return {"key": key, "cancel_requested": False, "status": rec["status"]}
        ev.set()
        if running and rec is not None and rec["status"] == "in_progress":
            self.journal.update(key, cancel_requested_at=time.time())
        return {"key": key, "cancel_requested": True, "status": "in_progress" if running else "queued"}

    # -------------------------------------------------------------- meta
    def validate_extractor(self, b: dict[str, Any]) -> dict[str, Any]:
        try:
            cfg = ex.validate(b["extractor"])
        except ex.ExtractorError as e:
            return {"ok": False, "error": {"code": "extractor_invalid", "message": str(e)}}
        return {"ok": True, "mode": "records" if "records" in cfg else "list", "normalized": cfg}

    def health(self) -> dict[str, Any]:
        if self._desktop.acquire(timeout=0.5):
            try:
                bridge = self.bridge.status()
            finally:
                self._desktop.release()
        else:
            bridge = {"ok": True, "busy": True, "server": self.bridge.server_info.get("name"),
                      "server_version": self.bridge.server_info.get("version")}
        return {"ok": True, "protocol": PROTOCOL, "runtime_version": __version__,
                "model_configured": self.client.configured, "model_ref": self.client.model_ref,
                "model_endpoint": self.client.endpoint_host, "model_egress": self.client.model_egress,
                "bridge": bridge, "interrupted_on_start": self.interrupted_on_start}

    def capabilities(self) -> dict[str, Any]:
        return {"protocol": PROTOCOL, "runtime_version": __version__,
                "endpoints": [f"{r.method} {r.template}" for r in ROUTES],
                "extractor": ex.describe(),
                "act": {"available": self.client.configured, "action_space": ACTION_SPACE,
                        "actions": list(ACTION_TYPES), "history": self.client.history_in_use,
                        "model_ref": self.client.model_ref, "model_endpoint": self.client.endpoint_host,
                        "model_egress": self.client.model_egress, "cancel": True,
                        "max_steps_limit": MAX_STEPS_LIMIT, "guards": list(GUARDS)},
                "limits": {"max_body_bytes": 256 * 1024, "timeouts_max_s": 3600}}

    def close(self) -> None:
        self.bridge.close()


ROUTES = [
    Route("GET", "/v1/health", None, lambda rt, b, p, q: rt.health(), response="health.v1.schema.json",
          summary="Liveness, protocol version and the bridge's status"),
    Route("GET", "/v1/capabilities", None, lambda rt, b, p, q: rt.capabilities(),
          response="capabilities.v1.schema.json", summary="What this runtime supports"),
    Route("POST", "/v1/extractors/validate", "extractor.v1.schema.json#/$defs/validate_request",
          lambda rt, b, p, q: rt.validate_extractor(b), response="extractor.v1.schema.json#/$defs/validate_response",
          summary="Check a desktop extractor without touching an app"),
    Route("POST", "/v1/observe", "observe.v1.schema.json", lambda rt, b, p, q: rt.observe(b),
          response="observe.v1.schema.json#/$defs/response", summary="Deterministic read of an app (the eye)"),
    Route("POST", "/v1/verify", "verify.v1.schema.json", lambda rt, b, p, q: rt.verify(b),
          response="verify.v1.schema.json#/$defs/response", summary="Deterministic read of one record"),
    Route("POST", "/v1/act", "act.v1.schema.json", lambda rt, b, p, q: rt.act(b),
          response="act.v1.schema.json#/$defs/response", summary="Run the guarded model loop, at most once per key"),
    Route("GET", "/v1/acts/{key}", None, lambda rt, b, p, q: rt.act_record(p["key"]),
          response="act_record.v1.schema.json", summary="Full journal record of an act"),
    Route("POST", "/v1/acts/{key}/cancel", None, lambda rt, b, p, q: rt.cancel_act(p["key"]),
          response="act.v1.schema.json#/$defs/cancel_response", summary="Cooperatively cancel a running act"),
    Route("GET", "/v1/act/status", None, lambda rt, b, p, q: rt.act_status(_status_key(q)),
          response="act.v1.schema.json#/$defs/status_response", summary="Journal status of an act", query=("key",)),
]


def desktop_dispatch(rt: Any, method: str, raw_path: str, body: Optional[dict[str, Any]]) -> Any:
    return dispatch(rt, method, raw_path, body, routes=ROUTES, schemas=SCHEMAS)


def make_desktop_handler(rt: DesktopRuntime) -> type:
    return make_handler(rt, dispatcher=desktop_dispatch, protocol=PROTOCOL, name="desktop-runtime",
                        timeout_message="the desktop did not finish in time")


def openapi() -> dict[str, Any]:
    from wakecore.protocol.openapi import ui_runtime_openapi
    return ui_runtime_openapi(ROUTES, protocol=PROTOCOL, runtime_version=__version__, schemas=SCHEMAS,
                              title="WakeCore Desktop Runtime",
                              summary="macOS desktop sidecar: deterministic accessibility-tree reads and a guarded, "
                                      "journalled model loop over one allow-listed app",
                              port="8766")


def openapi_json() -> str:
    from wakecore.protocol.openapi import dumps
    return dumps(openapi())


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(prog="wakecore-desktop-runtime", description=f"WakeCore Desktop Runtime ({PROTOCOL})")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__} ({PROTOCOL})")
    ap.add_argument("--state", help="state directory: journal, artifacts (keep it 0700)")
    ap.add_argument("--print-openapi", action="store_true", help="print the OpenAPI 3.1 document and exit")
    ap.add_argument("--port", type=int, default=0, help="0 = pick a free port (printed as LISTENING <port>)")
    ap.add_argument("--mcp-command", default=os.environ.get("WAKECORE_DESKTOP_MCP_COMMAND", ""),
                    help="the Computer Use MCP bridge, e.g. \"node ~/src/codex-computer-use-mcp/dist/mcp-server.js\"")
    ap.add_argument("--openai-base-url")
    ap.add_argument("--model")
    ap.add_argument("--history", choices=HISTORY_MODES)
    ap.add_argument("--token", default=os.environ.get("WAKECORE_DESKTOP_RUNTIME_TOKEN", ""))
    ap.add_argument("--openai-key-file", default=os.environ.get("OPENAI_API_KEY_FILE"),
                    help="file holding only the API key (must not be group/world readable)")
    a = ap.parse_args(argv)
    if a.print_openapi:
        sys.stdout.write(openapi_json())
        return
    if not a.state:
        ap.error("--state is required")
    if not a.mcp_command:
        ap.error("--mcp-command (or WAKECORE_DESKTOP_MCP_COMMAND) is required")
    try:
        client = DesktopModelClient(base_url=a.openai_base_url, api_key=read_key_file(a.openai_key_file),
                                    model=a.model, history=a.history)
    except ValueError as e:
        ap.error(str(e))
    os.makedirs(a.state, mode=0o700, exist_ok=True)
    bridge = McpBridge([os.path.expanduser(p) for p in shlex.split(a.mcp_command)],
                       stderr_path=os.path.join(a.state, "bridge.log"), client_version=__version__)
    print(f"MODEL {client.model_ref} at {client.endpoint_host} (egress {client.model_egress}, "
          f"key {'configured' if client.configured else 'missing: act unavailable'})", flush=True)
    rt = DesktopRuntime(a.state, bridge, client=client, token=a.token)
    httpd = ThreadingHTTPServer(("127.0.0.1", a.port), make_desktop_handler(rt))
    httpd.daemon_threads = True
    print(f"LISTENING {httpd.server_address[1]}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        rt.close()


if __name__ == "__main__":
    main()


__all__ = ["BUNDLE_ID", "DesktopRuntime", "PROTOCOL", "ROUTES", "main", "openapi"]
