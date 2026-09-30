"""Management + ingress HTTP API (RFC §15) as a dependency-free WSGI application.

Three entry points are kept apart (RFC §3.1):
  * management endpoints require `Authorization: Bearer <token>` and map it to a Principal;
  * ingress (`POST /v1/ingress/{source_ref}`) authenticates by HMAC signature only and can
    only persist events, never control tasks;
  * nothing here runs work: workers do that from the database.

Errors carry code, retryable and trace_id; they never echo tokens or request bodies.
"""
import hashlib
import hmac
import json
import logging
import re
import uuid
from typing import Any, Callable, Iterable, Optional

from wakecore.kernel.domain.errors import KernelError, NotFound, SchemaMismatch, Unauthenticated
from wakecore.kernel.domain.taskspec import parse_ts
from wakecore.kernel.service import KernelService, Principal, Response

log = logging.getLogger("wakecore.api")
MAX_BODY = 1024 * 1024
SIGNATURE_HEADER = "HTTP_X_WAKECORE_SIGNATURE"

_STATUS_TEXT = {200: "OK", 201: "Created", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized",
                403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
                413: "Payload Too Large", 422: "Unprocessable Entity", 500: "Internal Server Error"}


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenAuth:
    """Bearer tokens are stored only as SHA-256 digests."""

    def __init__(self, principals_by_digest: dict[str, Principal]) -> None:
        self._by_digest = dict(principals_by_digest)

    @classmethod
    def from_tokens(cls, tokens: dict[str, Principal]) -> "TokenAuth":
        return cls({token_digest(t): p for t, p in tokens.items()})

    def authenticate(self, header: Optional[str]) -> Principal:
        if not header or not header.startswith("Bearer "):
            raise Unauthenticated("missing bearer token")
        got = token_digest(header[len("Bearer "):].strip())
        for known, principal in self._by_digest.items():
            if hmac.compare_digest(known, got):
                return principal
        raise Unauthenticated("invalid bearer token")


