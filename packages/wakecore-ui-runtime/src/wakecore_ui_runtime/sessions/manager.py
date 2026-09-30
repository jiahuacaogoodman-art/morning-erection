"""Browser sessions: one persistent Chromium profile per opaque session_ref.

The profile directory holds the cookies a human created by logging in (password + MFA are
typed by the human, never by a model, never stored by WakeCore). Each session gets its own
worker thread with its own Playwright instance, so an operation on one account never blocks
observation of another, and Playwright's thread affinity is respected.
"""
import json
import os
import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Optional

from ..policy import SESSION_REF

VIEWPORT = {"width": 1280, "height": 800}


class SessionClosed(RuntimeError):
    pass


class SessionWorker:
    def __init__(self, ref: str, profile_dir: str, *, headless: bool) -> None:
        self.ref, self.profile_dir, self.headless = ref, profile_dir, headless
        self._q: "queue.Queue[Optional[tuple[Callable, Future]]]" = queue.Queue()
        self._pw = None
        self.context = None
        self.guard: Any = None           # set by the browser module: the context's request guard
        self.busy = False
        self.last_used_at: Optional[float] = None
        self.thread = threading.Thread(target=self._loop, name=f"browser-{ref}", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            fn, fut = item
            if not fut.set_running_or_notify_cancel():
                continue
            self.busy = True
            try:
                fut.set_result(fn(self))
            except BaseException as exc:  # noqa: BLE001 - delivered to the caller
                fut.set_exception(exc)
            finally:
                self.busy, self.last_used_at = False, time.time()
        self._shutdown()

    def ensure_context(self, install_guard: Callable[["SessionWorker"], None]) -> Any:
        if self.context is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            os.makedirs(self.profile_dir, exist_ok=True)
            self.context = self._pw.chromium.launch_persistent_context(
                self.profile_dir, headless=self.headless, viewport=VIEWPORT, accept_downloads=False,
                locale="zh-CN", args=["--disable-features=PasswordManagerOnboarding"])
            self.context.set_default_timeout(15000)
            install_guard(self)
            self.restore_session_cookies()
        return self.context

    # Chromium keeps only persistent cookies in the profile; many portals log in with
    # *session* cookies (no expiry) that a browser restart would drop. They are kept next to
    # the profile (same trust level: the runtime's state dir, 0700) and restored on launch.
    @property
    def cookie_file(self) -> str:
        return self.profile_dir.rstrip(os.sep) + ".session-cookies.json"

    def restore_session_cookies(self) -> None:
        try:
            with open(self.cookie_file, encoding="utf-8") as f:
                cookies = json.load(f)
        except (FileNotFoundError, ValueError):
            return
        if cookies:
            self.context.add_cookies(cookies)

    def persist_session_cookies(self) -> None:
        if self.context is not None:
            save_session_cookies(self.context, self.cookie_file)

    def submit(self, fn: Callable[["SessionWorker"], Any]) -> Future:
        fut: Future = Future()
        self._q.put((fn, fut))
        return fut

    def close(self) -> None:
        self._q.put(None)
        self.thread.join(timeout=30)

    def _shutdown(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        finally:
            self.context = None
            if self._pw is not None:
                self._pw.stop()
                self._pw = None


def save_session_cookies(context: Any, path: str) -> None:
    cookies = [c for c in context.cookies() if c.get("expires", -1) in (-1, None)]
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cookies, f)
    os.replace(path + ".tmp", path)


class SessionManager:
    def __init__(self, state_dir: str, *, headless: bool = True) -> None:
        self.root = os.path.join(state_dir, "profiles")
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        self.headless = headless
        self._lock = threading.Lock()
        self._workers: dict[str, SessionWorker] = {}

    def profile_dir(self, ref: str) -> str:
        if not SESSION_REF.match(ref or ""):
            raise ValueError("invalid session_ref")
        return os.path.join(self.root, ref)

    def worker(self, ref: str) -> SessionWorker:
        path = self.profile_dir(ref)
        with self._lock:
            w = self._workers.get(ref)
            if w is None:
                w = self._workers[ref] = SessionWorker(ref, path, headless=self.headless)
            return w

    def release(self, ref: str) -> bool:
        """Close the profile so a human can log in with it (Chromium locks a profile per process)."""
        self.profile_dir(ref)
        with self._lock:
            w = self._workers.pop(ref, None)
        if w is not None:
            w.close()
        return w is not None

    def describe(self, ref: str) -> Optional[dict[str, Any]]:
        """Public view of one session (no paths, no cookies); None if it has never existed."""
        path = self.profile_dir(ref)
        with self._lock:
            w = self._workers.get(ref)
        exists = os.path.isdir(path)
        if w is None and not exists:
            return None
        state = "closed" if w is None else "busy" if w.busy else "open" if w.context is not None else "closed"
        return {"session_ref": ref, "state": state, "profile_exists": exists,
                "last_used_at": None if w is None else w.last_used_at}

    def describe_all(self) -> list[dict[str, Any]]:
        refs = {n for n in os.listdir(self.root) if SESSION_REF.match(n) and os.path.isdir(os.path.join(self.root, n))}
        refs |= set(self.active())
        return [d for d in (self.describe(r) for r in sorted(refs)) if d is not None]

    def active(self) -> list[str]:
        with self._lock:
            return sorted(self._workers)

    def close_all(self) -> None:
        for ref in self.active():
            self.release(ref)
