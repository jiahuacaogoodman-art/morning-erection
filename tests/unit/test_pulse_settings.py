"""App-wide Pulse preferences: validation, persistence and the loopback routes."""
import io
import json
import stat
from datetime import datetime, timezone

import pytest

from wakecore.adapters.clock import FakeClock
from wakecore.app.bootstrap import build
from wakecore.app.pulse.server import LocalPulseApp
from wakecore.app.pulse.settings import DEFAULTS, SettingsError, SettingsStore


def test_defaults_and_validation():
    store = SettingsStore()
    assert store.all() == DEFAULTS
    with pytest.raises(SettingsError):
        store.update({"theme": "neon"})
    with pytest.raises(SettingsError):
        store.update({"notify_sound": 1})
    with pytest.raises(SettingsError):
        store.update({"polling_min_seconds": True})
    with pytest.raises(SettingsError):
        store.update({"polling_min_seconds": 1})
    with pytest.raises(SettingsError):
        store.update({"polling_min_seconds": 60, "polling_max_seconds": 30})
    with pytest.raises(SettingsError, match="无法识别"):
        store.update({"api_key": "x"})
    # A rejected update leaves nothing half-applied.
    assert store.all() == DEFAULTS


def test_persists_privately_and_ignores_unknown_stored_keys(tmp_path):
    path = tmp_path / "state" / "settings.json"
    SettingsStore(path).update({"theme": "dark", "polling_min_seconds": 30, "polling_max_seconds": 600})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    stored = json.loads(path.read_text())
    stored["retired_setting"] = True
    path.write_text(json.dumps(stored))
    value = SettingsStore(path).all()
    assert value["theme"] == "dark" and value["polling_min_seconds"] == 30
    assert "retired_setting" not in value


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    assert SettingsStore(path).all() == DEFAULTS


def call(app, method, path, body=None, cookie=""):
    data = json.dumps(body).encode() if body is not None else b""
    env = {"REQUEST_METHOD": method, "PATH_INFO": "/api" + path, "CONTENT_LENGTH": str(len(data)),
           "wsgi.input": io.BytesIO(data), "HTTP_HOST": "127.0.0.1:8788", "HTTP_COOKIE": cookie,
           "HTTP_ORIGIN": "http://127.0.0.1:8788", "HTTP_X_PULSE_CLIENT": "1", "HTTP_IDEMPOTENCY_KEY": "k"}
    got = {}

    def start(status, headers):
        got["status"], got["headers"] = int(status.split()[0]), dict(headers)
    value = json.loads(b"".join(app(env, start)))
    return got["status"], value, got["headers"]


def test_settings_routes(tmp_path):
    kernel = build(db_url=f"sqlite:///{tmp_path / 'kernel.db'}", with_model=False,
                   clock=FakeClock(datetime(2026, 10, 1, tzinfo=timezone.utc)))
    try:
        app = LocalPulseApp(kernel, state_dir=tmp_path)
        assert call(app, "GET", "/pulse/settings")[0] == 401
        cookie = call(app, "GET", "/bootstrap")[2]["Set-Cookie"].split(";", 1)[0]
        status, value, _ = call(app, "GET", "/pulse/settings", cookie=cookie)
        assert status == 200 and value["theme"] == "system"
        assert value["_data_dir"] == str(tmp_path) and value["_model_configured"] is False
        status, value, _ = call(app, "POST", "/pulse/settings", {"theme": "dark", "polling_min_seconds": 20}, cookie)
        assert status == 200 and value["theme"] == "dark" and value["polling_min_seconds"] == 20
        assert app.api.polling_defaults() == {"min_seconds": 20, "max_seconds": 120}
        status, value, _ = call(app, "POST", "/pulse/settings", {"theme": "neon"}, cookie)
        assert status >= 400 and "theme" in value["error"]["message"]
        # Read-only fields are reported but can never be written back.
        assert call(app, "POST", "/pulse/settings", {"_data_dir": "/tmp"}, cookie)[0] >= 400
        assert LocalPulseApp(kernel, state_dir=tmp_path).api.settings.all()["theme"] == "dark"
    finally:
        kernel.ctx.store.close()
