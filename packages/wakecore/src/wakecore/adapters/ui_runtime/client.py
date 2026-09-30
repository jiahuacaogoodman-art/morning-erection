"""HTTP client for the UI Runtime sidecar, protocol `wakecore.ui-runtime/1`.

Standard library only: the kernel never imports Playwright or an OpenAI SDK. The protocol is
specified in docs/spec/ui-runtime-protocol.md and wakecore/protocol/schemas/ui_runtime/.

Errors distinguish what matters for side effects:
  RuntimeUnreachable     the connection was refused: the request provably never arrived
  RuntimeTransportError  reset / timeout after sending: it may have been processed
  RuntimeRejected        the runtime answered with an HTTP error (`.status`, `.code`, `.retryable`)
  ProtocolMismatch       the runtime speaks another major protocol version: nothing was sent

The protocol is negotiated lazily: before the first operation the client reads /v1/health
once and refuses a runtime whose major version differs (or that predates versioning).
"""
import json
import threading
import urllib.error
import urllib.request
from typing import Any, Optional
from urllib.parse import quote

PROTOCOL = "wakecore.ui-runtime/1"
PROTOCOL_FAMILY, PROTOCOL_MAJOR = PROTOCOL.split("/")


class UiRuntimeError(RuntimeError):
    pass


class RuntimeUnreachable(UiRuntimeError):
    pass


class RuntimeTransportError(UiRuntimeError):
    pass


class ProtocolMismatch(UiRuntimeError):
    def __init__(self, got: Optional[str], expected: str = PROTOCOL) -> None:
        super().__init__(f"the runtime speaks {got or 'an unversioned protocol'}, this client speaks {expected}")
        self.got, self.expected = got, expected


class RuntimeRejected(UiRuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"http_{status}: {body[:200]}")
        self.status = status
        self.code, self.message, self.retryable, self.details = f"http_{status}", body[:500], status >= 500, {}
        try:
            err = json.loads(body).get("error")
        except (ValueError, AttributeError):
            err = None
        if isinstance(err, dict):              # v1 envelope
            self.code = str(err.get("code") or self.code)
            self.message = str(err.get("message") or "")
            self.retryable = bool(err.get("retryable", self.retryable))
            self.details = err.get("details") if isinstance(err.get("details"), dict) else {}
        elif isinstance(err, str):             # v0.3: {"error": "..."}
            self.code = err.replace(" ", "_")


def _refused(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ConnectionRefusedError)


def compatible(protocol: Any, expected: str = PROTOCOL) -> bool:
    if not isinstance(protocol, str) or "/" not in protocol:
        return False
    family, _, version = protocol.partition("/")
    want_family, _, want_major = expected.partition("/")
    return family == want_family and version.split(".")[0] == want_major


