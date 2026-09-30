"""Real macOS desktop runs: the real Calculator, the real Computer Use MCP bridge
(tmustier/codex-computer-use-mcp) and, when a key is present, a real model. Opt-in only:

    WAKECORE_REAL=1 WAKECORE_REAL_DESKTOP=1 uv run pytest -q -s tests/real_desktop

Needs macOS with Accessibility / Screen Recording granted to the bridge, Node, and the bridge
built at $WAKECORE_DESKTOP_MCP_COMMAND (default: node ~/src/codex-computer-use-mcp/dist/mcp-server.js).
Only Calculator is touched; no other app is read or operated.

The key and the endpoint are the same as for tests/real: `.secrets/openai.key` (read only by the
runtime process via --openai-key-file; the tests check its size, never its contents) and
`.secrets/openai.base_url` or $WAKECORE_REAL_BASE_URL. Evidence goes to
reports/real/<run>/desktop.{json,md}.
"""
import os
import sys

import pytest

if sys.platform != "darwin" or os.environ.get("WAKECORE_REAL") != "1" or \
        os.environ.get("WAKECORE_REAL_DESKTOP") != "1":
    collect_ignore_glob = ["test_*.py"]


from real_desktop_env import Report  # noqa: E402


@pytest.fixture(scope="session")
def report():
    return Report()