class Request:
    def __init__(self, environ: dict[str, Any]) -> None:
        self.environ = environ
        self.method = environ.get("REQUEST_METHOD", "GET").upper()
        self.path = environ.get("PATH_INFO", "/") or "/"
        self._raw: Optional[bytes] = None

    def header(self, name: str) -> Optional[str]:
        return self.environ.get("HTTP_" + name.upper().replace("-", "_"))

    @property
    def raw(self) -> bytes:
        if self._raw is None:
            try:
                length = int(self.environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY:
                raise _HttpError(413, "PAYLOAD_TOO_LARGE", "request body too large")
            self._raw = self.environ["wsgi.input"].read(length) if length else b""
        return self._raw

    def json(self) -> dict[str, Any]:
        if not self.raw:
            return {}
        try:
            body = json.loads(self.raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SchemaMismatch("request body is not JSON") from exc
        if not isinstance(body, dict):
            raise SchemaMismatch("request body must be an object")
        return body

    def expected_version(self, body: dict[str, Any]) -> Optional[int]:
        """`If-Match: "3"` or body `expected_version`; they must agree when both are sent."""
        header = self.header("If-Match")
        from_header = None
        if header:
            m = re.fullmatch(r'\s*(?:W/)?"?(\d+)"?\s*', header)
            if not m:
                raise SchemaMismatch("If-Match must carry a task version")
            from_header = int(m.group(1))
        from_body = body.get("expected_version")
        if from_body is not None and (not isinstance(from_body, int) or isinstance(from_body, bool)):
            raise SchemaMismatch("expected_version must be an integer")
        if from_header is not None and from_body is not None and from_header != from_body:
            raise SchemaMismatch("If-Match and expected_version disagree")
        return from_header if from_header is not None else from_body


class _HttpError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code = status, code


Handler = Callable[[Request, Optional[Principal], dict[str, str]], Response]


class WakeCoreAPI:
    def __init__(self, service: KernelService, auth: TokenAuth) -> None:
        self.service = service
        self.auth = auth
        s = service
        self.routes: list[tuple[str, re.Pattern[str], bool, Handler]] = []
        add = self._add
        add("POST", r"/v1/tasks", self._create_task)
        add("GET", r"/v1/tasks", lambda r, p, a: Response(200, {"items": s.list_tasks(p)}))
        add("POST", r"/v1/tasks/(?P<id>[^/]+)/activate", self._activate)
        add("POST", r"/v1/tasks/(?P<id>[^/]+)/pause", self._control("pause"))
        add("POST", r"/v1/tasks/(?P<id>[^/]+)/resume", self._control("resume"))
        add("POST", r"/v1/tasks/(?P<id>[^/]+)/cancel", self._control("cancel"))
        add("GET", r"/v1/tasks/(?P<id>[^/]+)", self._get_task)
        add("GET", r"/v1/tasks/(?P<id>[^/]+)/timeline", lambda r, p, a: Response(200, s.timeline(p, a["id"])))
        add("GET", r"/v1/runs/(?P<id>[^/]+)", lambda r, p, a: Response(200, s.get_run(p, a["id"])))
        add("GET", r"/v1/evidence/(?P<id>[^/]+)", lambda r, p, a: Response(200, s.get_evidence(p, a["id"])))
        add("POST", r"/v1/approvals/(?P<id>[^/]+)/approve", self._approve)
        add("POST", r"/v1/approvals/(?P<id>[^/]+)/reject", self._reject)
        add("POST", r"/v1/actions/(?P<id>[^/]+)/revise", self._revise)
        add("POST", r"/v1/actions/(?P<id>[^/]+)/resolve", self._resolve)
        add("POST", r"/v1/grants", self._grant)
        add("POST", r"/v1/grants/(?P<id>[^/]+)/revoke", lambda r, p, a: s.revoke_grant(
            p, a["id"], idem_key=_idem(r)))
        add("POST", r"/v1/bindings", self._binding)
        add("POST", r"/v1/bindings/(?P<id>[^/]+)/reauthorised", lambda r, p, a: s.mark_source_reauthorised(
            p, a["id"], idem_key=_idem(r)))
        add("GET", r"/v1/tools", lambda r, p, a: Response(200, {"items": s.list_tools(p)}))
        add("GET", r"/v1/connectors", lambda r, p, a: Response(200, {"items": s.list_connectors(p)}))
        add("POST", r"/v1/ingress/(?P<id>[^/]+)", self._ingress, authenticated=False)
        add("GET", r"/healthz", lambda r, p, a: Response(200, {"status": "live"}), authenticated=False)

    def _add(self, method: str, pattern: str, handler: Handler, *, authenticated: bool = True) -> None:
        self.routes.append((method, re.compile(pattern + r"/?"), authenticated, handler))

    # ------------------------------------------------------------------ WSGI

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        trace_id = environ.get("HTTP_X_TRACE_ID") or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", trace_id):
            trace_id = uuid.uuid4().hex
        req = Request(environ)
        headers = [("Content-Type", "application/json; charset=utf-8"), ("X-Trace-Id", trace_id)]
        try:
            resp, extra = self._dispatch(req)
            headers += extra
            status, body = resp.status_code, resp.body
            if resp.replayed:
                headers.append(("Idempotent-Replayed", "true"))
        except KernelError as exc:
            status = exc.http_status
            body = {"error": {**exc.to_dict(), "trace_id": trace_id}}
        except _HttpError as exc:
            status = exc.status
            body = {"error": {"code": exc.code, "message": str(exc), "retryable": False, "details": {},
                              "trace_id": trace_id}}
        except Exception:  # noqa: BLE001 - never leak internals or bodies
            log.exception("unhandled error trace_id=%s", trace_id)
            status = 500
            body = {"error": {"code": "INTERNAL", "message": "internal error", "retryable": True, "details": {},
                              "trace_id": trace_id}}
        payload = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
        headers.append(("Content-Length", str(len(payload))))
        start_response(f"{status} {_STATUS_TEXT.get(status, 'Error')}", headers)
        return [payload]

    def _dispatch(self, req: Request) -> tuple[Response, list[tuple[str, str]]]:
        path_matched = False
        for method, pattern, authenticated, handler in self.routes:
            m = pattern.fullmatch(req.path)
            if not m:
                continue
            path_matched = True
            if method != req.method:
                continue
            principal = self.auth.authenticate(req.header("Authorization")) if authenticated else None
            resp = handler(req, principal, m.groupdict())
            extra = []
            if req.method == "GET" and isinstance(resp.body, dict) and "version" in resp.body and \
                    pattern.pattern.startswith(r"/v1/tasks/(?P<id>[^/]+)/?"):
                extra.append(("ETag", f'"{resp.body["version"]}"'))
            return resp, extra
        if path_matched:
            raise _HttpError(405, "METHOD_NOT_ALLOWED", "method not allowed")
        raise NotFound("no such endpoint")

    # ------------------------------------------------------------------ handlers

    def _create_task(self, r: Request, p: Principal, a: dict[str, str]) -> Response:
        body = r.json()
        spec = body.get("spec", body)
        return self.service.create_task(p, spec, idem_key=_idem(r))

    def _activate(self, r: Request, p: Principal, a: dict[str, str]) -> Response:
        body = r.json()
        return self.service.activate_task(p, a["id"], spec_version=body.get("spec_version"),
                                          spec_digest=body.get("spec_digest"),
                                          expected_version=r.expected_version(body), idem_key=_idem(r))

    def _control(self, op: str) -> Handler:
        def handler(r: Request, p: Principal, a: dict[str, str]) -> Response:
            body = r.json()
            ev = r.expected_version(body)
            if op == "pause":
                return self.service.pause_task(p, a["id"], expected_version=ev, idem_key=_idem(r))
            if op == "resume":
                return self.service.resume_task(p, a["id"], expected_version=ev, idem_key=_idem(r))
            reason = str(body.get("reason") or "cancelled_by_user")[:200]
            return self.service.cancel_task(p, a["id"], reason=reason, expected_version=ev, idem_key=_idem(r))
        return handler

    def _get_task(self, r: Request, p: Principal, a: dict[str, str]) -> Response:
        return Response(200, self.service.get_task(p, a["id"]))

    def _approve(self, r: Request, p: Principal, a: dict[str, str]) -> Response:
        body = r.json()
        pd = body.get("payload_digest")
        if not isinstance(pd, str):
            raise SchemaMismatch("payload_digest is required: approvals bind to exact content")
        return self.service.approve(p, a["id"], payload_digest=pd, idem_key=_idem(r))

    def _reject(self, r: Request, p: Principal, a: dict[str, str]) -> Response:
        body = r.json()
        reason = body.get("reason")
        return self.service.reject(p, a["id"], reason=str(reason)[:200] if reason else None, idem_key=_idem(r))

    def _revise(self, r: Request, p: Principal, a: dict[str, str]) -> Response:
        body = r.json()
        if not isinstance(body.get("payload"), dict):
            raise SchemaMismatch("payload object is required")
        return self.service.revise_action(p, a["id"], payload=body["payload"], idem_key=_idem(r))

    def _resolve(self, r: Request, p: Principal, a: dict[str, str]) -> Response:
        body = r.json()
        note = body.get("note")
        if not isinstance(note, str) or not note:
            raise SchemaMismatch("note is required")
        return self.service.resolve_action(p, a["id"], note=note, idem_key=_idem(r))

    def _grant(self, r: Request, p: Principal, a: dict[str, str]) -> Response:
        b = r.json()
        for key in ("grant_ref", "capabilities"):
            if key not in b:
                raise SchemaMismatch(f"{key} is required")
        return self.service.register_grant(
            p, grant_ref=str(b["grant_ref"]), capabilities=_strs(b["capabilities"], "capabilities"),
            data_egress=_strs(b.get("data_egress", []), "data_egress"), resource_scope=b.get("resource_scope") or {},
            expires_at=parse_ts(b["expires_at"], "expires_at") if b.get("expires_at") else None, idem_key=_idem(r))

    def _binding(self, r: Request, p: Principal, a: dict[str, str]) -> Response:
        b = r.json()
        for key in ("source_ref", "connector_id", "source_uri"):
            if not isinstance(b.get(key), str):
                raise SchemaMismatch(f"{key} is required")
        return self.service.register_binding(
            p, source_ref=b["source_ref"], connector_id=b["connector_id"], source_uri=b["source_uri"],
            resource_scope=b.get("resource_scope") or {}, capabilities=_strs(b.get("capabilities", []), "capabilities"),
            secret_ref=b.get("secret_ref"), ingress_secret_ref=b.get("ingress_secret_ref"), idem_key=_idem(r))

    def _ingress(self, r: Request, p: None, a: dict[str, str]) -> Response:
        return self.service.ingest(a["id"], r.raw, r.environ.get(SIGNATURE_HEADER))


def _idem(r: Request) -> Optional[str]:
    return r.header("Idempotency-Key")


def _strs(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SchemaMismatch(f"{where} must be a list of strings")
    return value
