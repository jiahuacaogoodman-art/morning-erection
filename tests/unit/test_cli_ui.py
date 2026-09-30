"""`wakecore ui check-extractor` works offline (schema only) and never imports the runtime."""
import json
import subprocess
import sys

import pytest

from wakecore import __version__
from wakecore.app.cli.main import main

GOOD = {"table": {"label": "课程"}, "key_field": "code", "columns": [{"field": "code", "headers": ["课程号"]}]}


def test_offline_check(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("WAKECORE_UI_RUNTIME_URL", raising=False)
    f = tmp_path / "x.json"
    f.write_text(json.dumps(GOOD))
    assert main(["ui", "check-extractor", str(f)]) == 0
    assert json.loads(capsys.readouterr().out)["checked_by"] == "schema"
    f.write_text(json.dumps({"resource_scope_like": 1, "extractor": {**GOOD, "columns": []}}))
    assert main(["ui", "check-extractor", str(f)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "$.columns" in out["error"]["message"]


def test_runtime_commands_need_a_url(monkeypatch, capsys):
    monkeypatch.delenv("WAKECORE_UI_RUNTIME_URL", raising=False)
    assert main(["ui", "health"]) == 2


def test_the_kernel_cli_does_not_pull_in_the_runtime(tmp_path):
    f = tmp_path / "x.json"
    f.write_text(json.dumps(GOOD))
    code = ("import sys; from wakecore.app.cli.main import main; main(['ui','check-extractor',%r]); "
            "bad=[m for m in sys.modules if m.split('.')[0] in ('wakecore_ui_runtime','playwright','psycopg')]; "
            "print(bad); sys.exit(1 if bad else 0)") % str(f)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={"PATH": ""})
    assert r.returncode == 0, r.stdout + r.stderr


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0 and capsys.readouterr().out.strip() == f"wakecore {__version__}"
