"""Entry-point plugins: explicit allowlist, a bad plugin is isolated, built-ins cannot be replaced.

Distributions are simulated with a `.dist-info` directory on sys.path (what an installed wheel
leaves behind), so these tests exercise importlib.metadata for real without pip. Building and
installing the example as a wheel is checked separately in the release verification.
"""
import dataclasses
import json
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from wakecore import plugins
from wakecore.adapters.clock import FakeClock
from wakecore.app.bootstrap import build
from wakecore.app.cli.main import main as cli
from wakecore.kernel.ports.action import AuthorizedAction

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "custom_connector" / "src"

BAD = dedent('''
    from wakecore.adapters.tools.inbox import InboxTool

    def raises(ctx):
        raise RuntimeError("cannot reach the CRM")

    def not_an_adapter(ctx):
        return object()

    def steals_builtin(ctx):
        return InboxTool(ctx.store)      # tool_id "inbox.notify" already belongs to the kernel

    def seen_ctx(ctx):
        seen_ctx.last = ctx
        raise RuntimeError("stop")
''')


def _dist(root: Path, name: str, entry_points: dict[str, dict[str, str]]) -> None:
    info = root / f"{name}-0.1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.1.0\n")
    lines = []
    for group, eps in entry_points.items():
        lines.append(f"[{group}]")
        lines += [f"{k} = {v}" for k, v in eps.items()]
    (info / "entry_points.txt").write_text("\n".join(lines) + "\n")


@pytest.fixture
def installed(tmp_path, monkeypatch):
    site = tmp_path / "site"
    site.mkdir()
    (site / "wk_bad_plugins.py").write_text(BAD)
    _dist(site, "wakecore_example_plugin", {
        plugins.CONNECTORS: {"example_json_file": "wakecore_example_plugin:make_connector"},
        plugins.TOOLS: {"example_journal": "wakecore_example_plugin:make_tool"}})
    _dist(site, "wk_bad_plugins", {
        plugins.TOOLS: {"bad_import": "wk_no_such_module:make", "bad_raises": "wk_bad_plugins:raises",
                        "bad_type": "wk_bad_plugins:not_an_adapter", "bad_steal": "wk_bad_plugins:steals_builtin",
                        "bad_ctx": "wk_bad_plugins:seen_ctx"},
        plugins.CONNECTORS: {"bad_connector_type": "wk_bad_plugins:not_an_adapter"}})
    monkeypatch.syspath_prepend(str(EXAMPLE))
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.delenv(plugins.ENV, raising=False)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("WAKECORE_EXAMPLE_ROOT", str(data))
    yield data
    for m in ("wakecore_example_plugin", "wk_bad_plugins"):
        sys.modules.pop(m, None)


def _load(names, **kw):
    return plugins.load(clock=FakeClock(), store=None, allowed=names, **kw)


def test_allowlist_parsing(monkeypatch):
    monkeypatch.setenv(plugins.ENV, " a, b ,,c ")
    assert plugins.allowlist() == {"a", "b", "c"}
    assert plugins.allowlist(["x", " ", ""]) == {"x"}
    monkeypatch.setenv(plugins.ENV, "*")
    assert plugins.allowlist() == {"*"}          # no wildcard semantics: "*" is just a name


def test_discover_lists_without_importing(installed):
    found = {(p.group, p.name): p for p in plugins.discover(["example_journal"])}
    assert found[(plugins.TOOLS, "example_journal")].enabled is True
    assert found[(plugins.CONNECTORS, "example_json_file")].enabled is False
    assert found[(plugins.TOOLS, "example_journal")].distribution == "wakecore_example_plugin"
    assert "wakecore_example_plugin" not in sys.modules and "wk_bad_plugins" not in sys.modules


def test_nothing_is_loaded_without_an_allowlist(installed):
    res = _load(None)
    assert (res.connectors, res.tools, res.errors, res.missing) == ({}, {}, {}, [])
    assert "wakecore_example_plugin" not in sys.modules


def test_only_allowlisted_plugins_load(installed):
    res = _load(["example_journal"])
    assert set(res.tools) == {"example.journal"} and res.connectors == {}
    assert res.loaded == ["wakecore.tools:example_journal -> example.journal"]
    assert not res.errors


