"""Meaning is judged by the model only after relevant observations change."""
from dataclasses import replace

from harness import TENANT, USER, base_spec
from wakecore.adapters.models.semantic_watch import SemanticWatchReasoner
from wakecore.kernel.service import KernelService, Principal


class JudgeDouble:
    configured = True
    model_ref = "test:semantic"
    data_egress_target = "model:test-semantic"

    def __init__(self):
        self.calls = []
        self.answers = []

    def complete(self, instructions, data, schema, **kwargs):
        self.calls.append(data)
        return self.answers.pop(0)


def setup_watch(h):
    client = JudgeDouble()
    h.ctx.reasoning = SemanticWatchReasoner(h.store, client)
    allowed = {"model:local-watch-plan", client.data_egress_target}
    h.ctx.system_policy = replace(h.ctx.system_policy, allowed_data_egress=h.ctx.system_policy.allowed_data_egress | allowed)
    spec = base_spec(decision={"profile": "semantic_watch.v1", "params": {
        "condition": "只有最终明确通过才通知，未通过与审核中不触发", "watch_fields": ["score"], "on_met": "notify"}},
        observation={"first_snapshot": "baseline_only"}, completion={"evaluator": "watch_forever", "required_outputs": []},
        authority={"model_data_egress": list(allowed)})
    h.standard(spec, egress=tuple(allowed))
    h.run()
    return client


def test_no_model_for_unchanged_content_and_negative_statuses_stay_quiet(h):
    client = setup_watch(h)
    assert client.calls == []
    h.cycle()
    assert client.calls == []
    client.answers.append({"met": False, "reason": "这是未通过，不是通过", "confidence": .99})
    h.k.grades.set_field("PHARM", score="未通过")
    h.cycle()
    assert len(client.calls) == 1 and h.inbox() == []
    assert KernelService(h.ctx).pulse(Principal(TENANT, USER), "grades_demo")["changes"] == []
    h.cycle()
    assert len(client.calls) == 1
    client.answers.append({"met": True, "reason": "结果最终明确为通过", "confidence": .99})
    h.k.grades.set_field("PHARM", score="审核已通过")
    h.cycle()
    assert len(client.calls) == 2
    assert len(h.inbox()) == 1
    assert "最终明确" in h.inbox()[0].body


def test_unrelated_field_changes_do_not_spend_semantic_budget(h):
    client = setup_watch(h)
    h.k.grades.set_field("PHARM", unrelated_visits=123)
    h.cycle()
    assert client.calls == []
