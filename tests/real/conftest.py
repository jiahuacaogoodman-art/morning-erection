"""Real-environment runs: real public websites, real Chromium, and (when a key is present) the
real OpenAI Computer Use model. Opt-in only; nothing here is mocked except the kernel's own
planner, which is scripted so the run is about the eye and the hand.

    WAKECORE_REAL=1 uv run pytest -q -s tests/real

The API key lives in `.secrets/openai.key` (chmod 600). Only the sidecar process reads it via
`--openai-key-file`; the tests check its size, never its contents. Without a key the scenarios
that need the model are skipped. Model: $WAKECORE_REAL_MODEL, else the runtime default;
variant: $WAKECORE_REAL_VARIANT (ga | preview | function; function for relays that refuse the
hosted computer tool).

OpenAI-compatible endpoint (relay, proxy, gateway): put its base URL, e.g.
`https://relay.example.com/v1`, on one line in `.secrets/openai.base_url` (or set
$WAKECORE_REAL_BASE_URL). Empty or missing means api.openai.com. The kernel side then uses the
egress the sidecar reports (`model:openai-compatible:<host>`) in its grant, task and approval.
R0 checks the endpoint first (two tiny calls) so a relay without the computer tool or without
previous_response_id fails fast with a reason.

Every scenario appends to reports/real/<run>/report.{json,md} (outcomes, model steps, tokens,
artifact paths), so a run on a real site leaves evidence behind.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[2]
KEY_FILE = ROOT / ".secrets" / "openai.key"
BASE_URL_FILE = ROOT / ".secrets" / "openai.base_url"

if importlib.util.find_spec("playwright") is None or os.environ.get("WAKECORE_REAL") != "1":
    collect_ignore_glob = ["test_*.py"]
else:
    from harness import TENANT, USER, Harness, approve, base_spec
    from wakecore.adapters.sources.playwright_web import PlaywrightWebSource
    from wakecore.adapters.tools.browser_cua import MODEL_EGRESS, BrowserCuaTool
    from wakecore.adapters.ui_runtime.client import UiRuntimeClient
    from wakecore.kernel.commands import setup as setup_cmd
    from wakecore.kernel.context import SystemPolicy
    from wakecore.kernel.domain.model import ActionAttempt, ActionRecord, Approval, SourceBinding
    from wakecore.kernel.locks import tx
    from wakecore.kernel.ports.reasoning import PlanProposal, ProposedAction, Usage
    from wakecore_ui_runtime.login import interactive_login, release


def has_key() -> bool:
    return KEY_FILE.is_file() and KEY_FILE.stat().st_size > 0


def base_url() -> Optional[str]:
    """The model endpoint, or None for the runtime default (api.openai.com)."""
    if os.environ.get("WAKECORE_REAL_BASE_URL"):
        return os.environ["WAKECORE_REAL_BASE_URL"].strip()
    if BASE_URL_FILE.is_file():
        return BASE_URL_FILE.read_text(encoding="utf-8").strip() or None
    return None


def model_args() -> list[str]:
    """Endpoint, model and variant flags shared by the sidecar and the connectivity check."""
    args = ["--openai-base-url", base_url()] if base_url() else []
    if os.environ.get("WAKECORE_REAL_MODEL"):
        args += ["--model", os.environ["WAKECORE_REAL_MODEL"]]
    if os.environ.get("WAKECORE_REAL_VARIANT"):
        args += ["--variant", os.environ["WAKECORE_REAL_VARIANT"]]
    return args


def sidecar_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if k not in ("OPENAI_API_KEY", "OPENAI_API_KEY_FILE", "OPENAI_BASE_URL")}


needs_key = pytest.mark.skipif(not has_key(), reason=".secrets/openai.key is empty")


class RealSidecar:
    def __init__(self, state: Path, *, with_key: bool) -> None:
        self.state, self.with_key = state, with_key
        self.proc: Optional[subprocess.Popen] = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "RealSidecar":
        env = sidecar_env()
        args = [sys.executable, "-m", "wakecore_ui_runtime.server", "--state", str(self.state), "--port", str(self.port)]
        if self.with_key:
            args += ["--openai-key-file", str(KEY_FILE)]
        args += model_args()
        self.log_path = self.state.parent / "sidecar.log"
        self._log = open(self.log_path, "a")
        self.proc = subprocess.Popen(args, cwd=ROOT, env=env, stdout=self._log, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 30
        while time.time() < deadline:
            text = self.log_path.read_text()
            if "LISTENING" in text:
                self.port = int(text.rsplit("LISTENING", 1)[1].split()[0])
                return self
            if self.proc.poll() is not None:
                raise RuntimeError(f"sidecar exited: {text[-2000:]}")
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
            self.proc = None


class Report:
    def __init__(self) -> None:
        self.dir = ROOT / "reports" / "real" / datetime.now().strftime("%Y%m%d-%H%M%S")
        self.entries: list[dict[str, Any]] = []

    def add(self, entry: dict[str, Any]) -> None:
        self.entries.append(entry)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "report.json").write_text(json.dumps(self.entries, ensure_ascii=False, indent=2, default=str))
        lines = ["# WakeCore 真实环境测试报告", ""]
        for e in self.entries:
            lines.append(f"## {e['scenario']}：{e.get('verdict', '?')}")
            for k, v in e.items():
                if k not in ("scenario", "verdict"):
                    lines.append(f"- **{k}**: `{json.dumps(v, ensure_ascii=False, default=str)[:600]}`")
            lines.append("")
        (self.dir / "report.md").write_text("\n".join(lines))


@pytest.fixture(scope="session")
def report():
    return Report()


class RealWorld:
    """One kernel (SQLite dev store, FakeClock) + one real sidecar + one browser session."""

    SOURCE = "web_src"

    def __init__(self, tmp: Path, *, origin: str, session: str, with_key: bool) -> None:
        self.tmp, self.origin, self.session = tmp, origin, session
        tmp.mkdir(parents=True, exist_ok=True)
        self.state = tmp / "ui-state"
        self.state.mkdir(mode=0o700, exist_ok=True)
        self.side = RealSidecar(self.state, with_key=with_key).start()
        self.h = Harness(str(tmp / "wk.db"), courses={})
        self.client = UiRuntimeClient(self.side.url, timeout=900)
        self.egress = self.client.health().get("model_egress") or MODEL_EGRESS   # who receives the screenshots
        self.web = PlaywrightWebSource(self.client)
        self.tool = BrowserCuaTool(self.client, self.h.clock, settle_seconds=120, max_steps=12,
                                   model_egress=self.egress)
        self.h.ctx.connectors[self.web.descriptor.connector_id] = self.web
        self.h.ctx.tools[self.tool.descriptor.tool_id] = self.tool
        base = SystemPolicy()
        self.h.ctx.system_policy = SystemPolicy(
            allowed_capabilities=base.allowed_capabilities | {"web.observe", "browser.operate"},
            allowed_data_egress=base.allowed_data_egress | {self.egress})
        self.h.k.secrets.put(TENANT, "sec_web", session)

    def close(self) -> None:
        self.side.stop()
        self.h.store.close()

    # ------------------------------------------------------------------ the human, in their own browser
    def as_user(self, url: str, fn: Any, *, done_url_contains: str = "") -> str:
        """The user opens the same profile (sidecar lets go of it first) and does something by hand."""
        if self.side.proc is not None:
            release(self.side.url, self.session)
        return interactive_login(str(self.state), self.session, url, human=fn, headless=True, timeout_s=60,
                                 done_url_contains=done_url_contains or url.split("://", 1)[1].split("/", 1)[0],
                                 login_markers=())

    # ------------------------------------------------------------------ kernel setup
    def setup(self, *, task_id: str, url: str, extractor: dict, record_ids: list[str], conditions: list[dict],
              complete_when: list[dict], watch_fields: list[str], on_met: str = "plan",
              first_snapshot: str = "baseline_only", scope_extra: Optional[dict] = None,
              max_run_seconds: int = 300, purpose: str = "") -> None:
        h = self.h
        self.task_id = task_id
        with tx(h.store) as repo:
            setup_cmd.register_grant(repo, h.ctx, tenant_id=TENANT, principal=USER, grant_ref="grant_confirmed_by_user",
                                     capabilities=["web.observe", "inbox.notify_self", "browser.operate"],
                                     data_egress=["model:offline-scripted", self.egress],
                                     resource_scope={"origins": [self.origin]})
        with tx(h.store) as repo:
            setup_cmd.register_source_binding(
                repo, h.ctx, tenant_id=TENANT, owner=USER, source_ref=self.SOURCE, connector_id="web.playwright",
                source_uri=url, resource_scope={"origins": [self.origin]}, capabilities=["web.observe"],
                secret_ref="sec_web", ingress_secret_ref="sec_web")
        h.create(base_spec(
            task_id=task_id, root_task_id=task_id, purpose=purpose or task_id,
            source={"binding_ref": self.SOURCE, "resource_scope": {
                "origins": [self.origin], "url": url, "extractor": extractor, "record_ids": record_ids,
                **(scope_extra or {})}},
            observation={"completeness_required": "full_target_scope", "first_snapshot": first_snapshot,
                         "ignore_fields": [], "target_key": "record_ids"},
            completion={"evaluator": "all_targets_match", "evaluator_version": 1, "required_outputs": []},
            authority={"read_capabilities": ["web.observe"], "write_capabilities": ["browser.operate"],
                       "notify_capabilities": ["inbox.notify_self"],
                       "model_data_egress": ["model:offline-scripted", self.egress]},
            decision={"profile": "generic_watch.v1", "params": {
                "conditions": conditions, "watch_fields": watch_fields, "on_met": on_met,
                "complete_when": complete_when}},
            limits={"max_run_seconds": max_run_seconds}))

    def script_plan(self, *, goal: str, start_url: str, record: str, conditions: list[dict]) -> None:
        self.h.k.model.script_plan(PlanProposal(steps=(ProposedAction(
            logical_step_id="do", tool_id="browser.cua", capability="browser.operate",
            payload={"goal": goal, "start_url": start_url, "verify": {"record_id": record, "conditions": conditions}},
            reason_code="condition_met"),), usage=Usage(1, 1, 1), model_ref="offline-scripted"))

    # ------------------------------------------------------------------ inspection
    def binding(self) -> "SourceBinding":
        return self.h.all(SourceBinding, {"source_ref": self.SOURCE})[0]

    def browser_actions(self) -> list["ActionRecord"]:
        return [a for a in self.h.actions() if a.tool_id == "browser.cua"]

    def current(self, action: "ActionRecord") -> "ActionRecord":
        return self.h.all(ActionRecord, {"action_id": action.action_id})[0]

    def approve(self, action: "ActionRecord") -> None:
        approve(self.h, self.h.all(Approval, {"action_id": action.action_id})[-1])

    def attempts(self, action: "ActionRecord") -> list["ActionAttempt"]:
        return self.h.all(ActionAttempt, {"action_id": action.action_id})

    def act_summary(self, action: "ActionRecord") -> dict[str, Any]:
        out = []
        for a in self.attempts(action):
            r = a.receipt or {}
            out.append({"error": a.error, "act": (r.get("receipt") or r).get("act"),
                        "verified": (r.get("receipt") or r).get("verified")})
        return {"status": self.current(action).status.value, "attempts": out}

    def artifacts_dir(self) -> str:
        return str(self.state / "artifacts")


@pytest.fixture
def make_real(tmp_path):
    made: list = []

    def make(**kw: Any) -> RealWorld:
        w = RealWorld(tmp_path / f"r{len(made)}", **kw)
        made.append(w)
        return w

    yield make
    for w in made:
        w.close()
