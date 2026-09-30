"""Clock and ID adapters. FakeClock/SequentialIds make tests and replays deterministic."""
import itertools
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional


class SystemClock:
    def utc_now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    def __init__(self, start: Optional[datetime] = None) -> None:
        self._now = start or datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)
        self._mono = 0.0
        self._lock = threading.Lock()

    def utc_now(self) -> datetime:
        with self._lock:
            return self._now

    def monotonic(self) -> float:
        with self._lock:
            return self._mono

    def advance(self, seconds: float = 0, **kw: float) -> datetime:
        delta = timedelta(seconds=seconds, **kw)
        with self._lock:
            self._now += delta
            self._mono += delta.total_seconds()
            return self._now

    def set(self, when: datetime) -> None:
        with self._lock:
            self._now = when


class UuidIds:
    def new(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"


class SequentialIds:
    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    def new(self, prefix: str) -> str:
        with self._lock:
            return f"{prefix}_{next(self._counter):06d}"
