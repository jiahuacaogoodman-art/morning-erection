"""UI Runtime HTTP service, protocol `wakecore.ui-runtime/1` (localhost only).

The contract is machine-readable: `wakecore/protocol/schemas/ui_runtime/*.v1.schema.json`
(requests are validated against it) and `spec/ui-runtime/openapi.v1.json`. The normative text
is docs/spec/ui-runtime-protocol.md.

  GET  /v1/health                     liveness + protocol version
  GET  /v1/capabilities               what this runtime implements
  POST /v1/extractors/validate        {extractor}                    check a config without opening a page
  POST /v1/observe                    {session_ref, url, origins, extractor, timeouts?}
  POST /v1/verify                     {session_ref, url, origins, extractor, record_id, timeouts?}
  POST /v1/act                        {key, attempt?, session_ref, goal, start_url, origins, login?, timeout_s, max_steps}
  GET  /v1/acts/{key}                 the journal record (writes sent, requests blocked, result, evidence files)
  POST /v1/acts/{key}/cancel          cooperative: honoured between model turns / actions
  GET  /v1/act/status?key=            never_received | in_progress | finished | interrupted   (kept from v0.3)
  GET  /v1/sessions                   browser profiles and whether a browser holds them
  GET  /v1/sessions/{ref}
  POST /v1/sessions/{ref}/release     close the profile so a human can log in with it
  POST /v1/sessions/release           {session_ref}                  (kept from v0.3)
  POST /__faults                      test fault hooks (only with --allow-faults)

`key` is the kernel's effect_key. An act is performed at most once per key: a repeat returns
the journalled result (`deduplicated: true`) instead of touching the page again, unless the
previous attempt provably sent nothing and the kernel started a *new* attempt. The same key
with a different request is refused (409 idempotency_key_reused), never "repaired".

Every error is `{"error": {"code", "message", "retryable", "details"?}}`; every response
carries `WakeCore-Protocol: wakecore.ui-runtime/1`.

The model endpoint may be OpenAI or any OpenAI-compatible Responses endpoint (relay, proxy,
gateway): `--openai-base-url`. Its host is a different recipient of the screenshots, so the
runtime declares it as `model_egress` in health and refuses an act whose `model_egress` (what
the kernel's grant and approval were bound to) differs: 409 model_egress_mismatch, before the
model or the page is touched.

Run:  wakecore-ui-runtime --state DIR [--port 0] [--headed] [--openai-base-url URL]
                          [--model M] [--variant ga|preview|function] [--history auto|server|client]
                          [--token T] [--allow-faults]
      wakecore-ui-runtime --check-model [--openai-key-file F] [--openai-base-url URL] [--model M]
                          [--variant V] [--history H]
                          two tiny model calls to prove the endpoint speaks the protocol; exit 0/1
Env:  OPENAI_API_KEY (or OPENAI_API_KEY_FILE / --openai-key-file), OPENAI_BASE_URL, WAKECORE_CU_MODEL,
      WAKECORE_CU_VARIANT, WAKECORE_CU_HISTORY, WAKECORE_UI_RUNTIME_TOKEN
variant function / history client are for endpoints without the hosted computer tool or without
previous_response_id (Codex-account relays); see cua/openai.py. The guards are the same.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

from wakecore.protocol.jsonschema_lite import SchemaError, registry

from ._version import __version__
from .browser import extractor as ex
from .browser.observer import observe
from .cua.loop import ActLoop
from .cua.openai import HISTORY_MODES, VARIANTS, ResponsesClient
from .cua.selfcheck import check_model, render
from .journal import Journal, sent_mutations
from .policy import SESSION_REF
from .sessions.manager import SessionManager
from .verify.verifier import verify

PROTOCOL = "wakecore.ui-runtime/1"
ACTION_SPACE = "wakecore.computer/1"
MAX_BODY = 256 * 1024
MAX_STEPS_LIMIT = 200
GUARDS = ("origin_allow_list", "write_ahead_journal", "send_once", "duplicate_submit_block", "credential_field_refusal",
          "popup_close", "dialog_dismiss", "no_file_upload", "no_downloads", "safety_checks_to_human")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False,
                 details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.retryable, self.details = status, code, message, retryable, details

    def body(self) -> dict[str, Any]:
        err = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.details:
            err["details"] = self.details
        return {"error": err}


def request_digest(b: dict[str, Any]) -> str:
    """What makes two act requests "the same effect". attempt / timeout_s / max_steps may differ."""
    canon = {"session_ref": b["session_ref"], "goal": b["goal"], "start_url": b["start_url"],
             "origins": sorted(b["origins"]), "login": b.get("login") or None}
    return hashlib.sha256(json.dumps(canon, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _timeouts(b: dict[str, Any]) -> tuple[dict[str, Any], float]:
    t = b.get("timeouts") or {}
    kw: dict[str, Any] = {}
    if "navigation_s" in t:
        kw["timeout_ms"] = int(t["navigation_s"] * 1000)
    if "ready_s" in t:
        kw["ready_timeout_ms"] = int(t["ready_s"] * 1000)
    wait = max(60.0, float(t.get("navigation_s", 15)) + float(t.get("ready_s", t.get("navigation_s", 15))) + 30)
    return kw, wait


class Runtime:
    def __init__(self, state_dir: str, *, headless: bool = True, client: Optional[ResponsesClient] = None,
                 allow_faults: bool = False, token: str = "", trace: bool = True) -> None:
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        self.state_dir = state_dir
        self.sessions = SessionManager(state_dir, headless=headless)
        self.journal = Journal(state_dir)
        self.interrupted_on_start = self.journal.mark_interrupted()
        self.client = client or ResponsesClient()
        self.allow_faults, self.token, self.trace = allow_faults, token, trace
        self.faults: dict[str, Any] = {}
        self.artifacts = os.path.join(state_dir, "artifacts")
        self._act_lock = threading.Lock()
        self._cancels: dict[str, threading.Event] = {}     # act keys received and not yet ended
        self._running: set[str] = set()                      # act keys inside the loop right now

    # -------------------------------------------------------------- operations
    def _on(self, ref: str, fn: Any, timeout: float) -> Any:
        worker = self.sessions.worker(ref)
        return worker.submit(fn).result(timeout=timeout)

    def observe(self, b: dict[str, Any]) -> dict[str, Any]:
        kw, wait = _timeouts(b)

        def op(w: Any) -> dict[str, Any]:
            r = observe(w, self.faults, url=b["url"], origins=b["origins"], extractor=b["extractor"], **kw)
            if r["outcome"] == "SUCCESS":
                w.persist_session_cookies()
            return r
        return self._on(b["session_ref"], op, wait)

    def verify(self, b: dict[str, Any]) -> dict[str, Any]:
        kw, wait = _timeouts(b)
        return self._on(b["session_ref"], lambda w: verify(w, self.faults, url=b["url"], origins=b["origins"],
                                                           extractor=b["extractor"], record_id=b["record_id"], **kw),
                        wait)

    def _dedupe(self, key: str, attempt: Optional[str], digest: str) -> Optional[dict[str, Any]]:
        rec = self.journal.get(key)
        if rec is None:
            return None
        if rec.get("request_digest") not in (None, digest):     # records from v0.3 carry no digest
            raise ApiError(409, "idempotency_key_reused",
                           "this act key was already used for a different request (session_ref, goal, "
                           "start_url, origins or login differ); use a new key",
                           details={"journal_status": rec["status"], "mutating_requests": sent_mutations(rec)})
        if rec["status"] == "in_progress":
            return {"status": "in_progress", "deduplicated": True, "mutating_requests": sent_mutations(rec),
                    "action_space": ACTION_SPACE}
        if sent_mutations(rec) == 0 and attempt is not None and rec.get("attempt") != attempt:
            return None   # a new kernel attempt, and the previous one provably sent nothing
        result = rec.get("result") or {"status": "interrupted", "reason": "runtime_restarted"}
        return {**result, "mutating_requests": sent_mutations(rec), "deduplicated": True,
                "journal_status": rec["status"], "action_space": ACTION_SPACE}

    def act(self, b: dict[str, Any]) -> dict[str, Any]:
        key, attempt, digest = b["key"], b.get("attempt"), request_digest(b)
        with self._act_lock:
            dup = self._dedupe(key, attempt, digest)
            if dup is not None:
                return dup   # the journalled result: no model call, so the egress check does not apply
            expected = b.get("model_egress")
            if expected is not None and expected != self.client.model_egress:
                raise ApiError(409, "model_egress_mismatch",
                               f"the approved model egress is {expected!r} but this runtime sends screenshots to "
                               f"{self.client.model_egress!r}; nothing was sent",
                               details={"runtime_model_egress": self.client.model_egress})
            cancel = self._cancels.setdefault(key, threading.Event())
        timeout_s = float(b.get("timeout_s", 120))

        def op(w: Any) -> dict[str, Any]:
            try:
                with self._act_lock:          # a concurrent duplicate may have run first
                    dup = self._dedupe(key, attempt, digest)
                    if dup is not None:
                        return dup
                    self._running.add(key)
                loop = ActLoop(w, self.faults, self.journal, self.client, self.artifacts, trace=self.trace)
                r = loop.run(key=key, attempt=attempt, goal=b["goal"], start_url=b["start_url"], origins=b["origins"],
                             login=b.get("login"), timeout_s=timeout_s, max_steps=int(b.get("max_steps", 15)),
                             cancel=cancel, request_digest=digest)
            finally:
                with self._act_lock:
                    self._running.discard(key)
                    if self._cancels.get(key) is cancel:
                        del self._cancels[key]
            w.persist_session_cookies()
            return {**r, "action_space": ACTION_SPACE}
        return self._on(b["session_ref"], op, timeout_s + 90)

    def act_status(self, key: str) -> dict[str, Any]:
        rec = self.journal.get(key)
        if rec is None:
            return {"status": "never_received", "mutating_requests": 0}
        return {"status": rec["status"], "mutating_requests": sent_mutations(rec), "attempt": rec.get("attempt"),
                "result_status": (rec.get("result") or {}).get("status")}

    def act_record(self, key: str) -> dict[str, Any]:
        rec = self.journal.get(key)
        if rec is None:
            raise ApiError(404, "act_not_found", "no act was ever journalled under this key")
        ref = os.path.join("artifacts", hashlib.sha256(key.encode()).hexdigest()[:24])
        adir = os.path.join(self.state_dir, ref)
        files = sorted(os.listdir(adir)) if os.path.isdir(adir) else []
        shots = [f for f in files if re.fullmatch(r"step-\d{3}\.png", f)]
        out = {"session_ref": None, "finished_at": None, "cancel_requested_at": None, "request_digest": None,
               "history": [], **rec}
        out["artifacts"] = {"ref": ref if files else None, "screenshots": shots,
                            "trace": "trace.zip" if "trace.zip" in files else None}
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

    def session(self, ref: str) -> dict[str, Any]:
        d = self.sessions.describe(ref)
        if d is None:
            raise ApiError(404, "session_not_found", "no profile and no browser for this session_ref")
        return d

    def release(self, ref: str) -> dict[str, Any]:
        return {"session_ref": ref, "released": self.sessions.release(ref)}

    def validate_extractor(self, b: dict[str, Any]) -> dict[str, Any]:
        try:
            cfg = ex.validate(b["extractor"])
        except ex.ExtractorError as e:
            return {"ok": False, "error": {"code": "extractor_invalid", "message": str(e)}}
        return {"ok": True, "mode": "table" if "table" in cfg else "list", "normalized": cfg}

    def health(self) -> dict[str, Any]:
        return {"ok": True, "protocol": PROTOCOL, "runtime_version": __version__,
                "model_configured": self.client.configured, "model_ref": self.client.model_ref,
                "model_endpoint": self.client.endpoint_host, "model_egress": self.client.model_egress,
                "sessions": self.sessions.active(), "interrupted_on_start": self.interrupted_on_start}

    def capabilities(self) -> dict[str, Any]:
        return {"protocol": PROTOCOL, "runtime_version": __version__,
                "endpoints": [f"{r.method} {r.template}" for r in ROUTES],
                "extractor": ex.describe(),
                "act": {"available": self.client.configured, "action_space": ACTION_SPACE,
                        "variant": self.client.variant, "history": self.client.history_in_use,
                        "model_ref": self.client.model_ref,
                        "model_endpoint": self.client.endpoint_host, "model_egress": self.client.model_egress,
                        "cancel": True,
                        "max_steps_limit": MAX_STEPS_LIMIT, "guards": list(GUARDS)},
                "limits": {"max_body_bytes": MAX_BODY, "timeouts_max_s": 120}}


# ------------------------------------------------------------------ routing

class Route:
    """`schema` validates the request body; `response` names the 200 body's schema (docs + OpenAPI)."""

    def __init__(self, method: str, template: str, schema: Optional[str],
                 call: Callable[[Runtime, dict[str, Any], dict[str, str], dict[str, list[str]]], Any], *,
                 response: str, summary: str = "", query: tuple[str, ...] = (), deprecated: bool = False) -> None:
        self.method, self.template, self.schema, self.call = method, template, schema, call
        self.response, self.summary, self.query, self.deprecated = response, summary, query, deprecated
        self.regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", template) + "$")


