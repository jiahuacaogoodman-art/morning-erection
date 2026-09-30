"""Manual login into a persistent browser profile (the human types password and MFA).

    python -m wakecore_ui_runtime.login --state DIR --session school_jw --url https://jw.example.edu/courses \\
        --done-url-contains /courses [--runtime http://127.0.0.1:PORT --token T]

A headed Chromium opens on the profile of `session`; the user logs in (password, MFA, captcha)
by hand; the window closes itself once the page reaches `--done-url-contains` and no login
marker is present. The credentials only ever exist in that browser window. WakeCore stores just
the session name as the source binding's secret_ref. If a UI Runtime is running it is asked to
release the profile first (Chromium allows one process per profile).
"""
import argparse
import json
import os
import urllib.request
from typing import Any, Callable, Optional

from .policy import SESSION_REF
from .sessions.manager import VIEWPORT, save_session_cookies

LOGIN_MARKERS = ("/login", "/mfa", "/sso", "/cas", "/auth")


def release(runtime_url: str, session_ref: str, token: str = "") -> None:
    if not SESSION_REF.match(session_ref):
        raise ValueError("invalid session_ref")
    req = urllib.request.Request(runtime_url.rstrip("/") + f"/v1/sessions/{session_ref}/release", method="POST",
                                 data=json.dumps({"session_ref": session_ref}).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    urllib.request.urlopen(req, timeout=60).read()


def interactive_login(state_dir: str, session_ref: str, url: str, *, done_url_contains: str,
                      login_markers: tuple[str, ...] = LOGIN_MARKERS, human: Optional[Callable[[Any], None]] = None,
                      headless: bool = False, timeout_s: float = 600) -> str:
    """Returns the final URL. `human` stands in for the person at the keyboard in tests."""
    from playwright.sync_api import sync_playwright

    if not SESSION_REF.match(session_ref):
        raise ValueError("invalid session_ref")
    profile = os.path.join(state_dir, "profiles", session_ref)
    os.makedirs(profile, mode=0o700, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(profile, headless=headless, viewport=VIEWPORT, locale="zh-CN")
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded")
            if human is not None:
                human(page)
            page.wait_for_url(lambda u: done_url_contains in u and not any(m in u for m in login_markers),
                              timeout=timeout_s * 1000)
            final = page.url
            save_session_cookies(ctx, profile.rstrip(os.sep) + ".session-cookies.json")
            return final
        finally:
            ctx.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="wakecore_ui_runtime.login")
    ap.add_argument("--state", required=True)
    ap.add_argument("--session", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--done-url-contains", required=True)
    ap.add_argument("--runtime")
    ap.add_argument("--token", default=os.environ.get("WAKECORE_UI_RUNTIME_TOKEN", ""))
    a = ap.parse_args()
    if a.runtime:
        release(a.runtime, a.session, a.token)
    final = interactive_login(a.state, a.session, a.url, done_url_contains=a.done_url_contains)
    print(f"logged in; profile '{a.session}' is ready ({final})")


if __name__ == "__main__":
    main()
