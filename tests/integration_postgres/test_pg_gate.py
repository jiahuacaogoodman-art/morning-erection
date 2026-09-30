"""M2 gate on real PostgreSQL: the same guarantees as the SQLite contract tests, but with true
concurrent writers under READ COMMITTED + row locks + SKIP LOCKED."""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from harness import TENANT, USER, approve, base_spec, email_waiting_approval
from wakecore.app.worker.loop import run_until_idle
from wakecore.kernel.budget.ledger import AccountSpec, reserve
from wakecore.kernel.domain.enums import ActionStatus, DeliveryStatus, StepStatus
from wakecore.kernel.domain.errors import BudgetDeferred, IdentityConflict, KernelError
from wakecore.kernel.domain.model import (
    ActionAttempt,
    BudgetAccount,
    Delivery,
    EventRecord,
    IdempotencyRecord,
    SourceBinding,
)
from wakecore.kernel.locks import tx
from wakecore.kernel.ports.store import DuplicateKey
from wakecore.kernel.replay import replay_task
from wakecore.kernel.service import KernelService, Principal

pytestmark = pytest.mark.postgres
N = 8


def parallel(n, fn):
    barrier = threading.Barrier(n)

    def go(i):
        barrier.wait()
        try:
            return fn(i)
        except Exception as exc:  # noqa: BLE001 - collected for assertions
            return exc

    with ThreadPoolExecutor(n) as ex:
        return list(ex.map(go, range(n)))


def test_pg_role_is_not_superuser_or_bypassrls(ph):
    """RFC §9.4. Disposable/embedded test clusters often run as superuser: set
    WAKECORE_PG_ALLOW_SUPERUSER=1 there, and never in a production-like environment."""
    if os.environ.get("WAKECORE_PG_ALLOW_SUPERUSER") == "1":
        pytest.skip("superuser explicitly allowed for this disposable test cluster")
    with ph.store.transaction() as uow:
        row = uow._c.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
    assert row == (False, False)


def test_pg_end_to_end_flow_and_replay(ph):
    ph.standard()
    ph.run()
    ph.k.grades.set_score("PHARM", 88)
    ph.cycle()
    ph.cycle()
    ph.k.grades.set_score("PATHOPHYS", 91)
    ph.cycle()
    assert len(ph.inbox()) == 2
    assert str(ph.runtime().lifecycle) == "COMPLETED"
    s = replay_task(ph.store, TENANT, "grades_demo").summary()
    assert s["mismatches"] == [] and s["matches"] == s["replayed"] > 0


def test_pg_concurrent_workers_never_double_execute(ph):
    ph.grant()
    ph.binding()
    for i in range(6):
        ph.create(base_spec(task_id=f"t{i}", root_task_id=f"t{i}"))
    ph.run()
    ph.k.grades.set_score("PHARM", 88)
    ph.advance(300)
    parallel(N, lambda i: run_until_idle(ph.ctx, f"w{i}", max_rounds=200))
    ph.run()
    inbox = ph.inbox()
    assert len(inbox) == 6 and len({m.effect_key for m in inbox}) == 6
    assert ph.k.inbox.executions == 6
    for a in ph.actions():
        attempts = ph.all(ActionAttempt, {"action_id": a.action_id})
        assert a.status is ActionStatus.CONFIRMED and len(attempts) == 1
    # every logical step ran to a single terminal state
    assert all(s.status in (StepStatus.SUCCEEDED, StepStatus.CANCELLED, StepStatus.FAILED) for s in ph.steps())


def test_pg_concurrent_identical_ingress_is_accepted_once(ph):
    ph.standard()
    ph.run()
    svc = KernelService(ph.ctx)
    body = ph.envelope("evt-race")
    sig = ph.signature(body)
    out = parallel(N, lambda i: svc.ingest("school_account_demo", body, sig))
    assert not [o for o in out if isinstance(o, Exception)], out
    assert sorted(o.body["status"] for o in out).count("accepted") == 1
    assert len(ph.all(EventRecord, {"internal": False})) == 1
    ph.run()
    assert {d.status for d in ph.all(Delivery)} == {DeliveryStatus.PROCESSED}


def test_pg_budget_race_reserves_exactly_the_limit(ph):
    ph.standard()
    acct = [AccountSpec("root_task", "grades_demo", "model_attempts", "attempt", 3)]

    def go(i):
        with tx(ph.store) as repo:
            return reserve(repo, ph.ctx.ids, tenant_id=TENANT, accounts=acct, amount=1, root_task_id="grades_demo",
                           attempt_id=f"a{i}", period="2026-09-29")

    out = parallel(N, go)
    assert sum(1 for o in out if isinstance(o, BudgetDeferred)) == N - 3
    assert not [o for o in out if isinstance(o, Exception) and not isinstance(o, BudgetDeferred)], out
    [a] = ph.all(BudgetAccount, {"account_key": "root_task:grades_demo:model_attempts:2026-09-29"})
    assert a.reserved == 3


def test_pg_concurrent_idempotent_create(ph):
    ph.grant()
    ph.binding()
    svc = KernelService(ph.ctx)
    me = Principal(TENANT, USER)
    out = parallel(N, lambda i: svc.create_task(me, base_spec(), idem_key="same"))
    assert not [o for o in out if isinstance(o, Exception)], out
    assert len({json.dumps(o.body, sort_keys=True) for o in out}) == 1   # JSONB reorders keys
    assert len(ph.all(IdempotencyRecord)) == 1


def test_pg_concurrent_approvals_send_once(ph):
    action, approval = email_waiting_approval(ph)
    out = parallel(N, lambda i: approve(ph, approval))
    assert not [o for o in out if isinstance(o, Exception) and not isinstance(o, KernelError)], out
    parallel(4, lambda i: run_until_idle(ph.ctx, f"w{i}", max_rounds=100))
    assert ph.k.email.send_calls == 1


def test_pg_source_ref_is_globally_unique_under_race(ph):
    ph.k.secrets.put("tenant_b", "s", "x")

    def go(i):
        return ph.binding(tenant=f"tenant_{i}", owner=f"user:{i}", source_ref="contested")

    out = parallel(N, go)
    ok = [o for o in out if isinstance(o, SourceBinding)]
    assert len(ok) == 1
    assert all(isinstance(o, (IdentityConflict, DuplicateKey)) for o in out if o not in ok), out
    assert len(ph.all(SourceBinding, {"source_ref": "contested"})) == 1
