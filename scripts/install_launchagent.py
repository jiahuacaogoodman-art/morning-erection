#!/usr/bin/env python3
"""Install (or uninstall) Morning Pulse as a macOS LaunchAgent.

Usage:
  uv run python scripts/install_launchagent.py          # install & load
  uv run python scripts/install_launchagent.py --uninstall  # unload & remove
  uv run python scripts/install_launchagent.py --status     # show current state

The service starts at login, restarts on crash, and keeps its state at
~/.pulse so Accessibility/Screen Recording permissions granted once stay
permanently — no more reconnecting on every restart.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

LABEL = "art.goodman.morning.pulse"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / "Morning"

# Resolve paths relative to this script's location (the repo root).
REPO = Path(__file__).resolve().parent.parent
# Use a stable shell launcher as the executable so macOS Accessibility permission
# only needs to be granted once — even when the underlying Python binary changes
# after a uv update or new cpython version.
LAUNCHER = REPO / "scripts" / "pulse-launcher.sh"
PYTHON = REPO / ".venv" / "bin" / "python"
SCRIPT = REPO / "scripts" / "pulse.py"
STATE = Path.home() / ".pulse"


def plist_content(port: int) -> str:
    log = LOG_DIR / "pulse.log"
    err = LOG_DIR / "pulse-error.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{PYTHON}</string>
        <string>{SCRIPT}</string>
        <string>--port</string>
        <string>{port}</string>
        <string>--state</string>
        <string>{STATE}</string>
    </array>

    <!-- Start at login; restart within 10 s if the process exits. -->
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>

    <!-- Write stdout + stderr to ~/Library/Logs/Morning/ -->
    <key>StandardOutPath</key>
    <string>{log}</string>
    <key>StandardErrorPath</key>
    <string>{err}</string>

    <!-- Inherit the user's PATH so `node` and other tools resolve correctly. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{Path.home()}/.local/node/bin:/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:{REPO}/.venv/bin</string>
        <key>HOME</key>
        <string>{Path.home()}</string>
        <key>PYTHONPATH</key>
        <string>{REPO}/packages/wakecore/src:{REPO}/packages/wakecore-ui-runtime/src</string>
    </dict>
</dict>
</plist>
"""


def run(cmd: list[str], check=True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def install(port: int) -> None:
    if not PYTHON.exists():
        sys.exit(f"Error: virtualenv not found at {PYTHON}\nRun `uv sync` first.")
    if not SCRIPT.exists():
        sys.exit(f"Error: {SCRIPT} not found.")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Write the plist.
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(plist_content(port))
    print(f"Wrote {PLIST_PATH}")

    # Unload any stale copy first (ignore errors — it may not be loaded yet).
    run(["launchctl", "unload", str(PLIST_PATH)], check=False)

    result = run(["launchctl", "load", "-w", str(PLIST_PATH)], check=False)
    if result.returncode != 0:
        sys.exit(f"launchctl load failed:\n{result.stderr}")

    print(f"Service loaded.  Pulse is now running at http://127.0.0.1:{port}")
    print(f"Logs: {LOG_DIR}/pulse.log")
    print(f"To view logs: tail -f '{LOG_DIR}/pulse.log'")


def uninstall() -> None:
    if not PLIST_PATH.exists():
        print("LaunchAgent plist not found — nothing to remove.")
        return
    run(["launchctl", "unload", "-w", str(PLIST_PATH)], check=False)
    PLIST_PATH.unlink()
    print(f"Removed {PLIST_PATH} and unloaded the service.")


def status() -> None:
    result = run(["launchctl", "list", LABEL], check=False)
    if result.returncode != 0:
        print("Service is NOT loaded.")
    else:
        print("Service is loaded:")
        print(result.stdout)
    if PLIST_PATH.exists():
        print(f"Plist: {PLIST_PATH}")
    else:
        print("Plist: not installed")
    log = LOG_DIR / "pulse.log"
    if log.exists():
        print(f"\nLast 10 log lines ({log}):")
        lines = log.read_text().splitlines()
        print("\n".join(lines[-10:]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Morning Pulse LaunchAgent installer")
    parser.add_argument("--uninstall", action="store_true", help="Unload and remove the LaunchAgent")
    parser.add_argument("--status", action="store_true", help="Show service status and recent logs")
    parser.add_argument("--port", type=int, default=8788, help="Port for Pulse (default: 8788)")
    args = parser.parse_args()

    if sys.platform != "darwin":
        sys.exit("LaunchAgents are macOS-only.")

    if args.uninstall:
        uninstall()
    elif args.status:
        status()
    else:
        install(args.port)


if __name__ == "__main__":
    main()
