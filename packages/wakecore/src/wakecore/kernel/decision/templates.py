"""Model-free notification templates (TEMPLATE_ACTION / HUMAN_REVIEW / budget deferral)."""
from typing import Any


def _score(rec: Any) -> Any:
    return rec.get("score") if isinstance(rec, dict) else None


def _name(change: dict[str, Any]) -> str:
    rec = change.get("new") or change.get("old") or {}
    return str(rec.get("name") or change["resource"])


def grades_notification(changes: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    lines = []
    for c in changes:
        if c["kind"] == "grade_published":
            lines.append(f"{_name(c)}: 已出分 {_score(c['new'])}")
        else:
            lines.append(f"{_name(c)}: 成绩变更 {_score(c['old'])} -> {_score(c['new'])}")
    return {
        "recipient": "self",
        "title": "成绩更新",
        "body": "\n".join(lines),
        "data": {"changes": [{"resource": c["resource"], "kind": c["kind"], "old": _score(c["old"]),
                              "new": _score(c["new"])} for c in changes]},
    }


def review_notification(changes: tuple[dict[str, Any], ...], reason: str) -> dict[str, Any]:
    return {
        "recipient": "self",
        "title": "有变化需要确认",
        "body": f"检测到 {len(changes)} 项变化，需要人工确认（{reason}）。",
        "data": {"reason": reason, "resources": [c["resource"] for c in changes],
                 "kinds": [c.get("kind") for c in changes]},
    }


def budget_deferred_notification(count: int) -> dict[str, Any]:
    return {
        "recipient": "self",
        "title": "有待处理变化",
        "body": f"有 {count} 项待处理变化，但预算不足暂未分析。",
        "data": {"reason": "budget_deferred", "count": count},
    }
