"""Helpers for the real desktop runs (see conftest.py)."""
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[2]
KEY_FILE = ROOT / ".secrets" / "openai.key"
BASE_URL_FILE = ROOT / ".secrets" / "openai.base_url"


def has_key() -> bool:
    return KEY_FILE.is_file() and KEY_FILE.stat().st_size > 0


def base_url() -> Optional[str]:
    if os.environ.get("WAKECORE_REAL_BASE_URL"):
        return os.environ["WAKECORE_REAL_BASE_URL"].strip()
    if BASE_URL_FILE.is_file():
        return BASE_URL_FILE.read_text(encoding="utf-8").strip() or None
    return None


def mcp_command() -> str:
    if os.environ.get("WAKECORE_DESKTOP_MCP_COMMAND"):
        return os.environ["WAKECORE_DESKTOP_MCP_COMMAND"]
    node = shutil.which("node") or os.path.expanduser("~/.local/node/bin/node")
    return f"{node} ~/src/codex-computer-use-mcp/dist/mcp-server.js"


class RealDesktopRuntime:
    """wakecore-desktop-runtime as a real subprocess over the real bridge."""

    def __init__(self, state: Path, *, with_key: bool, token: str = "real-desktop") -> None:
        self.state, self.with_key, self.token = state, with_key, token
        self.proc: Optional[subprocess.Popen[str]] = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "RealDesktopRuntime":
        env = {k: v for k, v in os.environ.items()
               if k not in ("OPENAI_API_KEY", "OPENAI_API_KEY_FILE", "OPENAI_BASE_URL")}
        args = [sys.executable, "-m", "wakecore_ui_runtime.desktop.server", "--state", str(self.state),
                "--mcp-command", mcp_command(), "--token", self.token]
        if self.with_key:
            args += ["--openai-key-file", str(KEY_FILE)]
        if base_url():
            args += ["--openai-base-url", base_url()]
        if os.environ.get("WAKECORE_REAL_MODEL"):
            args += ["--model", os.environ["WAKECORE_REAL_MODEL"]]
        self.log_path = self.state.parent / "desktop-runtime.log"
        self._log = open(self.log_path, "a")
        self.proc = subprocess.Popen(args, cwd=ROOT, env=env, stdout=self._log, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 30
        while time.time() < deadline:
            text = self.log_path.read_text()
            if "LISTENING" in text:
                self.port = int(text.rsplit("LISTENING", 1)[1].split()[0])
                return self
            if self.proc.poll() is not None:
                raise RuntimeError(f"desktop runtime exited: {text[-2000:]}")
            time.sleep(0.05)
        raise RuntimeError("desktop runtime did not start")

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
        (self.dir / "desktop.json").write_text(json.dumps(self.entries, ensure_ascii=False, indent=2, default=str))
        lines = ["# WakeCore 真机桌面测试报告", ""]
        for e in self.entries:
            lines.append(f"## {e['scenario']}：{e.get('verdict', '?')}")
            for k, v in e.items():
                if k not in ("scenario", "verdict"):
                    lines.append(f"- **{k}**: `{json.dumps(v, ensure_ascii=False, default=str)[:600]}`")
            lines.append("")
        (self.dir / "desktop.md").write_text("\n".join(lines))
