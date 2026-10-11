"""Cadence must not alter legacy tasks or repeatedly observe a blocked page."""
import pytest

from harness import TENANT, USER, base_spec
from wakecore.kernel.decision.profiles import get_profile
from wakecore.kernel.domain.errors import SchemaMismatch
from wakecore.kernel.domain.model import TriggerRecord
from wakecore.kernel.scheduling.adaptive import polling_policy
from wakecore.kernel.service import KernelService, Principal


def policy_spec():
    spec = base_spec(observation={"first_snapshot": "baseline_only"})
    spec["source"]["resource_scope"]["polling"] = {
        "min_seconds": 300, "max_seconds": 1200, "error_max_seconds": 2400, "jitter_percent": 0}
    return spec


def trigger(h):
    return h.all(TriggerRecord, {"tenant_id": TENANT, "trigger_id": "trg:grades_demo:main"})[0]


def test_quiet_backoff_change_reset_and_durable_restart(h):
    h.standard(policy_spec())
    h.run()
    assert trigger(h).every_seconds == 300
    h.cycle(300)
    assert trigger(h).every_seconds == 450
    due = trigger(h).due_at
    h.restart()
    assert trigger(h).due_at == due and trigger(h).every_seconds == 450
    h.k.grades.set_score("PHARM", 80)
    h.cycle(450)
    assert trigger(h).every_seconds == 300


@pytest.mark.parametrize("mode,health", [("login_page", "AUTH_REQUIRED"), ("schema_invalid", "SCHEMA_INVALID")])
def test_suspend_keeps_trusted_value_and_only_explicit_check_rearms(h, mode, health):
    h.standard(policy_spec())
    h.run()
    svc, me = KernelService(h.ctx), Principal(TENANT, USER)
    before = svc.pulse(me, "grades_demo")["records"]
    h.k.grades.set_mode(mode)
    h.cycle(300)
    assert trigger(h).status == "PAUSED" and trigger(h).due_at is None
    after = svc.pulse(me, "grades_demo")
    assert after["source"]["health"] == health and after["records"] == before and after["stale"]
    count = h.k.grades.fetch_count
    h.cycle(3000)
    assert h.k.grades.fetch_count == count
    h.k.grades.set_mode("normal")
    svc.check_task(me, "grades_demo", idem_key="recheck")
    h.run()
    assert svc.pulse(me, "grades_demo")["source"]["health"] == "HEALTHY"
    assert trigger(h).status == "ACTIVE"


def test_transient_failure_backs_off_and_cooldown_blocks_manual_poll(h):
    h.standard(policy_spec())
    h.run()
    svc, me = KernelService(h.ctx), Principal(TENANT, USER)
    assert svc.check_task(me, "grades_demo").body["reason"] == "minimum_interval"
    h.k.grades.set_mode("unavailable")
    h.cycle(300)
    assert trigger(h).every_seconds == 600
    assert trigger(h).status == "ACTIVE"
    assert svc.pulse(me, "grades_demo")["state"] == "reconnecting"


def test_legacy_fixed_interval_untouched(h):
    h.standard()
    h.run()
    h.cycle(300)
    assert trigger(h).every_seconds == 300 and not trigger(h).reason.startswith("adaptive:")


@pytest.mark.parametrize("raw", [{"min_seconds": 0}, {"min_seconds": True}, {"max_seconds": 1},
                                  {"jitter_percent": 26}, {"unknown": 1}, []])
def test_invalid_policy_cannot_activate(raw):
    with pytest.raises(SchemaMismatch):
        polling_policy({"polling": raw}, minimum=2, rate_limit=30)


def test_unmet_numeric_condition_does_not_wake_or_spend_model_budget():
    profile = get_profile("web_watch.v1")
    params = {"conditions": [{"field": "value", "op": "lt", "value": 50}], "on_met": "plan"}
    movement = [{"resource": "page", "old": {"value": 100}, "new": {"value": 90}}]
    decision = profile.route(params, source_ok=True, coverage_ok=True, blocked_reason="", baseline=False,
                             changes=profile.annotate(movement, params), model_available=True, egress_allowed=True)
    assert decision.route == "NOOP"
    movement[0]["new"]["value"] = 40
    decision = profile.route(params, source_ok=True, coverage_ok=True, blocked_reason="", baseline=False,
                             changes=profile.annotate(movement, params), model_available=True, egress_allowed=True)
    assert decision.route == "PLANNER"
