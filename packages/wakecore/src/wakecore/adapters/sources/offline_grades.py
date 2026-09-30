"""Offline grades portal connector (demo / tests).

Simulates a snapshot-only web source whose failure modes matter for T04 and T17:
login pages, captcha pages, empty responses, partial reads and outages. It never
touches task state; it only returns typed ObservationResults (RFC §13.1).
"""
import copy
import threading
from datetime import datetime
from typing import Any, Optional

from wakecore.kernel.domain.enums import Completeness, ObservationMode, ObservationOutcome
from wakecore.kernel.ports.observation import ConnectorDescriptor, ObservationRequest, ObservationResult

MODES = ("normal", "login_page", "captcha", "empty", "unavailable", "partial", "schema_invalid", "raise")


class OfflineGradesSource:
    descriptor = ConnectorDescriptor(
        connector_id="offline.grades", version="1.0.0", supported_resource_types=("course_grade",),
        observation_mode=ObservationMode.SNAPSHOT, supports_history_replay=False, supports_entity_revision=False,
        authentication_model="session_secret", required_scopes=("grades.read",), rate_limit_per_minute=12,
        max_payload_bytes=256 * 1024, snapshot_completeness_contract="full_target_scope_when_success",
        read_capability="grades.read")

    def __init__(self, courses: Optional[dict[str, dict[str, Any]]] = None, *, require_secret: bool = True) -> None:
        self._lock = threading.Lock()
        self.courses: dict[str, dict[str, Any]] = copy.deepcopy(courses or {})
        self.mode = "normal"
        self.partial_ids: tuple[str, ...] = ()
        self.require_secret = require_secret
        self.fetch_count = 0
        self.render_counter = 0

    # -- test controls -------------------------------------------------------------------
    def set_mode(self, mode: str, *, partial_ids: tuple[str, ...] = ()) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        with self._lock:
            self.mode = mode
            self.partial_ids = tuple(partial_ids)

    def set_score(self, course_id: str, score: Any, **fields: Any) -> None:
        with self._lock:
            rec = self.courses.setdefault(course_id, {"name": course_id, "score": None})
            rec["score"] = score
            rec.update(fields)

    def set_field(self, course_id: str, **fields: Any) -> None:
        with self._lock:
            self.courses.setdefault(course_id, {"name": course_id, "score": None}).update(fields)

    # -- port ----------------------------------------------------------------------------
    def fetch(self, request: ObservationRequest) -> ObservationResult:
        with self._lock:
            self.fetch_count += 1
            self.render_counter += 1
            mode, now = self.mode, request.requested_at
            wanted = list(request.resource_scope.get("course_ids", sorted(self.courses)))
            if mode == "raise":
                raise ConnectionError("simulated socket reset")
            if self.require_secret and not request.secret:
                return self._fail(ObservationOutcome.AUTH_REQUIRED, "missing_session", now)
            if mode == "login_page":
                return self._fail(ObservationOutcome.AUTH_REQUIRED, "login_page", now,
                                  excerpt={"title": "统一身份认证 - 登录"})
            if mode == "captcha":
                return self._fail(ObservationOutcome.AUTH_REQUIRED, "captcha", now, excerpt={"title": "请输入验证码"})
            if mode == "unavailable":
                return self._fail(ObservationOutcome.UNAVAILABLE, "http_503", now, retry_after=120)
            if mode == "schema_invalid":
                return self._fail(ObservationOutcome.SCHEMA_INVALID, "unexpected_layout", now)
            if mode == "empty":
                # A naive scraper reports success with nothing in it; the kernel must not read this
                # as "all grades removed" (T04). No scope is declared, so coverage is insufficient.
                return ObservationResult(outcome=ObservationOutcome.SUCCESS, completeness=Completeness.UNKNOWN,
                                         scope={}, records={}, observed_at=now, raw_excerpt={"body_bytes": 0})
            read = [c for c in wanted if c in self.partial_ids] if mode == "partial" else wanted
            records = {}
            for cid in read:
                if cid in self.courses:
                    rec = copy.deepcopy(self.courses[cid])
                    rec["page_rendered_at"] = f"render-{self.render_counter}"
                    records[cid] = rec
            partial = mode == "partial"
            return ObservationResult(
                outcome=ObservationOutcome.PARTIAL if partial else ObservationOutcome.SUCCESS,
                completeness=Completeness.PARTIAL if partial else Completeness.COMPLETE,
                scope={"course_ids": read, "semester": request.resource_scope.get("semester")},
                records=records, observed_at=now, source_revision=None,
                coverage_gaps=tuple(c for c in wanted if c not in read))

    @staticmethod
    def _fail(outcome: ObservationOutcome, code: str, now: datetime, *, excerpt: Optional[dict] = None,
              retry_after: Optional[int] = None) -> ObservationResult:
        return ObservationResult(outcome=outcome, completeness=Completeness.UNKNOWN, scope={}, records={},
                                 observed_at=now, error_code=code, retry_after_seconds=retry_after,
                                 raw_excerpt=excerpt or {})