class UiRuntimeClient:
    protocol = PROTOCOL

    def __init__(self, base_url: str, *, token: str = "", timeout: float = 60.0, negotiate: bool = True) -> None:
        self.base_url, self.token, self.timeout = base_url.rstrip("/"), token, timeout
        self.negotiate = negotiate
        self._negotiated: Optional[dict[str, Any]] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ transport
    def _call(self, method: str, path: str, body: Optional[dict[str, Any]] = None,
              timeout: Optional[float] = None) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base_url + path, method=method, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeRejected(e.code, e.read().decode(errors="replace")) from None
        except urllib.error.URLError as e:
            self._negotiated = None
            if _refused(e):
                raise RuntimeUnreachable(str(e.reason)) from None
            raise RuntimeTransportError(str(e.reason)) from None
        except ConnectionRefusedError as e:
            self._negotiated = None
            raise RuntimeUnreachable(str(e)) from None
        except (OSError, TimeoutError) as e:   # reset / timeout while reading the answer
            self._negotiated = None
            raise RuntimeTransportError(type(e).__name__) from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise RuntimeTransportError("bad_json") from None
        if not isinstance(data, dict):
            raise RuntimeTransportError("bad_json")
        return data

    def _op(self, method: str, path: str, body: Optional[dict[str, Any]] = None,
            timeout: Optional[float] = None) -> dict[str, Any]:
        self.ensure_protocol()
        return self._call(method, path, body, timeout)

    def ensure_protocol(self) -> dict[str, Any]:
        """Health once per (re)connection; raises ProtocolMismatch before anything is sent."""
        if not self.negotiate:
            return {}
        with self._lock:
            if self._negotiated is None:
                h = self._call("GET", "/v1/health", timeout=5)
                if not compatible(h.get("protocol"), self.protocol):
                    raise ProtocolMismatch(h.get("protocol"), self.protocol)
                self._negotiated = h
            return self._negotiated

    # ------------------------------------------------------------------ discovery
    def health(self) -> dict[str, Any]:
        return self._call("GET", "/v1/health", timeout=5)

    def capabilities(self) -> dict[str, Any]:
        return self._op("GET", "/v1/capabilities", timeout=10)

    def validate_extractor(self, extractor: dict[str, Any]) -> dict[str, Any]:
        """{ok: true, mode, normalized} or {ok: false, error: {code, message}}; no page is opened."""
        return self._op("POST", "/v1/extractors/validate", {"extractor": extractor}, timeout=10)

    # ------------------------------------------------------------------ the eye
    def observe(self, *, session_ref: str, url: str, origins: list[str], extractor: dict[str, Any],
                timeouts: Optional[dict[str, float]] = None) -> dict[str, Any]:
        body = {"session_ref": session_ref, "url": url, "origins": origins, "extractor": extractor}
        if timeouts:
            body["timeouts"] = timeouts
        return self._op("POST", "/v1/observe", body)

    def verify(self, *, session_ref: str, url: str, origins: list[str], extractor: dict[str, Any],
               record_id: str, timeouts: Optional[dict[str, float]] = None) -> dict[str, Any]:
        body = {"session_ref": session_ref, "url": url, "origins": origins, "extractor": extractor,
                "record_id": record_id}
        if timeouts:
            body["timeouts"] = timeouts
        return self._op("POST", "/v1/verify", body)

    # ------------------------------------------------------------------ the hand
    def act(self, *, key: str, attempt: Optional[str], session_ref: str, goal: str, start_url: str,
            origins: list[str], login: Optional[dict[str, Any]], timeout_s: float, max_steps: int,
            model_egress: Optional[str] = None) -> dict[str, Any]:
        """`model_egress`: the egress the approval covers; the runtime refuses (409 model_egress_mismatch)
        to send screenshots anywhere else."""
        body = {"key": key, "attempt": attempt, "session_ref": session_ref, "goal": goal, "start_url": start_url,
                "origins": origins, "login": login, "timeout_s": timeout_s, "max_steps": max_steps}
        if model_egress is not None:
            body["model_egress"] = model_egress
        return self._op("POST", "/v1/act", body, timeout=timeout_s + 60)

    def act_status(self, key: str) -> dict[str, Any]:
        return self._op("GET", "/v1/act/status?key=" + quote(key, safe=""), timeout=10)

    def act_record(self, key: str) -> Optional[dict[str, Any]]:
        """The journal record, or None if the runtime never received this key."""
        try:
            return self._op("GET", "/v1/acts/" + quote(key, safe=""), timeout=10)
        except RuntimeRejected as e:
            if e.status == 404 and e.code == "act_not_found":
                return None
            raise

    def cancel_act(self, key: str) -> dict[str, Any]:
        return self._op("POST", "/v1/acts/" + quote(key, safe="") + "/cancel", {}, timeout=10)

    # ------------------------------------------------------------------ sessions
    def sessions(self) -> list[dict[str, Any]]:
        return list(self._op("GET", "/v1/sessions", timeout=10).get("sessions") or [])

    def session(self, session_ref: str) -> Optional[dict[str, Any]]:
        try:
            return self._op("GET", "/v1/sessions/" + quote(session_ref, safe=""), timeout=10)
        except RuntimeRejected as e:
            if e.status == 404 and e.code == "session_not_found":
                return None
            raise

    def release_session(self, session_ref: str) -> bool:
        r = self._op("POST", "/v1/sessions/" + quote(session_ref, safe="") + "/release", {}, timeout=60)
        return bool(r.get("released"))
