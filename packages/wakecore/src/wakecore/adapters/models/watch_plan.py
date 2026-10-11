"""Copies a user-confirmed action recipe on a deterministic wake; never invents actions."""
from wakecore.kernel.commands.tasks import load_spec
from wakecore.kernel.domain.enums import Route
from wakecore.kernel.domain.model import TaskRuntime
from wakecore.kernel.locks import tx
from wakecore.kernel.ports.reasoning import ContextBundle, DecisionProposal, PlanProposal, ProposedAction


class WatchPlan:
    model_ref = "configured-watch-plan/1"
    data_egress_target = "model:local-watch-plan"

    def __init__(self, store):
        self.store = store

    def classify(self, context: ContextBundle) -> DecisionProposal:
        return DecisionProposal(route=Route.HUMAN_REVIEW, reason_code="unconfigured_change", model_ref=self.model_ref)

    def plan(self, context: ContextBundle) -> PlanProposal:
        with tx(self.store) as repo:
            rt = repo.get(TaskRuntime, tenant_id=context.tenant_id, task_id=context.task_id)
            recipe = load_spec(repo, rt).source.resource_scope.get("action") if rt else None
        if not recipe or not any(t["tool_id"] == recipe["tool_id"] and t["capability"] == recipe["capability"]
                                 for t in context.allowed_tools):
            return PlanProposal(steps=(), stop_conditions=("configured_tool_unavailable",), model_ref=self.model_ref)
        return PlanProposal(steps=(ProposedAction(
            logical_step_id="confirmed-watch-action", tool_id=recipe["tool_id"], capability=recipe["capability"],
            payload=recipe["payload"], data_egress=tuple(recipe.get("data_egress", [])),
            reason_code="user_confirmed_recipe"),), model_ref=self.model_ref)