def _ref(p: dict[str, str]) -> str:
    ref = p["ref"]
    if not SESSION_REF.match(ref):
        raise ApiError(400, "invalid_session_ref", "session_ref must match ^[A-Za-z0-9_.-]{1,64}$")
    return ref


def _status_key(q: dict[str, list[str]]) -> str:
    key = (q.get("key") or [""])[0]
    if not key:
        raise ApiError(400, "invalid_request", "query parameter `key` is required")
    return key


ROUTES = [
    Route("GET", "/v1/health", None, lambda rt, b, p, q: rt.health(), response="health.v1.schema.json",
          summary="Liveness, protocol version and session count"),
    Route("GET", "/v1/capabilities", None, lambda rt, b, p, q: rt.capabilities(),
          response="capabilities.v1.schema.json", summary="What this runtime supports (negotiate before use)"),
    Route("POST", "/v1/extractors/validate", "extractor.v1.schema.json#/$defs/validate_request",
          lambda rt, b, p, q: rt.validate_extractor(b), response="extractor.v1.schema.json#/$defs/validate_response",
          summary="Check an extractor without opening a page"),
    Route("POST", "/v1/observe", "observe.v1.schema.json", lambda rt, b, p, q: rt.observe(b),
          response="observe.v1.schema.json#/$defs/response", summary="Deterministic read of a page (the eye)"),
    Route("POST", "/v1/verify", "verify.v1.schema.json", lambda rt, b, p, q: rt.verify(b),
          response="verify.v1.schema.json#/$defs/response", summary="Deterministic read of one record"),
    Route("POST", "/v1/act", "act.v1.schema.json", lambda rt, b, p, q: rt.act(b),
          response="act.v1.schema.json#/$defs/response", summary="Run the Computer Use loop, at most once per key"),
    Route("GET", "/v1/acts/{key}", None, lambda rt, b, p, q: rt.act_record(p["key"]),
          response="act_record.v1.schema.json", summary="Full journal record of an act"),
    Route("POST", "/v1/acts/{key}/cancel", None, lambda rt, b, p, q: rt.cancel_act(p["key"]),
          response="act.v1.schema.json#/$defs/cancel_response", summary="Cooperatively cancel a running act"),
    Route("GET", "/v1/act/status", None, lambda rt, b, p, q: rt.act_status(_status_key(q)),
          response="act.v1.schema.json#/$defs/status_response", summary="Journal status of an act (v0.3)",
          query=("key",), deprecated=True),
    Route("GET", "/v1/sessions", None, lambda rt, b, p, q: {"sessions": rt.sessions.describe_all()},
          response="session.v1.schema.json#/$defs/list_response", summary="Browser sessions (no cookies, no paths)"),
    Route("GET", "/v1/sessions/{ref}", None, lambda rt, b, p, q: rt.session(_ref(p)),
          response="session.v1.schema.json#/$defs/session", summary="One browser session"),
    Route("POST", "/v1/sessions/{ref}/release", None, lambda rt, b, p, q: rt.release(_ref(p)),
          response="session.v1.schema.json#/$defs/release_response",
          summary="Close a session's browser so a human can log in with the same profile"),
    Route("POST", "/v1/sessions/release", "session.v1.schema.json#/$defs/release_request",
          lambda rt, b, p, q: rt.release(b["session_ref"]), response="session.v1.schema.json#/$defs/release_response",
          summary="Release a session (v0.3 form)", deprecated=True),
]


