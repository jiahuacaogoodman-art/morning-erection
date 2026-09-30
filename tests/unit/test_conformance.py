"""The adapter conformance suite passes every built-in adapter and catches adapters that lie."""
import dataclasses
import sys
from pathlib import Path

import pytest
from test_browser_adapter_options import LIST, NOT_DONE, ORIGIN, FakeRuntime

from wakecore.adapters.clock import FakeClock
from wakecore.adapters.sources.offline_grades import OfflineGradesSource
from wakecore.adapters.sources.playwright_web import PlaywrightWebSource
from wakecore.adapters.sqlite_dev.store import SqliteStore
from wakecore.adapters.tools.browser_cua import MODEL_EGRESS, BrowserCuaTool
from wakecore.adapters.tools.fake_external import FakeEmailProvider
from wakecore.adapters.tools.inbox import InboxTool
from wakecore.kernel.domain.enums import Completeness, ObservationOutcome, SideEffectClass
from wakecore.kernel.domain.model import InboxMessage
from wakecore.kernel.locks import tx
from wakecore.kernel.ports.action import ActionResult, ReconcileResult
from wakecore.kernel.ports.observation import ObservationResult
from wakecore.testing.conformance import (
    ConnectorScenario,
    ToolScenario,
    check_connector,
    check_connector_descriptor,
    check_tool,
    check_tool_descriptor,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "custom_connector" / "src"
EMAIL = ToolScenario(payload={"to": ["a@example.com"], "subject": "s", "body": "b"},
                     effect_count=lambda t: len(t.sent), expect_status="confirmed")


# ---------------------------------------------------------------------- built-in tools

def test_fake_email_provider_conforms():
    check_tool(FakeEmailProvider, EMAIL).assert_ok()


def test_inbox_tool_conforms(tmp_path):
    store = SqliteStore(str(tmp_path / "wk.db"), FakeClock())
    store.migrate()

    def rows(_tool):
        with tx(store) as repo:
            return len(repo.find(InboxMessage, {"tenant_id": "t_conformance"}))

    try:
        report = check_tool(lambda: InboxTool(store), ToolScenario(
            payload={"recipient": "self", "title": "t", "body": "b"}, effect_count=rows, expect_status="confirmed"))
    finally:
        store.close()
    report.assert_ok()
    assert "idempotency.no_second_effect" in report.names() and not report.warnings


def _browser_scope(**extra):
    return {"origins": [ORIGIN], "url": ORIGIN + "/", "extractor": LIST, "record_ids": ["交房租"], **extra}


def _browser_payload():
    return {"goal": "mark 交房租 done", "start_url": ORIGIN + "/",
            "verify": {"record_id": "交房租", "conditions": [{"field": "done", "op": "eq", "value": True}]}}


def test_browser_cua_tool_conforms_on_a_proven_no_effect():
    clock = FakeClock()
    runtimes: list[FakeRuntime] = []

    def make():
        rt = FakeRuntime(NOT_DONE)
        runtimes.append(rt)
        return BrowserCuaTool(rt, clock)

    report = check_tool(make, ToolScenario(payload=_browser_payload(), resource_scope=_browser_scope(), secret="sess",
                                           data_egress=(MODEL_EGRESS,), now=clock.utc_now,
                                           expect_status="failed_no_effect"))
    report.assert_ok()
    assert all(rt.acts <= 1 for rt in runtimes)       # reconcile never re-operated the page


class DoingRuntime(FakeRuntime):
    """The model's act really changes the page (and sends one write request)."""

    def act(self, **kw):
        super().act(**kw)
        self.records = {"交房租": {"title": "交房租", "done": True}}
        return {"status": "done", "actions_executed": 3, "mutating_requests": 1}


def test_browser_cua_tool_conforms_when_the_act_reaches_the_goal():
    clock = FakeClock()
    report = check_tool(lambda: BrowserCuaTool(DoingRuntime(NOT_DONE), clock),
                        ToolScenario(payload=_browser_payload(), resource_scope=_browser_scope(), secret="sess",
                                     data_egress=(MODEL_EGRESS,), now=clock.utc_now, expect_status="confirmed"))
    report.assert_ok()


# ---------------------------------------------------------------------- built-in connectors

def test_offline_grades_conforms():
    def make():
        return OfflineGradesSource({"math": {"name": "Math", "score": 90}, "art": {"name": "Art", "score": None}})

    report = check_connector(make, ConnectorScenario(resource_scope={"course_ids": ["math", "art"]}, secret="cookie",
                                                     volatile_fields=("page_rendered_at",),
                                                     expect_outcome=ObservationOutcome.SUCCESS))
    report.assert_ok()
    assert "fetch.without_secret" in report.names()


@pytest.mark.parametrize("mode", ["login_page", "captcha", "empty", "unavailable", "partial", "schema_invalid"])
def test_offline_grades_failure_modes_conform(mode):
    def make():
        src = OfflineGradesSource({"math": {"name": "Math", "score": 90}, "art": {"name": "Art", "score": 1}})
        src.set_mode(mode, partial_ids=("math",))
        return src

    check_connector(make, ConnectorScenario(resource_scope={"course_ids": ["math", "art"]}, secret="cookie",
                                            volatile_fields=("page_rendered_at",), secret_required=False)).assert_ok()


def test_playwright_web_source_conforms():
    records = {"交房租": {"title": "交房租"}, "买菜": {"title": "买菜"}}
    report = check_connector(lambda: PlaywrightWebSource(FakeRuntime(records)), ConnectorScenario(
        resource_scope=_browser_scope(record_ids=["交房租", "买菜"]), secret="sess", secret_required=False,
        expect_outcome=ObservationOutcome.SUCCESS))
    report.assert_ok()


def test_playwright_web_source_partial_conforms():
    report = check_connector(lambda: PlaywrightWebSource(FakeRuntime({"买菜": {"title": "买菜"}})), ConnectorScenario(
        resource_scope=_browser_scope(), secret="sess", secret_required=False,
        expect_outcome=ObservationOutcome.PARTIAL))
    report.assert_ok()
    assert not report.warnings


# ---------------------------------------------------------------------- the example plugin

@pytest.fixture
def example(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(EXAMPLE))
    monkeypatch.setenv("WAKECORE_EXAMPLE_ROOT", str(tmp_path))
    sys.modules.pop("wakecore_example_plugin", None)
    import wakecore_example_plugin as mod
    yield mod, tmp_path
    sys.modules.pop("wakecore_example_plugin", None)


def test_example_plugin_adapters_conform(example):
    mod, root = example
    from wakecore.plugins import PluginContext

    ctx = PluginContext(name="example", clock=FakeClock(), store=None, config={"root": str(root)})
    journal = root / "journal.log"

    def lines(_tool):
        return len(journal.read_text().splitlines()) if journal.exists() else 0

    check_tool(lambda: mod.make_tool(ctx), ToolScenario(payload={"file": "journal.log", "line": "hello"},
                                                        effect_count=lines, expect_status="confirmed")).assert_ok()
    (root / "grades.json").write_text('{"records": {"math": {"score": 90}}}')
    check_connector(lambda: mod.make_connector(ctx), ConnectorScenario(
        resource_scope={"path": "grades.json", "record_ids": ["math"]},
        expect_outcome=ObservationOutcome.SUCCESS)).assert_ok()
    check_connector(lambda: mod.make_connector(ctx), ConnectorScenario(
        resource_scope={"path": "grades.json", "record_ids": ["math", "art"]},
        expect_outcome=ObservationOutcome.PARTIAL)).assert_ok()
    check_connector(lambda: mod.make_connector(ctx), ConnectorScenario(
        resource_scope={"path": "missing.json", "record_ids": ["math"]},
        expect_outcome=ObservationOutcome.UNAVAILABLE)).assert_ok()


# ---------------------------------------------------------------------- adapters that lie

class DoubleSender(FakeEmailProvider):
    """Claims idempotency but sends again on every attempt."""

    def execute(self, request):
        self.sent[f"{request.effect_key}/{request.attempt_id}"] = {"provider_ref": request.attempt_id}
        return ActionResult(status="confirmed", provider_ref=request.attempt_id, receipt={})


class ReconcileSends(FakeEmailProvider):
    """Reconcile "checks" by sending."""

    def reconcile(self, request):
        self.sent.setdefault(request.effect_key, {"provider_ref": "p"})
        return ReconcileResult(status="confirmed", evidence={})


class FailedButSent(FakeEmailProvider):
    def execute(self, request):
        self.sent[request.effect_key] = {"provider_ref": "p"}
        return ActionResult(status="failed_no_effect", error="timeout")


class ForgetsEffects(FakeEmailProvider):
    def reconcile(self, request):
        return ReconcileResult(status="no_effect", evidence={})


class WrongStatus(FakeEmailProvider):
    def execute(self, request):
        return ActionResult(status="ok")


class BadReceipt(FakeEmailProvider):
    def execute(self, request):
        return ActionResult(status="confirmed", provider_ref="p", receipt={"when": object()})


@pytest.mark.parametrize("cls,failed", [
    (DoubleSender, {"idempotency.no_second_effect", "idempotency.same_provider_ref"}),
    (ReconcileSends, {"reconcile.unexecuted", "reconcile.unexecuted_never_executes"}),
    (FailedButSent, {"execute.no_effect_means_none"}),
    (ForgetsEffects, {"reconcile.sees_confirmed_effect"}),
    (WrongStatus, {"execute.status_legal"}),
    (BadReceipt, {"execute.json_receipt"}),
])
def test_lying_tools_are_caught(cls, failed):
    report = check_tool(cls, EMAIL)
    assert not report.ok
    assert failed <= report.names(failed=True), report.summary()


def test_payload_the_kernel_would_deny_stops_the_run():
    report = check_tool(FakeEmailProvider, ToolScenario(payload={"to": "not-a-list", "subject": "s", "body": "b"}))
    assert report.names(failed=True) == {"scenario.payload_valid"}


def test_bad_tool_descriptor():
    d = dataclasses.replace(FakeEmailProvider.descriptor, tool_id="Email Send", idempotency_retention_seconds=0,
                            retry_owner="provider", url_fields=("target.url",), required_egress=("anywhere",),
                            input_schema={"type": "object", "properties": {"to": {"type": "string", "format": "email"}}})
    r = check_tool_descriptor(d)
    assert {"descriptor.tool_id", "descriptor.idempotency_retention", "descriptor.retry_owner",
            "descriptor.url_fields_in_schema", "descriptor.required_egress"} <= r.names(failed=True)
    assert "descriptor.schema_keywords_enforced" in {c.name for c in r.warnings}
    assert "$.to.format" in r.summary()


def test_writing_tool_that_cannot_resolve_unknown_is_warned():
    d = dataclasses.replace(FakeEmailProvider.descriptor, supports_idempotency=False, supports_reconciliation=False,
                            side_effect_class=SideEffectClass.EXTERNAL_WRITE)
    r = check_tool_descriptor(d)
    assert r.ok and "descriptor.unknown_is_resolvable" in {c.name for c in r.warnings}


def test_not_a_descriptor():
    assert check_tool_descriptor(object()).names(failed=True) == {"descriptor.type"}
    assert check_connector_descriptor({"connector_id": "x"}).names(failed=True) == {"descriptor.type"}


class RecordsOnFailure(OfflineGradesSource):
    def fetch(self, request):
        return ObservationResult(outcome=ObservationOutcome.UNAVAILABLE, completeness=Completeness.COMPLETE, scope={},
                                 records={"math": {"score": 1}}, observed_at=request.requested_at)


class NaiveTime(OfflineGradesSource):
    def fetch(self, request):
        res = super().fetch(request)
        return dataclasses.replace(res, observed_at=res.observed_at.replace(tzinfo=None))


class Flaky(OfflineGradesSource):
    n = 0

    def fetch(self, request):
        Flaky.n += 1
        res = super().fetch(request)
        return dataclasses.replace(res, records={k: {**v, "n": Flaky.n} for k, v in res.records.items()})


class IgnoresSecret(OfflineGradesSource):
    def __init__(self, *a, **kw):
        super().__init__(*a, require_secret=False, **kw)


@pytest.mark.parametrize("cls,failed", [
    (RecordsOnFailure, {"fetch.failure_has_no_records", "fetch.failure_has_error_code", "fetch.failure_not_complete",
                        "fetch.complete_is_success"}),
    (NaiveTime, {"fetch.observed_at_aware"}),
    (Flaky, {"fetch.repeatable"}),
    (IgnoresSecret, {"fetch.without_secret"}),
])
def test_lying_connectors_are_caught(cls, failed):
    report = check_connector(lambda: cls({"math": {"name": "Math", "score": 90}}),
                             ConnectorScenario(resource_scope={"course_ids": ["math"]}, secret="cookie",
                                               volatile_fields=("page_rendered_at",)))
    assert failed <= report.names(failed=True), report.summary()


def test_report_summary_and_assert():
    report = check_tool(WrongStatus, EMAIL)
    with pytest.raises(AssertionError, match=r"\[FAIL\] execute.status_legal"):
        report.assert_ok()
    assert report.summary().startswith("tool email.send: FAILED")
