"""macOS app "eye": ObservationPort over the Desktop Runtime's deterministic accessibility read.

The runtime asks the Computer Use bridge for the app's full accessibility tree (never a diff)
and converts it with a declarative extractor: no model is called and nothing leaves the
machine (constraint 3). The task's resource_scope carries everything, so it is authorised,
digest-bound and replayable:

    {"apps": ["com.apple.calculator"],       # the only apps this task may read or operate
     "app": "com.apple.calculator",          # the app to watch (must be in apps)
     "extractor": {...wakecore_ui_runtime.desktop.extractor...},   # tree -> records
     "record_ids": ["display"]}              # targets (observation.target_key)

No credential: the app runs in the user's logged-in macOS session. If the app shows its own
login screen (`extractor.login`), the read is AUTH_REQUIRED, never an empty read; a human signs
in, the model never does. Secure text field values never leave the runtime.

`"missing_targets": "absent"` works as for the web eye (it requires `extractor.ready`).
"""
from typing import Any, Optional

from wakecore.kernel.domain.enums import Completeness, ObservationMode, ObservationOutcome
from wakecore.kernel.ports.observation import ConnectorDescriptor, ObservationRequest, ObservationResult

from ..ui_runtime.client import ProtocolMismatch, RuntimeRejected, UiRuntimeError
from ..ui_runtime.desktop_client import DesktopRuntimeClient

_OUTCOMES = {"AUTH_REQUIRED": ObservationOutcome.AUTH_REQUIRED, "SCHEMA_INVALID": ObservationOutcome.SCHEMA_INVALID,
             "UNAVAILABLE": ObservationOutcome.UNAVAILABLE}


def app_scope(scope: dict[str, Any]) -> tuple[Optional[str], list[str]]:
    """(app, apps) if the scope names an app inside its own allow-list, else (None, [])."""
    app, apps = scope.get("app"), scope.get("apps")
    if not isinstance(app, str) or not isinstance(apps, list) or app not in apps \
            or not all(isinstance(a, str) for a in apps):
        return None, []
    return app, list(apps)


class DesktopAppSource:
    descriptor = ConnectorDescriptor(
        connector_id="desktop.macos", version="0.3.0", supported_resource_types=("app_record",),
        observation_mode=ObservationMode.SNAPSHOT, supports_history_replay=False, supports_entity_revision=False,
        authentication_model="none", required_scopes=("desktop.observe",), rate_limit_per_minute=30,
        max_payload_bytes=1024 * 1024, snapshot_completeness_contract="full_target_scope_when_success",
        read_capability="desktop.observe")

    def __init__(self, client: DesktopRuntimeClient, *, target_key: str = "record_ids") -> None:
        self.client, self.target_key = client, target_key
        self.fetch_count = 0

    def fetch(self, request: ObservationRequest) -> ObservationResult:
        self.fetch_count += 1
        now, scope = request.requested_at, request.resource_scope
        app, apps = app_scope(scope)
        extractor = scope.get("extractor")
        if app is None or not isinstance(extractor, dict):
            return _fail(ObservationOutcome.SCHEMA_INVALID, "scope_needs_app_apps_extractor", now)
        if scope.get("missing_targets") == "absent" and not extractor.get("ready"):
            return _fail(ObservationOutcome.SCHEMA_INVALID, "missing_targets_absent_needs_ready", now)
        try:
            r = self.client.observe(app=app, apps=apps, extractor=extractor)
        except ProtocolMismatch:
            return _fail(ObservationOutcome.UNAVAILABLE, "desktop_runtime_protocol_mismatch", now, retry_after=3600)
        except RuntimeRejected as e:
            if e.code == "desktop_busy":
                return _fail(ObservationOutcome.UNAVAILABLE, "desktop_busy", now, retry_after=30)
            if e.status < 500:
                return _fail(ObservationOutcome.SCHEMA_INVALID, e.code, now)
            return _fail(ObservationOutcome.UNAVAILABLE, "desktop_runtime_error", now, retry_after=60)
        except UiRuntimeError:
            return _fail(ObservationOutcome.UNAVAILABLE, "desktop_runtime_unreachable", now, retry_after=60)
        excerpt = {k: r[k] for k in ("app", "window") if isinstance(r.get(k), str)}
        if r.get("outcome") != "SUCCESS":
            return _fail(_OUTCOMES.get(r.get("outcome"), ObservationOutcome.UNAVAILABLE),
                         str(r.get("error_code") or "desktop_runtime_error"), now, excerpt=excerpt)
        records: dict[str, dict[str, Any]] = r.get("records") or {}
        wanted = scope.get(self.target_key)
        wanted = list(wanted) if isinstance(wanted, list) else sorted(records)
        if scope.get("missing_targets") == "absent":
            read, gaps = wanted, ()
        else:
            read = [k for k in wanted if k in records]
            gaps = tuple(k for k in wanted if k not in records)
        return ObservationResult(
            outcome=ObservationOutcome.PARTIAL if gaps else ObservationOutcome.SUCCESS,
            completeness=Completeness.PARTIAL if gaps else Completeness.COMPLETE,
            scope={self.target_key: read}, records={k: records[k] for k in read if k in records}, observed_at=now,
            source_revision=r.get("content_digest"), coverage_gaps=gaps,
            raw_excerpt={**excerpt, "records_in_app": len(records)})


def _fail(outcome: ObservationOutcome, code: str, now: Any, *, excerpt: Optional[dict] = None,
          retry_after: Optional[int] = None) -> ObservationResult:
    return ObservationResult(outcome=outcome, completeness=Completeness.UNKNOWN, scope={}, records={},
                             observed_at=now, error_code=code, retry_after_seconds=retry_after,
                             raw_excerpt=excerpt or {})
