"""Typed access to the storage port: domain records <-> logical rows.

This keeps kernel services free of stringly-typed dictionaries while leaving SQL,
locking and physical types to adapters.
"""
import dataclasses
import functools
import typing
from enum import Enum
from typing import Any, Mapping, Optional, Sequence, TypeVar

from .domain.errors import VersionConflict
from .ports.store import Cond, UnitOfWork
from .ports.tables import table_for

R = TypeVar("R")


@functools.cache
def _enum_fields(cls: type) -> dict[str, type]:
    hints = typing.get_type_hints(cls)
    out = {}
    for name, tp in hints.items():
        if typing.get_origin(tp) is typing.Union:
            tp = next(a for a in typing.get_args(tp) if a is not type(None))
        if isinstance(tp, type) and issubclass(tp, Enum):
            out[name] = tp
    return out


def to_row(obj: Any) -> dict[str, Any]:
    return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}


def from_row(cls: type[R], row: Mapping[str, Any]) -> R:
    enums = _enum_fields(cls)
    kwargs = {}
    for f in dataclasses.fields(cls):
        value = row[f.name]
        if value is not None and f.name in enums:
            value = enums[f.name](value)
        kwargs[f.name] = value
    return cls(**kwargs)


def pk_of(obj: Any) -> dict[str, Any]:
    t = table_for(type(obj))
    return {k: getattr(obj, k) for k in t.pk}


class Repo:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    def insert(self, obj: Any) -> None:
        self.uow.insert(table_for(type(obj)).name, to_row(obj))

    def insert_if_absent(self, obj: Any) -> bool:
        return self.uow.insert_if_absent(table_for(type(obj)).name, to_row(obj))

    def get(self, cls: type[R], *, for_update: bool = False, **key: Any) -> Optional[R]:
        row = self.uow.get(table_for(cls).name, key, for_update=for_update)
        return from_row(cls, row) if row is not None else None

    def find(
        self,
        cls: type[R],
        where: Optional[Mapping[str, Any]] = None,
        *,
        conds: Sequence[Cond] = (),
        order_by: Sequence[str] = (),
        limit: Optional[int] = None,
        for_update: bool = False,
        skip_locked: bool = False,
    ) -> list[R]:
        rows = self.uow.find(table_for(cls).name, where, conds=conds, order_by=order_by, limit=limit,
                             for_update=for_update, skip_locked=skip_locked)
        return [from_row(cls, r) for r in rows]

    def count(self, cls: type, where: Optional[Mapping[str, Any]] = None, *, conds: Sequence[Cond] = ()) -> int:
        return self.uow.count(table_for(cls).name, where, conds=conds)

    def change(self, obj: R, *, expect: Optional[Mapping[str, Any]] = None, **changes: Any) -> R:
        """CAS-update `obj` by primary key. Raises VersionConflict if `expect` did not hold."""
        t = table_for(type(obj))
        n = self.uow.update(t.name, pk_of(obj), changes, expect=expect)
        if n != 1:
            raise VersionConflict(f"{t.name} {pk_of(obj)} changed concurrently", details={"expect": dict(expect or {})})
        return dataclasses.replace(obj, **changes)

    def try_change(self, obj: R, *, expect: Mapping[str, Any], **changes: Any) -> Optional[R]:
        t = table_for(type(obj))
        n = self.uow.update(t.name, pk_of(obj), changes, expect=expect)
        return dataclasses.replace(obj, **changes) if n == 1 else None

    def delete(self, obj: Any, *, expect: Optional[Mapping[str, Any]] = None) -> int:
        return self.uow.delete(table_for(type(obj)).name, pk_of(obj), expect=expect)
