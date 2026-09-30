"""Deterministic rule set for the grades domain (rules-v1, RFC §8.1).

Change kinds:
  critical     -> handled by TEMPLATE_ACTION; a model can never downgrade it.
  protected    -> may go to JUDGE, but the outcome can never be lower than HUMAN_REVIEW.
  ambiguous    -> needs semantic classification (JUDGE) or human review.
  informational-> recorded as a transition, never wakes a model.
"""
from typing import Any, Optional

RULES_VERSION = "rules-v1"

CRITICAL = frozenset({"grade_published", "grade_changed"})
PROTECTED = frozenset({"grade_withdrawn"})
INFORMATIONAL = frozenset({"record_added"})


def _score(rec: Optional[dict[str, Any]]) -> Any:
    return None if rec is None else rec.get("score")


def classify_change(change: dict[str, Any]) -> str:
    old, new = change.get("old"), change.get("new")
    before, after = _score(old), _score(new)
    if before is None and after is not None:
        return "grade_published"
    if before is not None and after is not None and before != after:
        return "grade_changed"
    if before is not None and after is None:
        return "grade_withdrawn"
    if old is None and new is not None:
        return "record_added"
    return "record_changed"


def all_expected_published(snapshot: dict[str, dict[str, Any]], targets: tuple[str, ...]) -> bool:
    if not targets:
        return False
    return all(_score(snapshot.get(t)) is not None for t in targets)


# Completion evaluators are versioned and explicit; unknown ones never complete a task.
COMPLETION_EVALUATORS = {
    ("all_expected_courses_published", 1): all_expected_published,
}
