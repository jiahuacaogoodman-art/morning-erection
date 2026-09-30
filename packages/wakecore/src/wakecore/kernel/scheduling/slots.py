"""Interval slot arithmetic (RFC §6.3). All times are UTC."""
from datetime import datetime, timedelta


def latest_slot(due_at: datetime, every_seconds: int, now: datetime) -> tuple[datetime, int]:
    """Latest slot <= now on the grid due_at + k*every, and how many slots it skips over."""
    if now < due_at:
        return due_at, 0
    missed = int((now - due_at).total_seconds() // every_seconds)
    return due_at + timedelta(seconds=missed * every_seconds), missed


def next_slot(due_at: datetime, every_seconds: int, now: datetime) -> datetime:
    slot, _ = latest_slot(due_at, every_seconds, now)
    return slot + timedelta(seconds=every_seconds) if slot <= now else slot
