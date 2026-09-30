"""Deterministic scripted reasoning adapter (tests, demos, replay).

It never sees anything the kernel did not put in the ContextBundle and can be scripted
to time out, return garbage, or attempt privilege escalation, so kernel admission can be
tested without a real model (RFC §8, T12, T16).
"""
import threading
from collections import deque
from typing import Any, Callable, Union

from wakecore.kernel.domain.enums import Route
from wakecore.kernel.ports.reasoning import (
    ContextBundle,
    DecisionProposal,
    ModelTimeout,
    PlanProposal,
    Usage,
)

Scripted = Union[DecisionProposal, PlanProposal, Exception, Callable[[ContextBundle], Any]]


class ScriptedModel:
    model_ref = "offline-scripted"
    data_egress_target = "model:offline-scripted"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.classify_script: deque[Scripted] = deque()
        self.plan_script: deque[Scripted] = deque()
        self.calls: list[tuple[str, ContextBundle]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def script_classify(self, *items: Scripted) -> "ScriptedModel":
        self.classify_script.extend(items)
        return self

    def script_plan(self, *items: Scripted) -> "ScriptedModel":
        self.plan_script.extend(items)
        return self

    def _next(self, kind: str, queue: deque, bundle: ContextBundle, default: Any) -> Any:
        with self._lock:
            self.calls.append((kind, bundle))
            item = queue.popleft() if queue else default
        if isinstance(item, Exception):
            raise item
        if callable(item) and not isinstance(item, (DecisionProposal, PlanProposal)):
            return item(bundle)
        return item

    def classify(self, context: ContextBundle) -> DecisionProposal:
        return self._next("classify", self.classify_script, context, DecisionProposal(
            route=Route.HUMAN_REVIEW, reason_code="scripted_default", usage=Usage(10, 5, 1),
            model_ref=self.model_ref))

    def plan(self, context: ContextBundle) -> PlanProposal:
        return self._next("plan", self.plan_script, context, PlanProposal(steps=(), usage=Usage(10, 5, 1),
                                                                            model_ref=self.model_ref))


def timeout() -> ModelTimeout:
    return ModelTimeout("scripted timeout after request was sent")
