"""App-wide preferences for the Pulse UI.

Distinct from per-watch settings, which live in the task spec's resource_scope and are
dispatched through `pulse.settings`. This is the user's own configuration: how the app looks,
how loud it is, and what cadence new watches start with. It is deliberately a flat, validated
key/value store rather than a domain model, because none of it is part of the kernel's
correctness story — nothing here may influence what a task is allowed to do.
"""
import json
import threading
from pathlib import Path

# Every key is declared with its default and a validator. Unknown keys are rejected rather than
# stored, so a typo in a request can never silently become a setting that nothing reads.
THEMES = ("light", "dark", "system")

DEFAULTS = {
    "theme": "system",
    "locale": "zh-CN",
    "notify_on_change": True,
    "notify_on_approval": True,
    "notify_sound": False,
    "polling_min_seconds": 10,
    "polling_max_seconds": 120,
    "orb_hue": None,
}


class SettingsError(ValueError):
    pass


def _bool(value, key):
    if not isinstance(value, bool):
        raise SettingsError(f"{key} 应该是开关")
    return value


def _int_range(low, high):
    def check(value, key):
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsError(f"{key} 应该是整数")
        if not low <= value <= high:
            raise SettingsError(f"{key} 应在 {low} 到 {high} 之间")
        return value
    return check


def _choice(options):
    def check(value, key):
        if value not in options:
            raise SettingsError(f"{key} 只能是 {'、'.join(options)}")
        return value
    return check


def _hue(value, key):
    # None means "follow the state colour". A number pins the orb to one hue for people who
    # want their own colour regardless of what the agent is doing.
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError(f"{key} 应该是色相角度")
    return int(value) % 360


VALIDATORS = {
    "theme": _choice(THEMES),
    "locale": _choice(("zh-CN", "en")),
    "notify_on_change": _bool,
    "notify_on_approval": _bool,
    "notify_sound": _bool,
    "polling_min_seconds": _int_range(2, 3600),
    "polling_max_seconds": _int_range(2, 86400),
    "orb_hue": _hue,
}


class SettingsStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        self.value = dict(DEFAULTS)
        if self.path and self.path.exists():
            try:
                stored = json.loads(self.path.read_text())
            except (OSError, ValueError):
                stored = None
            # Read only keys that still exist, so an upgrade that drops a setting does not
            # resurrect it, and a hand-edited file cannot smuggle in an unvalidated value.
            if isinstance(stored, dict):
                for key in DEFAULTS:
                    if key in stored:
                        self.value[key] = stored[key]

    def all(self):
        with self.lock:
            return dict(self.value)

    def update(self, body):
        if not isinstance(body, dict):
            raise SettingsError("设置应该是一个对象")
        unknown = sorted(set(body) - set(VALIDATORS))
        if unknown:
            raise SettingsError(f"无法识别的设置项：{'、'.join(unknown)}")
        with self.lock:
            candidate = dict(self.value)
            for key, raw in body.items():
                candidate[key] = VALIDATORS[key](raw, key)
            if candidate["polling_max_seconds"] < candidate["polling_min_seconds"]:
                raise SettingsError("最长间隔应该不小于最短间隔")
            self.value = candidate
            self._write(candidate)
            return dict(candidate)

    def _write(self, value):
        if not self.path:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        temp.chmod(0o600)
        temp.replace(self.path)
