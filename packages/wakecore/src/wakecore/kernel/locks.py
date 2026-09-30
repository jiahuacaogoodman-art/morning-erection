"""Lock ordering for task aggregates (RFC §9.3, §12.2).

Every transaction that touches more than one task locks the root runtime first and then
the task runtime, so cancel (T7) and dispatch (T5) serialise on the root row and cannot
deadlock against each other.
"""
from contextlib import contextmanager
from typing import Iterator

from .domain.errors import NotFound
from .domain.model import TaskRuntime
from .ports.store import StateStore
from .repo import Repo


@contextmanager
def tx(store: StateStore) -> Iterator[Repo]:
    with store.transaction() as uow:
        yield Repo(uow)


def lock_task(repo: Repo, tenant_id: str, task_id: str) -> tuple[TaskRuntime, TaskRuntime]:
    """Returns (root_runtime, task_runtime), both row-locked, root first."""
    peek = repo.get(TaskRuntime, tenant_id=tenant_id, task_id=task_id)
    if peek is None:
        raise NotFound(f"task {task_id}")
    if peek.root_task_id != task_id:
        root = repo.get(TaskRuntime, for_update=True, tenant_id=tenant_id, task_id=peek.root_task_id)
        if root is None:
            raise NotFound(f"root task {peek.root_task_id}")
    else:
        root = None
    rt = repo.get(TaskRuntime, for_update=True, tenant_id=tenant_id, task_id=task_id)
    assert rt is not None
    return (root or rt), rt
