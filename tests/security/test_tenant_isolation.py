"""T20: tenants and principals are isolated. Another tenant's id behaves exactly like an id
that does not exist (NotFound), so ids cannot be probed either."""
import dataclasses

import pytest

from harness import TENANT, USER, base_spec, email_waiting_approval
from wakecore.kernel.domain.enums import ApprovalDecision
from wakecore.kernel.domain.errors import IdentityConflict, NotFound, PolicyDenied
from wakecore.kernel.domain.model import Approval, InboxMessage, ObservationRecord, Run, SourceBinding
from wakecore.kernel.locks import tx
from wakecore.kernel.ports.store import DuplicateKey
from wakecore.kernel.service import KernelService, Principal

BOB_TENANT, BOB = "tenant_b", "user:bob"
ALICE = Principal(TENANT, USER)
BOB_P = Principal(BOB_TENANT, BOB)
CAROL = Principal(TENANT, "user:carol")      # same tenant, different principal


@pytest.fixture
def two(h):
    h.standard()
    h.k.grades.set_score("PHARM", 88)
    h.run()
    h.k.secrets.put(BOB_TENANT, "sec_bob", "bob-cookie")
    h.grant(tenant=BOB_TENANT, principal=BOB)
    h.binding(tenant=BOB_TENANT, owner=BOB, source_ref="bob_source", uri="urn:wakecore:source:bob",
              secret_ref="sec_bob")
    h.create(base_spec(source={"binding_ref": "bob_source"}), tenant=BOB_TENANT, principal=BOB)  # same task_id
    h.run()
    return h


def alice_ids(h):
    run = h.all(Run, {"tenant_id": TENANT})[0]
    obs = [o for o in h.all(ObservationRecord, {"tenant_id": TENANT}) if o.evidence_ref][0]
    return run.run_id, obs.evidence_ref


@pytest.mark.parametrize("who", [BOB_P, CAROL], ids=["other_tenant", "other_principal"])
def test_t20_reads_of_foreign_resources_are_not_found(two, who):
    svc = KernelService(two.ctx)
    run_id, ev = alice_ids(two)
    if who is BOB_P:   # Bob has his own task with the same id; he must see his, not Alice's
        assert svc.get_task(who, "grades_demo")["source"]["source_ref"] == "bob_source"
    else:
        with pytest.raises(NotFound):
            svc.get_task(who, "grades_demo")
    with pytest.raises(NotFound):
        svc.get_run(who, run_id)
    with pytest.raises(NotFound):
        svc.get_evidence(who, ev)
    assert [t["task_id"] for t in svc.list_tasks(who)] == (["grades_demo"] if who is BOB_P else [])
    if who is BOB_P:
        assert svc.get_task(who, "grades_demo")["version"] == two.runtime(tenant=BOB_TENANT).version


def test_t20_same_task_id_in_two_tenants_is_two_tasks(two):
    # The offline grades fake serves the same data to every binding, so both tenants get one
    # notice; what matters is that each is keyed, stored and counted under its own tenant.
    for tenant in (TENANT, BOB_TENANT):
        [msg] = two.all(InboxMessage, {"tenant_id": tenant})
        assert msg.effect_key.startswith(f"{tenant}:grades_demo:")
    assert two.runtime(tenant=BOB_TENANT).source_ref == "bob_source"
    assert two.runtime().source_ref == "school_account_demo"


@pytest.mark.parametrize("op", ["pause", "cancel"])
def test_t20_foreign_control_commands_are_rejected(two, op):
    two.create(base_spec(task_id="alice_only", root_task_id="alice_only"))
    svc = KernelService(two.ctx)
    with pytest.raises(NotFound):                      # other tenant: indistinguishable from absent
        getattr(svc, f"{op}_task")(BOB_P, "alice_only")
    with pytest.raises((PolicyDenied, NotFound)):      # same tenant, not the owner
        getattr(svc, f"{op}_task")(CAROL, "alice_only")
    assert two.runtime("alice_only").lifecycle.value == two.runtime().lifecycle.value == "ACTIVE"


def test_t20_foreign_principal_cannot_approve(h):
    _, approval = email_waiting_approval(h)
    svc = KernelService(h.ctx)
    with pytest.raises(NotFound):
        svc.approve(BOB_P, approval.approval_id, payload_digest=approval.payload_digest)
    with pytest.raises((PolicyDenied, NotFound)):
        svc.approve(CAROL, approval.approval_id, payload_digest=approval.payload_digest)
    h.run()
    assert h.k.email.send_calls == 0
    assert h.all(Approval, {"approval_id": approval.approval_id})[0].decision is ApprovalDecision.PENDING


def test_t20_secrets_are_tenant_scoped(two):
    assert two.k.secrets.resolve(TENANT, "sec_school") == "session-cookie-demo"
    assert two.k.secrets.resolve(BOB_TENANT, "sec_school") is None
    assert two.k.secrets.resolve(TENANT, "sec_bob") is None


def test_t20_task_cannot_use_another_tenants_binding(two):
    with pytest.raises(NotFound):
        two.create(base_spec(task_id="steal", root_task_id="steal"), tenant=BOB_TENANT, principal=BOB)


def test_t20_task_cannot_use_another_principals_binding(two):
    two.grant(principal=CAROL.subject, grant_ref="grant_carol")
    spec = base_spec(task_id="carol_task", root_task_id="carol_task", authority={"grant_ref": "grant_carol"})
    with pytest.raises(PolicyDenied):
        two.create(spec, principal=CAROL.subject)


def test_t20_ingress_address_cannot_be_claimed_by_another_tenant(two):
    with pytest.raises(IdentityConflict):
        two.binding(tenant=BOB_TENANT, owner=BOB, source_ref="school_account_demo", secret_ref="sec_bob")
    # and the database enforces it even if setup code were bypassed (e.g. two racing registrations)
    [alice_binding] = two.all(SourceBinding, {"tenant_id": TENANT, "source_ref": "school_account_demo"})
    with pytest.raises(DuplicateKey):
        with tx(two.store) as repo:
            repo.insert(dataclasses.replace(alice_binding, tenant_id=BOB_TENANT, owner=BOB))
    # ingress still routes to Alice only
    body = two.envelope("evt-iso-1")
    out = KernelService(two.ctx).ingest("school_account_demo", body, two.signature(body))
    assert out.body["status"] == "accepted"
    two.k.grades.set_score("PATHOPHYS", 70)
    two.run()
    assert len(two.all(InboxMessage, {"tenant_id": TENANT})) == 2
    from wakecore.kernel.domain.model import Delivery, EventRecord
    assert [e.tenant_id for e in two.all(EventRecord, {"internal": False})] == [TENANT]
    assert {d.tenant_id for d in two.all(Delivery)} == {TENANT}
