"""Fault-injection seam for recovery tests (RFC §18: T05, T06, T09). No-op in production."""


class SimulatedCrash(Exception):
    """Represents process death at a named point; tests restart the kernel afterwards."""


class NoFaults:
    def check(self, point: str) -> None:
        return None


class FaultPlan:
    def __init__(self) -> None:
        self._armed: dict[str, int] = {}
        self.hits: list[str] = []

    def arm(self, point: str, times: int = 1) -> "FaultPlan":
        self._armed[point] = times
        return self

    def check(self, point: str) -> None:
        remaining = self._armed.get(point, 0)
        if remaining > 0:
            self._armed[point] = remaining - 1
            self.hits.append(point)
            raise SimulatedCrash(point)
