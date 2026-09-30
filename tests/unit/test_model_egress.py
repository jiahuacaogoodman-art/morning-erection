"""The kernel side of OpenAI-compatible endpoints: the browser tool's egress is configurable,
is what the planner, grants and approvals see, and travels with every act (stdlib only)."""
from typing import Any

import pytest
from test_browser_adapter_options import NOT_DONE, FakeRuntime, action

from wakecore.adapters.clock import FakeClock
from wakecore.adapters.tools.browser_cua import MODEL_EGRESS, BrowserCuaTool, check_model_egress
from wakecore.adapters.ui_runtime.client import RuntimeRejected
from wakecore.app.bootstrap import build

RELAY_EGRESS = "model:openai-compatible:relay.example.com"


def test_default_and_relay_descriptors():
    assert BrowserCuaTool(FakeRuntime(), FakeClock()).descriptor.required_egress == (MODEL_EGRESS,)
    tool = BrowserCuaTool(FakeRuntime(), FakeClock(), model_egress=RELAY_EGRESS)
    assert tool.descriptor.required_egress == (RELAY_EGRESS,)
    assert BrowserCuaTool.descriptor.required_egress == (MODEL_EGRESS,)     # the class is untouched


@pytest.mark.parametrize("bad", ["", "openai", "model:openai-compatible:", "model:anthropic",
                                 "model:openai-compatible:Relay.Example.com", "model:openai-compatible:a/b"])
def test_malformed_egress_is_refused(bad):
    with pytest.raises(ValueError):
        check_model_egress(bad)


class Recording(FakeRuntime):
    def __init__(self, **kw: Any) -> None:
        super().__init__(NOT_DONE, **kw)
        self.kw: dict = {}

    def act(self, **kw: Any) -> dict:
        self.kw = kw
        return super().act(**kw)


def test_every_act_carries_the_egress():
    rt, clock = Recording(), FakeClock()
    BrowserCuaTool(rt, clock, model_egress=RELAY_EGRESS).execute(action(clock))
    assert rt.kw["model_egress"] == RELAY_EGRESS


def test_a_runtime_sending_elsewhere_is_no_effect():
    class Elsewhere(Recording):
        def act(self, **kw: Any) -> dict:
            self.acts += 1
            raise RuntimeRejected(409, '{"error": {"code": "model_egress_mismatch", "message": "m", "retryable": false,'
                                       ' "details": {"runtime_model_egress": "model:openai-compatible:other.example"}}}')
    rt, clock = Elsewhere(), FakeClock()
    r = BrowserCuaTool(rt, clock).execute(action(clock))
    assert (r.status, r.error) == ("failed_no_effect", "model_egress_mismatch")
    assert r.receipt == {"runtime_model_egress": "model:openai-compatible:other.example",
                         "approved_model_egress": MODEL_EGRESS}


def test_bootstrap_uses_the_configured_egress(monkeypatch, tmp_path):
    monkeypatch.setenv("WAKECORE_UI_MODEL_EGRESS", RELAY_EGRESS)
    k = build(db_url=f"sqlite:///{tmp_path}/k.db", ui_runtime_url="http://127.0.0.1:9")
    assert k.browser.descriptor.required_egress == (RELAY_EGRESS,)
    assert RELAY_EGRESS in k.ctx.system_policy.allowed_data_egress
    assert MODEL_EGRESS not in k.ctx.system_policy.allowed_data_egress
    k.ctx.store.close()
