"""The eye: navigate, detect login walls, extract records. Deterministic, read-only, no model."""
import hashlib
import json
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

from ..policy import in_scope, normalise
from . import extractor as ex
from .playwright import install_guard


def fail(outcome: str, code: str, **extra: Any) -> dict[str, Any]:
    return {"outcome": outcome, "error_code": code, "records": {}, **extra}


def retry_after(value: str | None) -> int | None:
    """HTTP seconds or date; a malformed header never turns an error into success."""
    if not value:
        return None
    try:
        if value.strip().isdigit():
            return min(604800, max(1, int(value)))
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return min(604800, max(1, math.ceil((date - datetime.now(timezone.utc)).total_seconds())))
    except (ValueError, TypeError, OverflowError):
        return None


def observe(worker: Any, faults: dict[str, Any], *, url: str, origins: list[str], extractor: Any,
            timeout_ms: int = 15000, ready_timeout_ms: Optional[int] = None) -> dict[str, Any]:
    """`timeout_ms` bounds the navigation, `ready_timeout_ms` (default: the same) the ready wait."""
    try:
        cfg = ex.validate(extractor)
    except ex.ExtractorError as e:
        return fail("SCHEMA_INVALID", f"extractor_invalid:{e}")
    allowed = normalise(origins)
    if not in_scope(url, allowed):
        return fail("UNAVAILABLE", "origin_outside_scope")
    ctx = worker.ensure_context(lambda w: install_guard(w, faults))
    guard = worker.guard
    guard.enter("observe", allowed)
    page = guard.own(ctx.new_page())
    try:
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as e:  # noqa: BLE001 - network error, DNS, timeout
            if guard.escaped:
                return fail("AUTH_REQUIRED", "redirected_off_origin")
            return fail("UNAVAILABLE", "navigation_failed", detail=type(e).__name__)
        if resp is not None and (resp.status == 429 or resp.status >= 500):
            return fail("UNAVAILABLE", f"http_{resp.status}",
                        retry_after_seconds=retry_after(resp.header_value("retry-after")), url=page.url)
        wait_for = ex.ready_selector(cfg)
        if wait_for:
            try:
                page.wait_for_selector(wait_for, state="attached",
                                       timeout=timeout_ms if ready_timeout_ms is None else ready_timeout_ms)
            except Exception:  # noqa: BLE001 - never rendered: judged below (login wall or schema)
                pass
        final, title = page.url, page.title()
        meta = {"url": final, "title": title}
        if guard.escaped or not in_scope(final, allowed):
            return fail("AUTH_REQUIRED", "redirected_off_origin", **meta)   # e.g. an external SSO wall
        if ex.is_login(cfg, final, title, page):
            return fail("AUTH_REQUIRED", "login_page", **meta)
        if resp is not None and resp.status >= 500:
            return fail("UNAVAILABLE", f"http_{resp.status}", **meta)
        if resp is not None and resp.status in (401, 403):
            return fail("AUTH_REQUIRED", f"http_{resp.status}", **meta)
        if cfg.get("ready") and page.query_selector(cfg["ready"]) is None:
            return fail("SCHEMA_INVALID", "not_ready", **meta)
        try:
            records = ex.convert(cfg, page.evaluate(ex.EXTRACT_JS, cfg))
        except ex.ExtractorError as e:
            return fail("SCHEMA_INVALID", str(e), **meta)
        digest = hashlib.sha256(json.dumps(records, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {"outcome": "SUCCESS", "records": records, "content_digest": digest, "error_code": None, **meta}
    finally:
        guard.leave()
        guard.disown(page)
        guard.close_strays(ctx)
