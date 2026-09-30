"""Business-field change detection (RFC §7). Pure functions, no I/O.

- Only business fields are compared: `ignore_fields` are stripped first.
- A->B->A yields two transitions: comparison is always against the last *trusted*
  snapshot, never against a permanent set of content hashes.
- "Not read" is never interpreted as "deleted": partial merges only touch the
  resources that were actually read.
"""
from typing import Any, Iterable, Mapping, Optional


def business_view(record: Mapping[str, Any], ignore_fields: Iterable[str]) -> dict[str, Any]:
    ignore = set(ignore_fields)
    return {k: v for k, v in record.items() if k not in ignore}


def normalise(records: Mapping[str, Mapping[str, Any]], ignore_fields: Iterable[str]) -> dict[str, dict[str, Any]]:
    fields = tuple(ignore_fields)
    return {key: business_view(rec, fields) for key, rec in records.items()}


def merge_declared(
    trusted: Mapping[str, dict[str, Any]],
    fresh: Mapping[str, dict[str, Any]],
    read_scope: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Merge a partial snapshot: resources inside `read_scope` take the fresh value
    (absent-in-fresh means confirmed absent); everything else keeps its trusted value."""
    merged = dict(trusted)
    for key in read_scope:
        if key in fresh:
            merged[key] = fresh[key]
        else:
            merged.pop(key, None)
    return merged


def diff(
    old: Mapping[str, dict[str, Any]],
    new: Mapping[str, dict[str, Any]],
    keys: Optional[Iterable[str]] = None,
) -> list[dict[str, Any]]:
    candidates = sorted(set(keys) if keys is not None else set(old) | set(new))
    changes = []
    for key in candidates:
        before, after = old.get(key), new.get(key)
        if before != after:
            changes.append({"resource": key, "old": before, "new": after})
    return changes