def test_env_allowlist(installed, monkeypatch):
    monkeypatch.setenv(plugins.ENV, "example_json_file,example_journal")
    res = _load(None)
    assert set(res.connectors) == {"example.json_file"} and set(res.tools) == {"example.journal"}


def test_bad_plugins_are_isolated(installed, caplog):
    names = ["bad_import", "bad_raises", "bad_type", "bad_connector_type", "example_journal"]
    with caplog.at_level("WARNING", logger="wakecore.plugins"):
        res = _load(names)
    assert set(res.tools) == {"example.journal"}
    assert res.errors["bad_import"].startswith("ModuleNotFoundError")
    assert res.errors["bad_raises"] == "RuntimeError: cannot reach the CRM"
    assert "ToolDescriptor" in res.errors["bad_type"]
    assert "ConnectorDescriptor" in res.errors["bad_connector_type"]
    assert sum("skipped" in r.message for r in caplog.records) == 4


def test_a_plugin_cannot_replace_a_builtin_or_another_plugin(installed):
    res = _load(["bad_steal"], taken_tools=["inbox.notify"])
    assert res.tools == {} and "already registered" in res.errors["bad_steal"]


def test_missing_names_are_reported(installed):
    res = _load(["example_journal", "not_installed"])
    assert res.missing == ["not_installed"]


def test_factory_gets_only_its_own_config(installed):
    res = _load(["bad_ctx"], config={"bad_ctx": {"k": 1}, "other": {"secret": "x"}}, ui_runtime="client")
    ctx = sys.modules["wk_bad_plugins"].seen_ctx.last
    assert ctx.name == "bad_ctx" and ctx.config == {"k": 1} and ctx.ui_runtime == "client"
    assert "bad_ctx" in res.errors


def test_build_registers_plugins_next_to_builtins(installed, tmp_path):
    k = build(db_url=f"sqlite:///{tmp_path / 'wk.db'}", clock=FakeClock(), with_model=False,
              plugins=["example_json_file", "example_journal", "bad_steal"])
    try:
        assert {"example.journal", "inbox.notify", "email.send"} <= set(k.ctx.tools)
        assert {"example.json_file", "offline.grades"} <= set(k.ctx.connectors)
        assert type(k.ctx.tools["inbox.notify"]).__module__ == "wakecore.adapters.tools.inbox"
        assert "bad_steal" in k.plugins.errors
        # loaded is not authorised: the default SystemPolicy does not allow the plugin's capability
        assert "files.append" not in k.ctx.system_policy.allowed_capabilities
    finally:
        k.ctx.store.close()


def test_example_journal_writes_once_per_effect_key(installed):
    tool = _load(["example_journal"]).tools["example.journal"]
    clock = FakeClock()

    def act(attempt):
        from datetime import timedelta
        return AuthorizedAction(tenant_id="t", action_id="a", attempt_id=attempt, effect_key="ek", task_id="task",
                                tool_id="example.journal", tool_version="0.1.0",
                                payload={"file": "log.txt", "line": "hi"}, payload_digest="p", revocation_epoch=0,
                                permit="x", secret=None, resource_scope={}, data_egress=(),
                                deadline_at=clock.utc_now() + timedelta(seconds=5))

    first, second = tool.execute(act("1")), tool.execute(act("2"))
    assert first.status == second.status == "confirmed" and first.provider_ref == second.provider_ref
    assert (installed / "log.txt").read_text() == "ek\thi\n"
    escape = tool.execute(dataclasses.replace(act("3"), effect_key="ek2",
                                              payload={"file": "../outside.txt", "line": "x"}))
    assert escape.status == "failed_no_effect" and not (installed.parent / "outside.txt").exists()


def test_cli_plugins_list(installed, monkeypatch, capsys):
    monkeypatch.setenv(plugins.ENV, "example_journal,ghost")
    assert cli(["plugins", "list"]) == 0
    out = json.loads(capsys.readouterr().out)
    rows = {p["name"]: p for p in out["plugins"]}
    assert rows["example_journal"]["enabled"] is True and rows["example_json_file"]["enabled"] is False
    assert rows["example_journal"]["entry_point"] == "wakecore_example_plugin:make_tool"
    assert out["allowlisted_but_missing"] == ["ghost"] and out["allowlist_env"] == plugins.ENV
