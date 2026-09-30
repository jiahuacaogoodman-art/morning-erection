"""Result verification: a *fresh* read of the one record an action was meant to change.

Computer Use saying "done" proves nothing. Verification opens a new page (never the page CU
drove, so no stale DOM or optimistic UI), re-runs the declarative extractor and returns
the record. Judging whether the goal holds is the kernel's job (the same condition language
as the decision profile), so there is exactly one implementation of "met".
"""
from typing import Any

from ..browser.observer import observe


def verify(worker: Any, faults: dict[str, Any], *, url: str, origins: list[str], extractor: Any,
           record_id: str, **timeouts: Any) -> dict[str, Any]:
    obs = observe(worker, faults, url=url, origins=origins, extractor=extractor, **timeouts)
    if obs["outcome"] != "SUCCESS":
        return {**obs, "record": None, "present": False}
    rec = obs["records"].get(record_id)
    return {"outcome": "SUCCESS", "record": rec, "present": rec is not None, "url": obs.get("url"),
            "content_digest": obs.get("content_digest"), "error_code": None}
