"""Web page "eye" (V0.3 P1): ObservationPort over the UI Runtime's deterministic extractor.

High-frequency observation is plain Playwright + a declarative DOM extractor: no screenshot
leaves the machine and no model is called (constraint 3). The task's resource_scope carries
everything the eye needs, so it is authorised, digest-bound and replayable:

    {"origins": ["https://jw.example.edu"],             # the only sites the browser may load
     "url": "https://jw.example.edu/courses",           # the page to watch
     "extractor": {...wakecore_ui_runtime.browser.extractor...},  # table -> records
     "record_ids": ["PHARM", "PATHOPHYS"]}              # targets (observation.target_key)

The binding's secret_ref resolves to an opaque browser `session_ref`: the profile a human
logged into. WakeCore never sees the password, cookies or MFA secret (constraint 5).

A target that is not on the page is a coverage gap (PARTIAL) by default. With
`"missing_targets": "absent"` (which requires an `extractor.ready` selector, so "rendered" is
checked rather than assumed) it counts as read and absent: a record that appears later is then
a real change (e.g. a new todo, a new listing) instead of a blocked source.
"""
from typing import Any, Optional

from wakecore.kernel.domain.enums import Completeness, ObservationMode, ObservationOutcome
from wakecore.kernel.policy.origins import url_in_scope
from wakecore.kernel.ports.observation import ConnectorDescriptor, ObservationRequest, ObservationResult

from ..ui_runtime.client import ProtocolMismatch, UiRuntimeClient, UiRuntimeError

_OUTCOMES = {"AUTH_REQUIRED": ObservationOutcome.AUTH_REQUIRED, "SCHEMA_INVALID": ObservationOutcome.SCHEMA_INVALID,
             "UNAVAILABLE": ObservationOutcome.UNAVAILABLE}


class PlaywrightWebSource:
    descriptor = ConnectorDescriptor(
        connector_id="web.playwright", version="0.3.0", supported_resource_types=("web_record",),
        observation_mode=ObservationMode.SNAPSHOT, supports_history_replay=False, supports_entity_revision=False,
        authentication_model="browser_session", required_scopes=("web.observe",), rate_limit_per_minute=30,
        max_payload_bytes=1024 * 1024, snapshot_completeness_contract="full_target_scope_when_success",
        read_capability="web.observe")

    def __init__(self, client: UiRuntimeClient, *, target_key: str = "record_ids") -> None:
        self.client, self.target_key = client, target_key
        self.fetch_count = 0

    def fetch(self, request: ObservationRequest) -> ObservationResult:
        self.fetch_count += 1
        now, scope = request.requested_at, request.resource_scope
        url, origins, extractor = scope.get("url"), scope.get("origins"), scope.get("extractor")
        if not isinstance(url, str) or not isinstance(origins, list) or not origins or not isinstance(extractor, dict):
            return _fail(ObservationOutcome.SCHEMA_INVALID, "scope_needs_url_origins_extractor", now)
        if scope.get("missing_targets") == "absent" and not extractor.get("ready"):
            return _fail(ObservationOutcome.SCHEMA_INVALID, "missing_targets_absent_needs_ready", now)
        if not url_in_scope(url, origins):
            return _fail(ObservationOutcome.SCHEMA_INVALID, "url_outside_origins", now)
        if not request.secret:
            return _fail(ObservationOutcome.AUTH_REQUIRED, "missing_session", now)
        try:
            r = self.client.observe(session_ref=request.secret, url=url, origins=origins, extractor=extractor)
        except ProtocolMismatch:
            return _fail(ObservationOutcome.UNAVAILABLE, "ui_runtime_protocol_mismatch", now, retry_after=3600)
        except UiRuntimeError:
            return _fail(ObservationOutcome.UNAVAILABLE, "ui_runtime_unreachable", now, retry_after=60)
        excerpt = {k: r[k] for k in ("title", "url") if isinstance(r.get(k), str)}
        if r.get("outcome") != "SUCCESS":
            return _fail(_OUTCOMES.get(r.get("outcome"), ObservationOutcome.UNAVAILABLE),
                         str(r.get("error_code") or "ui_runtime_error"), now, excerpt=excerpt)
        records: dict[str, dict[str, Any]] = r.get("records") or {}
        wanted = scope.get(self.target_key)
        wanted = list(wanted) if isinstance(wanted, list) else sorted(records)
        if scope.get("missing_targets") == "absent":
            # the page rendered completely (the runtime checked its ready marker), so a target that
            # is not on it does not exist yet: covered and absent, not a coverage gap
            read, gaps = wanted, ()
        else:
            read = [k for k in wanted if k in records]
            gaps = tuple(k for k in wanted if k not in records)
        return ObservationResult(
            outcome=ObservationOutcome.PARTIAL if gaps else ObservationOutcome.SUCCESS,
            completeness=Completeness.PARTIAL if gaps else Completeness.COMPLETE,
            scope={self.target_key: read}, records={k: records[k] for k in read if k in records}, observed_at=now,
            source_revision=r.get("content_digest"), coverage_gaps=gaps,
            raw_excerpt={**excerpt, "records_on_page": len(records)})


def _fail(outcome: ObservationOutcome, code: str, now: Any, *, excerpt: Optional[dict] = None,
          retry_after: Optional[int] = None) -> ObservationResult:
    return ObservationResult(outcome=outcome, completeness=Completeness.UNKNOWN, scope={}, records={},
                             observed_at=now, error_code=code, retry_after_seconds=retry_after,
                             raw_excerpt=excerpt or {})
