"""Explicit human login window. No model, credential extraction, or automatic login."""
import os
import threading

from .manager import VIEWPORT, save_session_cookies


class HumanLogin:
    def __init__(self, manager, *, headless=False):
        self.manager = manager
        self.headless = headless
        self.lock = threading.RLock()
        self.entries = {}

    def busy(self, ref):
        with self.lock:
            item = self.entries.get(ref)
            return bool(item and item["state"] in ("opening", "human_login", "closing"))

    def begin(self, ref, url):
        path = self.manager.profile_dir(ref)
        with self.lock:
            if self.busy(ref):
                return {"state": self.entries[ref]["state"]}
            described = self.manager.describe(ref)
            if described and described["state"] == "busy":
                raise ValueError("浏览器正忙，请稍后再打开登录窗口")
            self.manager.release(ref)
            item = {"state": "opening", "done": threading.Event(), "closed": threading.Event()}
            self.entries[ref] = item

        def run():
            try:
                from playwright.sync_api import sync_playwright

                os.makedirs(path, mode=0o700, exist_ok=True)
                with sync_playwright() as pw:
                    context = pw.chromium.launch_persistent_context(path, headless=self.headless, viewport=VIEWPORT, locale="zh-CN")
                    try:
                        page = context.pages[0] if context.pages else context.new_page()
                        page.goto(url, wait_until="domcontentloaded", timeout=20000)
                        item["state"] = "human_login"
                        # Poll Playwright, so closing the human window also completes the session.
                        for _ in range(1200):
                            if item["done"].is_set() or not context.pages:
                                break
                            page.wait_for_timeout(500)
                        save_session_cookies(context, path.rstrip(os.sep) + ".session-cookies.json")
                    finally:
                        context.close()
                item["state"] = "closed"
            except Exception:
                item["state"] = "login_window_failed"
            finally:
                item["closed"].set()

        threading.Thread(target=run, name=f"human-login-{ref}", daemon=True).start()
        return {"state": "opening"}

    def finish(self, ref):
        with self.lock:
            item = self.entries.get(ref)
        if not item:
            return {"state": "closed"}
        if item["state"] in ("closed", "login_window_failed"):
            return {"state": item["state"]}
        item["state"] = "closing"
        item["done"].set()
        item["closed"].wait(timeout=5)
        return {"state": item["state"]}

    def close_all(self):
        for ref in list(self.entries):
            self.finish(ref)
