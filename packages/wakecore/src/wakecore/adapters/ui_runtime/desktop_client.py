"""HTTP client for the Desktop Runtime sidecar, protocol `wakecore.desktop-runtime/1`.

Same transport, errors and lazy protocol negotiation as UiRuntimeClient (see client.py); only
the operations differ: the eye and the hand address a macOS app by bundle ID, and `apps` (the
task's allow-list, from resource_scope) travels with every request so the runtime checks the
scope again on its side. Standard library only.
"""
from typing import Any, Optional

from .client import UiRuntimeClient

DESKTOP_PROTOCOL = "wakecore.desktop-runtime/1"


class DesktopRuntimeClient(UiRuntimeClient):
    protocol = DESKTOP_PROTOCOL

    # ------------------------------------------------------------------ the eye
    def observe(self, *, app: str, apps: list[str], extractor: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        return self._op("POST", "/v1/observe", {"app": app, "apps": apps, "extractor": extractor})

    def verify(self, *, app: str, apps: list[str], extractor: dict[str, Any],  # type: ignore[override]
               record_id: str) -> dict[str, Any]:
        return self._op("POST", "/v1/verify", {"app": app, "apps": apps, "extractor": extractor,
                                               "record_id": record_id})

    # ------------------------------------------------------------------ the hand
    def act(self, *, key: str, attempt: Optional[str], app: str, apps: list[str], goal: str,  # type: ignore[override]
            timeout_s: float, max_steps: int, model_egress: Optional[str] = None) -> dict[str, Any]:
        """`model_egress`: the egress the approval covers; the runtime refuses (409 model_egress_mismatch)
        to send the app's accessibility tree and screenshots anywhere else."""
        body: dict[str, Any] = {"key": key, "attempt": attempt, "app": app, "apps": apps, "goal": goal,
                                "timeout_s": timeout_s, "max_steps": max_steps}
        if model_egress is not None:
            body["model_egress"] = model_egress
        return self._op("POST", "/v1/act", body, timeout=timeout_s + 60)

    # the desktop runtime has no browser sessions
    def sessions(self) -> list[dict[str, Any]]:
        raise NotImplementedError("the desktop runtime has no sessions")

    def session(self, session_ref: str) -> Optional[dict[str, Any]]:
        raise NotImplementedError("the desktop runtime has no sessions")

    def release_session(self, session_ref: str) -> bool:
        raise NotImplementedError("the desktop runtime has no sessions")
