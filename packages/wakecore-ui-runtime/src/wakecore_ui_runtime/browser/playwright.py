"""The request guard every browser context runs under (the network half of the sandbox).

  * Requests to origins outside the current allow-list are aborted (and logged). A blocked
    main-frame navigation is an "origin escape": the CU loop stops on it.
  * Observation is read-only: any non-GET/HEAD/OPTIONS request while observing is aborted.
  * While acting, each mutating request is written to the journal *before* it is released
    (write-ahead), sent exactly once by the guard itself (route.fetch, no retries), and an
    identical repeat (same method, URL and body) within one act is aborted as a duplicate.
  * Fault hooks (only when the server runs with --allow-faults) kill the process right
    before a mutating request is sent, or right after its response arrives.
"""
import os
import time
from typing import Any, Optional
from urllib.parse import urlsplit

from ..policy import SAFE_METHODS, in_scope


def _path_only(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


class RequestGuard:
    def __init__(self, faults: dict[str, Any]) -> None:
        self.faults = faults
        self.mode = "idle"            # idle | observe | act
        self.origins: frozenset[str] = frozenset()
        self.journal: Any = None
        self.key: Optional[str] = None
        self.seen: set[tuple[str, str, str]] = set()
        self.blocked: list[dict[str, Any]] = []
        self.escaped = False
        self.inflight = 0
        self.owned: list[Any] = []

    # -------------------------------------------------------------- lifecycle
    def enter(self, mode: str, origins: frozenset[str], *, journal: Any = None, key: Optional[str] = None) -> None:
        self.mode, self.origins, self.journal, self.key = mode, origins, journal, key
        self.seen, self.blocked, self.escaped, self.inflight = set(), [], False, 0

    def leave(self) -> None:
        self.mode, self.origins, self.journal, self.key = "idle", frozenset(), None, None

    def own(self, page: Any) -> Any:
        self.owned.append(page)
        page.on("dialog", lambda d: self._dialog(d))
        page.on("filechooser", lambda fc: self._block("file_chooser", page.url, "file_upload_not_allowed"))
        return page

    def disown(self, page: Any) -> None:
        if page in self.owned:
            self.owned.remove(page)
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass

    def close_strays(self, context: Any) -> int:
        """Popups / new tabs the page opened: closed; they are never operated."""
        n = 0
        for p in list(context.pages):
            if p not in self.owned:
                try:
                    p.close()
                except Exception:  # noqa: BLE001
                    pass
                n += 1
                self._block("popup", "", "popup_closed")
        return n

    # -------------------------------------------------------------- hooks
    def _dialog(self, dialog: Any) -> None:
        self._block("dialog", "", f"dialog_dismissed:{dialog.type}")
        try:
            dialog.dismiss()
        except Exception:  # noqa: BLE001
            pass

    def _block(self, method: str, url: str, reason: str) -> None:
        item = {"method": method, "url": _path_only(url) if url else "", "reason": reason, "at": time.time()}
        self.blocked.append(item)
        if self.journal is not None and self.key is not None:
            self.journal.append(self.key, "blocked", item)

    def on_route(self, route: Any, request: Any) -> None:
        url, method = request.url, request.method.upper()
        if not in_scope(url, self.origins):
            main_nav = False
            try:
                main_nav = request.is_navigation_request() and request.frame.parent_frame is None
            except Exception:  # noqa: BLE001 - frame may already be gone
                pass
            if main_nav:
                self.escaped = True
            self._block(method, url, "origin_outside_scope" + (":navigation" if main_nav else ""))
            return route.abort("blockedbyclient")
        if method in SAFE_METHODS:
            return route.continue_()
        if self.mode != "act":
            self._block(method, url, "read_only_observation")
            return route.abort("blockedbyclient")
        sig = (method, url, request.post_data or "")
        if sig in self.seen:
            self._block(method, url, "duplicate_submit_blocked")
            return route.abort("blockedbyclient")
        self.seen.add(sig)
        if self.faults.get("exit_before_mutating"):
            os._exit(17)  # crash: the request was decided on but never sent
        journal, key = self.journal, self.key
        journal.append(key, "mutating", {"method": method, "url": _path_only(url), "at": time.time()})
        self.inflight += 1
        # Send it exactly once ourselves. With route.continue_() Chromium's network stack may
        # silently re-send a POST whose keep-alive socket was dropped without a response
        # (observed against a portal that commits and then resets the connection) -- a second
        # submit the journal never sees. The API request context shares the browser context's
        # cookies, does not retry, and does not follow redirects, so the page still does.
        try:
            resp = route.fetch(max_redirects=0, max_retries=0)
        except Exception:  # noqa: BLE001 - reset / timeout: the outcome is unknown, never re-sent
            self._record_response(journal, key, None)
            return route.abort("failed")
        self._record_response(journal, key, resp.status)
        if self.faults.get("exit_after_mutating_response"):
            os._exit(18)  # crash: the site processed the submit, the runtime never reported back
        route.fulfill(response=resp)

    @staticmethod
    def _record_response(journal: Any, key: Optional[str], status: Optional[int]) -> None:
        rec = journal.get(key)
        if rec and rec["mutating"]:
            rec["mutating"][-1]["response_status"] = status
            journal.put(rec)

    def on_done(self, request: Any) -> None:
        if request.method.upper() not in SAFE_METHODS and self.inflight > 0 and in_scope(request.url, self.origins):
            self.inflight -= 1


def install_guard(worker: Any, faults: dict[str, Any]) -> None:
    guard = RequestGuard(faults)
    ctx = worker.context
    ctx.route("**/*", guard.on_route)
    ctx.on("requestfinished", guard.on_done)
    ctx.on("requestfailed", guard.on_done)
    worker.guard = guard
    for p in list(ctx.pages):  # the blank start page of a persistent context
        p.close()