def dispatch(rt: Any, method: str, raw_path: str, body: Optional[dict[str, Any]], *,
             routes: Optional[list[Route]] = None, schemas: str = "ui_runtime") -> Any:
    """Route + validate + call. Raises ApiError; returns the JSON body of a 200.
    `routes` / `schemas` let the desktop runtime reuse this with its own contract."""
    u = urlparse(raw_path)
    if u.path == "/__faults" and method == "POST" and rt.allow_faults:
        rt.faults.clear()
        rt.faults.update(body or {})
        return {"faults": rt.faults}
    allowed = []
    for r in ROUTES if routes is None else routes:
        m = r.regex.match(u.path)
        if m is None:
            continue
        if r.method != method:
            allowed.append(r.method)
            continue
        if r.schema and body is not None:
            try:
                registry(schemas).validate(body, r.schema)
            except SchemaError as e:
                code = "invalid_session_ref" if e.path.startswith("$.session_ref") else "invalid_request"
                raise ApiError(400, code, str(e), details={"path": e.path}) from None
        params = {k: unquote(v) for k, v in m.groupdict().items()}
        return r.call(rt, body or {}, params, parse_qs(u.query))
    if allowed:
        raise ApiError(405, "method_not_allowed", f"use {' or '.join(sorted(set(allowed)))}")
    raise ApiError(404, "not_found", "no such endpoint")


