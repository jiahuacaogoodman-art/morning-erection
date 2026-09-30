"""Seeded randomized fault schedule: publish order, crash points, restarts, duplicate and
out-of-order events, pause/resume and clock jumps are all random. After the system settles,
the RFC invariants must hold for every seed. A failing seed is reproducible:
    WAKECORE_FUZZ_SEEDS=1 WAKECORE_FUZZ_FIRST=<seed> pytest tests/recovery/test_randomized_faults.py
"""
import os
import random

import pytest

from harness import TENANT, USER, base_spec
from wakecore.kernel.domain.enums import ActionStatus, TaskLifecycle
from wakecore.kernel.domain.errors import KernelError
from wakecore.kernel.ports.faults import SimulatedCrash
from wakecore.kernel.replay import replay_task
from wakecore.kernel.service import KernelService, Principal

COURSES = {f"C{i}": {"name": f"课程{i}", "score": None} for i in range(4)}
POINTS = ["after_fetch", "before_commit_T4", "after_commit_T4", "after_dispatch_permit", "after_tool_execute"]
FIRST = int(os.environ.get("WAKECORE_FUZZ_FIRST", "1"))
SEEDS = range(FIRST, FIRST + int(os.environ.get("WAKECORE_FUZZ_SEEDS", "30")))
ME = Principal(TENANT, USER)


def spec():
    return base_spec(source={"resource_scope": {"semester": "fall_demo", "course_ids": sorted(COURSES)}})


def step(h, fn):
    """Run fn; a simulated crash kills the process, which then restarts."""
    try:
        fn()
    except SimulatedCrash:
        h.restart()
    except KernelError:
        pass   # e.g. pausing an already-paused task: rejected, state unchanged


@pytest.mark.parametrize("seed", SEEDS)
def test_random_fault_schedule_preserves_invariants(make_harness, seed):
    rnd = random.Random(seed)
    h = make_harness(courses=COURSES)
    h.standard(spec())
    h.run()
    unpublished = sorted(COURSES)
    rnd.shuffle(unpublished)
    paused = False
    log = []
    for _ in range(rnd.randint(8, 25)):
        op = rnd.choice(["publish", "cycle", "crash", "event", "pause", "resume", "jump"])
        log.append(op)
        if op == "publish" and unpublished:
            h.k.grades.set_score(unpublished.pop(), rnd.randint(60, 99))
        elif op == "cycle":
            step(h, lambda: h.cycle(rnd.choice([30, 60, 300, 900])))
        elif op == "crash":
            h.faults.arm(rnd.choice(POINTS), times=1)
            step(h, lambda: h.cycle(300))
        elif op == "event":
            body = h.envelope(f"evt-{rnd.randint(1, 4)}", data={"schema_version": 1, "revision": rnd.randint(1, 3)})
            step(h, lambda: KernelService(h.ctx).ingest("school_account_demo", body, h.signature(body)))
        elif op == "pause" and not paused:
            step(h, lambda: KernelService(h.ctx).pause_task(ME, "grades_demo"))
            paused = h.runtime().lifecycle is TaskLifecycle.PAUSED
        elif op == "resume" and paused:
            step(h, lambda: KernelService(h.ctx).resume_task(ME, "grades_demo"))
            paused = h.runtime().lifecycle is TaskLifecycle.PAUSED
        elif op == "jump":
            h.advance(rnd.choice([3600, 6 * 3600]))
    # settle: no more faults, resume, publish the rest, let leases expire and recovery run
    h.restart()   # a clean process: armed-but-unfired faults are gone
    if h.runtime().lifecycle is TaskLifecycle.PAUSED:
        KernelService(h.ctx).resume_task(ME, "grades_demo")
    for c in unpublished:
        h.k.grades.set_score(c, 77)
    for _ in range(12):
        h.cycle(120)

    ctx = f"seed={seed} ops={log}"
    rt = h.runtime()
    assert rt.lifecycle is TaskLifecycle.COMPLETED, ctx
    # 1. every course was announced exactly once (no loss, no duplicate)
    announced = []
    for m in h.inbox():
        announced += [c["resource"] for c in m.data.get("changes", [])]
    assert sorted(announced) == sorted(COURSES), ctx
    # 2. at most one confirmed effect per effect key; nothing left in an unknown or in-flight state
    confirmed = [a.effect_key for a in h.actions() if a.status is ActionStatus.CONFIRMED]
    assert len(confirmed) == len(set(confirmed)), ctx
    assert not [a for a in h.actions() if a.status in (ActionStatus.UNKNOWN, ActionStatus.DISPATCHING)], ctx
    # 3. the inbox tool never delivered more often than messages exist (reconciled, not resent)
    assert h.k.inbox.executions == len(h.inbox()), ctx
    # 4. history replays deterministically
    assert not replay_task(h.store, TENANT, "grades_demo").mismatches, ctx
