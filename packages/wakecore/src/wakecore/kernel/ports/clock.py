"""Clock and identifier ports (RFC §3.2, §13.3). Time and IDs enter the kernel as recorded inputs."""
from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def utc_now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class IdGenerator(Protocol):
    def new(self, prefix: str) -> str: ...