def make_handler(rt: Any, *, dispatcher: Optional[Callable[..., Any]] = None, protocol: str = PROTOCOL,
                 name: str = "ui-runtime", timeout_message: str = "the browser did not finish in time") -> type:
    def call(*a: Any) -> Any:   # looked up per request, so tests can wrap the module-level dispatch
        return (dispatcher or dispatch)(*a)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"WakeCore-{name}/{__version__}"

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write(f"{name}: " + fmt % args + "\n")

        def _reply(self, status: int, body: dict[str, Any], *, close: bool = False) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("WakeCore-Protocol", protocol)
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(data)

        def _authorised(self) -> bool:
            if not rt.token:
                return True
            got = self.headers.get("Authorization", "")
            return hmac.compare_digest(got.encode(), f"Bearer {rt.token}".encode())

        def _body(self) -> dict[str, Any]:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError(400, "bad_body", "bad Content-Length") from None
            if n > MAX_BODY:
                raise ApiError(413, "payload_too_large", f"request bodies are limited to {MAX_BODY} bytes",
                               details={"max_body_bytes": MAX_BODY})
            try:
                b = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                raise ApiError(400, "bad_body", "the body is not JSON") from None
            if not isinstance(b, dict):
                raise ApiError(400, "bad_body", "the body must be a JSON object")
            return b

        def _handle(self, method: str) -> None:
            try:
                if not self._authorised():
                    raise ApiError(401, "unauthorised", "missing or wrong bearer token")
                body = self._body() if method == "POST" else None
                return self._reply(200, call(rt, method, self.path, body))
            except ApiError as e:
                return self._reply(e.status, e.body(), close=e.status == 413)
            except FutureTimeout:
                e = ApiError(504, "runtime_timeout", timeout_message, retryable=True)
            except ValueError as exc:
                e = ApiError(400, "invalid_request", str(exc)[:300])
            except Exception as exc:  # noqa: BLE001
                e = ApiError(500, "internal", f"{type(exc).__name__}: {str(exc)[:300]}", retryable=True,
                             details={"type": type(exc).__name__})
            self._reply(e.status, e.body())

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._handle("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle("DELETE")

    return Handler


def read_key_file(path: Optional[str]) -> Optional[str]:
    """The key never appears on a command line or in logs; None falls back to OPENAI_API_KEY."""
    if not path:
        return None
    if os.stat(path).st_mode & 0o077:
        sys.exit(f"{path}: permissions too open, run: chmod 600 {path}")
    with open(path, encoding="utf-8") as f:
        key = f.read().strip()
    if not key:
        sys.exit(f"{path}: empty, paste the OpenAI API key into it")
    return key


def openapi() -> dict[str, Any]:
    """This runtime's OpenAPI document, generated from ROUTES (committed as spec/ui-runtime/openapi.v1.json)."""
    from wakecore.protocol.openapi import ui_runtime_openapi
    return ui_runtime_openapi(ROUTES, protocol=PROTOCOL, runtime_version=__version__)


def openapi_json() -> str:
    from wakecore.protocol.openapi import dumps
    return dumps(openapi())


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(prog="wakecore-ui-runtime", description=f"WakeCore UI Runtime ({PROTOCOL})")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__} ({PROTOCOL})")
    ap.add_argument("--state", help="state directory: profiles, journal, artifacts (keep it 0700)")
    ap.add_argument("--print-openapi", action="store_true", help="print the OpenAPI 3.1 document and exit")
    ap.add_argument("--port", type=int, default=0, help="0 = pick a free port (printed as LISTENING <port>)")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--openai-base-url", help="OpenAI or an OpenAI-compatible Responses endpoint "
                    "(https; default $OPENAI_BASE_URL or https://api.openai.com/v1)")
    ap.add_argument("--model")
    ap.add_argument("--variant", choices=VARIANTS, help="ga (hosted computer tool), preview (legacy), "
                    "function (the same actions as a plain function tool)")
    ap.add_argument("--history", choices=HISTORY_MODES, help="auto (default): previous_response_id, falling back "
                    "to resending the conversation when the endpoint refuses it")
    ap.add_argument("--token", default=os.environ.get("WAKECORE_UI_RUNTIME_TOKEN", ""))
    ap.add_argument("--allow-faults", action="store_true", help="tests only")
    ap.add_argument("--no-trace", action="store_true")
    ap.add_argument("--openai-key-file", default=os.environ.get("OPENAI_API_KEY_FILE"),
                    help="file holding only the API key (must not be group/world readable)")
    ap.add_argument("--check-model", action="store_true",
                    help="make two tiny model calls to check the endpoint, print the result and exit")
    a = ap.parse_args(argv)
    if a.print_openapi:
        sys.stdout.write(openapi_json())
        return
    if not a.state and not a.check_model:
        ap.error("--state is required")
    try:
        client = ResponsesClient(base_url=a.openai_base_url, api_key=read_key_file(a.openai_key_file),
                                 model=a.model, variant=a.variant, history=a.history)
    except ValueError as e:
        ap.error(str(e))
    if a.check_model:
        r = check_model(client)
        print(render(r), flush=True)
        sys.exit(0 if r["ok"] else 1)
    print(f"MODEL {client.model_ref} at {client.endpoint_host} (egress {client.model_egress}, "
          f"key {'configured' if client.configured else 'missing: act unavailable'})", flush=True)
    rt = Runtime(a.state, headless=not a.headed, client=client, allow_faults=a.allow_faults, token=a.token,
                 trace=not a.no_trace)
    httpd = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(rt))
    httpd.daemon_threads = True
    print(f"LISTENING {httpd.server_address[1]}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        rt.sessions.close_all()


if __name__ == "__main__":
    main()
