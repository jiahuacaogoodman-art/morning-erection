"""Real-browser scenario fixtures (V0.3 P3).

Every scenario runs the real stack except the two things we cannot have here:
  * the website is `wakecore_ui_runtime.testing.jw_portal` (a local 教务 portal with CSRF + TOTP login), and
  * the model is `wakecore_ui_runtime.testing.fake_openai` (speaks the Responses computer-use protocol).
Chromium, Playwright, the UI Runtime sidecar process, its journal and persistent profile,
and the WakeCore kernel (SQLite dev store, FakeClock) are all real.

Run with:  uv run pytest -q tests/ui   (needs `uv run playwright install chromium`)
Without Playwright installed these tests are not collected.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

import pytest

from wakecore.adapters.tools.browser_cua import MODEL_EGRESS  # noqa: F401  (re-exported for tests)

# The fake model is an OpenAI-compatible endpoint on loopback, so that is who receives the screenshots.
FAKE_EGRESS = "model:openai-compatible:127.0.0.1"

if importlib.util.find_spec("playwright") is None:
    collect_ignore_glob = ["test_*.py"]
else:
    from harness import TENANT, USER, Harness, approve, base_spec
    from wakecore.adapters.sources.playwright_web import PlaywrightWebSource
    from wakecore.adapters.tools.browser_cua import BrowserCuaTool
    from wakecore.adapters.ui_runtime.client import UiRuntimeClient
    from wakecore.kernel.commands import setup as setup_cmd
    from wakecore.kernel.context import SystemPolicy
    from wakecore.kernel.domain.model import ActionRecord, Approval, SourceBinding
    from wakecore.kernel.locks import tx
    from wakecore.kernel.ports.reasoning import PlanProposal, ProposedAction, Usage
    from wakecore_ui_runtime.login import interactive_login, release
    from wakecore_ui_runtime.testing.fake_openai import FakeOpenAI
    from wakecore_ui_runtime.testing.jw_portal import EvilSite, JwPortal
    from wakecore_ui_runtime.testing.totp import totp

ROOT = Path(__file__).resolve().parents[2]
SESSION = "jw_alice"
PASSWORD, TOTP_SECRET = "correct-horse-battery", "JBSWY3DPEHPK3PXP"

EXTRACTOR = {
    "table": {"label": "可选课程"}, "key_attr": "data-course-id",
    "columns": [
        {"field": "name", "headers": ["课程名", "课程名称"]},
        {"field": "teacher", "headers": ["教师", "任课教师"]},
        {"field": "seats", "headers": ["余量", "剩余名额"], "type": "int"},
        {"field": "status", "headers": ["状态", "开课状态"]},
        {"field": "enrolled", "headers": ["我的状态", "选课情况"], "type": "bool", "true": ["已选"], "false": ["未选"]},
    ],
    "login": {"url_contains": ["/login", "/mfa"]},
}
SEATS_OPEN = {"field": "seats", "op": "gt", "value": 0, "kind": "slot_opened"}
ENROLLED = {"field": "enrolled", "op": "eq", "value": True}


def human_login(page: Any) -> None:
    """What the user does by hand in the headed login window (scripted here)."""
    page.fill("#username", "alice")
    page.fill("#password", PASSWORD)
    page.click("#login")
    page.wait_for_url("**/mfa")
    page.fill("#otp", totp(TOTP_SECRET))
    page.click("#verify")


class Sidecar:
    """The UI Runtime as a separate OS process, restartable on the same port and state."""

    def __init__(self, state: Path, fake: Any, *, with_key: bool = True, variant: str = "ga", token: str = "") -> None:
        self.state, self.fake, self.with_key, self.variant, self.token = state, fake, with_key, variant, token
        self.port = 0
        self.proc: Optional[subprocess.Popen] = None
        self.starts = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "Sidecar":
        self.starts += 1
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        if self.with_key:
            env["OPENAI_API_KEY"] = self.fake.api_key
        log = self.state.parent / f"sidecar-{self.starts}.log"
        args = [sys.executable, "-m", "wakecore_ui_runtime.server", "--state", str(self.state), "--port", str(self.port),
                "--openai-base-url", self.fake.base_url, "--model", "computer-use-test", "--allow-faults",
                "--variant", self.variant]
        if self.token:
            args += ["--token", self.token]
        self._log = open(log, "w")
        self.proc = subprocess.Popen(args, cwd=ROOT, env=env, stdout=self._log, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 30
        while time.time() < deadline:
            text = log.read_text()
            if "LISTENING" in text:
                self.port = int(text.split("LISTENING", 1)[1].split()[0])
                return self
            if self.proc.poll() is not None:
                raise RuntimeError(f"sidecar exited: {text}")
            time.sleep(0.05)
        raise RuntimeError("sidecar did not start")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self.proc:
            self._log.close()

    def dead(self) -> bool:
        return self.proc is not None and self.proc.poll() is not None

    def wait_dead(self, timeout: float = 15) -> int:
        return self.proc.wait(timeout=timeout)

    def restart(self) -> "Sidecar":
        self.stop()
        time.sleep(0.3)   # let Chromium release the profile lock
        return self.start()

    def faults(self, **kw: Any) -> None:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        urllib.request.urlopen(urllib.request.Request(self.url + "/__faults", data=json.dumps(kw).encode(),
                                                      method="POST", headers=headers), timeout=10).read()


class World:
    def __init__(self, tmp: Path, *, with_key: bool = True, variant: str = "ga", token: str = "",
                 policy: str = "oracle", stateless: bool = False) -> None:
        self.tmp = tmp
        tmp.mkdir(parents=True, exist_ok=True)
        self.evil = EvilSite().start()
        self.portal = JwPortal().start()
        self.portal.set(evil_origin=self.evil.origin)
        self.fake = FakeOpenAI(self.portal, policy=policy, variant=variant, stateless=stateless).start()
        self.state = tmp / "ui-state"
        self.login()
        self.side = Sidecar(self.state, self.fake, with_key=with_key, variant=variant, token=token).start()
        self.h = Harness(str(tmp / "wk.db"), courses={})
        self.attach()

    @property
    def origin(self) -> str:
        return self.portal.origin

    def login(self) -> dict[str, Any]:
        """Manual login + MFA into the persistent profile. Only the sidecar state dir sees it."""
        return interactive_login(str(self.state), SESSION, self.origin + "/courses", done_url_contains="/courses",
                                 human=human_login, headless=True, timeout_s=30)

    def relogin(self) -> None:
        release(self.side.url, SESSION, self.side.token)
        self.login()

    def attach(self) -> None:
        """Wire the browser adapters into the (possibly restarted) kernel, like bootstrap does."""
        self.client = UiRuntimeClient(self.side.url, token=self.side.token)
        self.web = PlaywrightWebSource(self.client)
        self.tool = BrowserCuaTool(self.client, self.h.clock, settle_seconds=120,
                                   model_egress=getattr(self, "egress", FAKE_EGRESS))
        self.h.ctx.connectors[self.web.descriptor.connector_id] = self.web
        self.h.ctx.tools[self.tool.descriptor.tool_id] = self.tool
        base = SystemPolicy()
        self.h.ctx.system_policy = SystemPolicy(
            allowed_capabilities=base.allowed_capabilities | {"web.observe", "browser.operate"},
            allowed_data_egress=base.allowed_data_egress | {self.tool.model_egress})
        self.h.k.secrets.put(TENANT, "sec_jw", SESSION)   # the binding's secret is only a session_ref

    def restart_kernel(self) -> None:
        self.h.restart()
        self.attach()

    def close(self) -> None:
        self.side.stop()
        self.fake.stop()
        self.portal.stop()
        self.evil.stop()
        self.h.store.close()

    # ------------------------------------------------------------------ task setup
    def spec(self, *, record_ids=("PHARM",), egress=("model:offline-scripted", FAKE_EGRESS),
             max_run_seconds: int = 180, **over: Any) -> dict:
        return base_spec(
            task_id="enroll_watch", root_task_id="enroll_watch",
            purpose="在教务选课页插眼：药理学有余量后帮我选上，确认已选后结束",
            source={"binding_ref": "jw_portal", "resource_scope": {
                "origins": [self.origin], "url": self.origin + "/courses", "extractor": EXTRACTOR,
                "record_ids": list(record_ids)}},
            observation={"completeness_required": "full_target_scope", "first_snapshot": "baseline_only",
                         "ignore_fields": [], "target_key": "record_ids"},
            completion={"evaluator": "all_targets_match", "evaluator_version": 1, "required_outputs": []},
            authority={"read_capabilities": ["web.observe"], "write_capabilities": ["browser.operate"],
                       "notify_capabilities": ["inbox.notify_self"], "model_data_egress": list(egress)},
            decision={"profile": "generic_watch.v1", "params": {
                "conditions": [SEATS_OPEN], "watch_fields": ["seats", "status"], "label_field": "name",
                "on_met": "plan", "complete_when": [ENROLLED]}},
            limits={"max_run_seconds": max_run_seconds},
            **over)

    def setup(self, *, grant_egress=("model:offline-scripted", FAKE_EGRESS), **spec_kw: Any) -> None:
        h = self.h
        with tx(h.store) as repo:
            setup_cmd.register_grant(repo, h.ctx, tenant_id=TENANT, principal=USER, grant_ref="grant_confirmed_by_user",
                                     capabilities=["web.observe", "inbox.notify_self", "browser.operate"],
                                     data_egress=list(grant_egress), resource_scope={"origins": [self.origin]})
        with tx(h.store) as repo:
            setup_cmd.register_source_binding(
                repo, h.ctx, tenant_id=TENANT, owner=USER, source_ref="jw_portal", connector_id="web.playwright",
                source_uri=self.origin + "/courses", resource_scope={"origins": [self.origin]},
                capabilities=["web.observe"], secret_ref="sec_jw", ingress_secret_ref="sec_jw")
        h.create(self.spec(**spec_kw))
        h.run()   # first observation: baseline only

    def reauthorised(self) -> None:
        with tx(self.h.store) as repo:
            setup_cmd.mark_source_reauthorised(repo, self.h.ctx, tenant_id=TENANT, owner=USER, source_ref="jw_portal")

    # ------------------------------------------------------------------ the scripted planner
    def payload(self, *, record: str = "PHARM", start_url: Optional[str] = None, goal: Optional[str] = None,
                verify_url: Optional[str] = None) -> dict:
        verify: dict[str, Any] = {"record_id": record, "conditions": [ENROLLED]}
        if verify_url:
            verify["url"] = verify_url
        return {"goal": goal or f"在选课页面选上课程 {record}（药理学），并在确认页提交",
                "start_url": start_url or self.origin + "/courses", "verify": verify}

    def script_plan(self, **kw: Any) -> None:
        self.h.k.model.script_plan(PlanProposal(steps=(ProposedAction(
            logical_step_id="enroll", tool_id="browser.cua", capability="browser.operate",
            payload=self.payload(**kw), reason_code="slot_opened"),), usage=Usage(1, 1, 1),
            model_ref="offline-scripted"))

    def open_seat(self, n: int = 1, **plan_kw: Any) -> "ActionRecord":
        self.script_plan(**plan_kw)
        self.portal.set(seats={"PHARM": n})
        self.h.cycle()
        return self.browser_action()

    def browser_action(self) -> "ActionRecord":
        return [a for a in self.h.actions() if a.tool_id == "browser.cua"][-1]

    def approve(self, action: "ActionRecord") -> None:
        approve(self.h, self.h.all(Approval, {"action_id": action.action_id})[-1])

    def current(self, action: "ActionRecord") -> "ActionRecord":
        return self.h.all(ActionRecord, {"action_id": action.action_id})[0]

    def binding(self) -> "SourceBinding":
        return self.h.all(SourceBinding, {"source_ref": "jw_portal"})[0]

    def confirm_posts(self) -> int:
        return len(self.portal.snapshot()["confirm_posts"])

    def session_tokens(self) -> list[str]:
        with self.portal.lock:
            return list(self.portal.state.sessions)

    def model_saw_secret(self) -> list[str]:
        needles = [PASSWORD, TOTP_SECRET, "JWSESSION", *self.session_tokens()]
        return [n for n in needles for r in self.fake.requests if n in r]


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


@pytest.fixture
def make_world(tmp_path):
    made: list = []

    def make(**kw: Any) -> World:
        w = World(tmp_path / f"w{len(made)}", **kw)
        made.append(w)
        return w

    yield make
    for w in made:
        w.close()
