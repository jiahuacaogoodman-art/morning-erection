"""Grounded watch tools for Pi. Planning uses Pi; polling stays in WakeCore."""
import time
import uuid

from wakecore.adapters.models.semantic import PROPOSAL_SCHEMA
from wakecore.adapters.sources.environment import validate_source
from wakecore.kernel.domain.errors import SchemaMismatch
from wakecore.kernel.domain.schema import validate

from .agent import request


def watch_schema():
    fields = {key: value for key, value in PROPOSAL_SCHEMA["properties"].items()
              if key not in {"status", "message", "url", "command", "task_id"}}
    return {"type": "object", "additionalProperties": False, "properties": fields, "required": list(fields)}


class NativeWatchTools:
    def __init__(self, api, principal):
        self.api, self.principal = api, principal
        self.inspections = {}

    def __call__(self, name, params):
        platform = getattr(self.api, "platform", None)
        scope = platform.background_scope if platform else None
        if scope is not None:
            if name == "morning_inspect_source":
                source, allowed = validate_source(params.get("source", {})), scope.get("environment", {})
                if not allowed or any(source.get(key) != allowed.get(key) for key in ("kind", "target", "display", "region", "json_pointer", "credential_ref")):
                    raise SchemaMismatch("后台任务只能观察创建时确认的来源。")
            elif name == "morning_inspect_page":
                if params.get("url") != scope.get("url"):
                    raise SchemaMismatch("后台任务只能观察创建时确认的网页。")
            elif name == "morning_computer":
                allowed = scope.get("computer")
                if allowed and params.get("operation") == "propose_action":
                    raise SchemaMismatch("本次唤醒仅核验已经批准的操作，不能再次准备同一个动作。")
                if allowed and params.get("operation") not in ("status", "devices") and (params.get("app") != allowed["app"] or params.get("device_id", "local") != allowed["device_id"]):
                    raise SchemaMismatch("这次核验只针对已经批准的应用。")
            else:
                raise SchemaMismatch("后台唤醒不能扩展任务或授权范围。")
        if platform and name == "morning_computer":
            operation = params.get("operation")
            if operation == "devices":
                return {"devices": platform.devices.public(), "enabled": bool(platform.modules.get("computer"))}
            if operation == "status":
                return {"actions": platform.store.list("computer", limit=20)}
            if operation not in ("inventory", "overview", "observe", "screen", "propose_action"):
                raise SchemaMismatch("Pi 不能修改设备权限或代替用户审批。")
            return platform.command(params)
        if platform and name == 'morning_terminal':
            op = params.get('operation')
            if op == 'list':
                return {'terminals': platform.workspace.state()['terminals']}
            operation = {'start': 'terminal_create', 'read': 'terminal_read', 'input': 'terminal_input', 'stop': 'terminal_stop'}.get(op)
            if not operation:
                raise SchemaMismatch('无法识别的终端操作。')
            body = {**params, 'operation': operation, 'request_id': params.get('_tool_call_id'), 'input_id': params.get('_tool_call_id')}
            result = platform.workspace.command(body)
            if operation == 'terminal_read' and len(result['output']) > 16000:
                result['output'] = result['output'][-16000:]
                result['truncated'] = True
            return {'terminal': result} if operation == 'terminal_create' else result
        if platform and name == 'morning_preview':
            return {'preview': platform.workspace.command({**params, 'operation': 'preview_open'})}
        if platform and name in ("morning_inspect_source", "morning_prepare_source_watch"):
            return platform.command({**params, "operation": "inspect_source" if name == "morning_inspect_source" else "prepare_watch"})
        if platform and name in ("morning_inspect_page", "morning_prepare_watch", "morning_watches"):
            platform.assert_enabled("watch")
        if name == "morning_inspect_page":
            page = self.api._inspect(request({"url": params.get("url")}, None), self.principal, {}).body
            reference = uuid.uuid4().hex
            page["candidates"] = [{**candidate, "id": index} for index, candidate in enumerate(page.get("candidates", []))]
            self.inspections[reference] = (time.monotonic(), page)
            if len(self.inspections) > 32:
                self.inspections.pop(next(iter(self.inspections)))
            return {"inspection_id": reference, **page}
        if name == "morning_prepare_watch":
            found = self.inspections.get(params.get("inspection_id"))
            if not found or time.monotonic() - found[0] > 600:
                raise SchemaMismatch("请重新查看页面后再准备驻守。")
            proposal = params.get("proposal")
            validate(watch_schema(), proposal)
            if proposal["confidence"] < .8:
                raise SchemaMismatch("等待条件仍不明确，请先向用户澄清。")
            if found[1].get("outcome") != "SUCCESS":
                raise SchemaMismatch("页面尚未读到，请先完成登录或解决页面错误。")
            draft = self.api.agent._prepare(self.principal, proposal, found[1], {}, None)
            return {"status": "awaiting_user_start", "draft": {**draft, "wait_summary": proposal["wait_summary"]},
                    "message": "已准备驻守计划，请在界面点击开始驻守。"}
        if name == "morning_watches":
            service = self.api.service
            tasks = [service.pulse(self.principal, task["task_id"]) for task in service.list_tasks(self.principal)
                     if task["lifecycle"] not in ("DRAFT", "CANCELLED", "EXPIRED")]
            operation = params.get("operation")
            if operation == "list":
                return {"items": [{key: task[key] for key in ("task_id", "label", "url", "state", "condition_label")} for task in tasks]}
            task = next((task for task in tasks if task["task_id"] == params.get("task_id")), None)
            if not task:
                raise SchemaMismatch("没有找到这个驻守任务。")
            if operation == "check":
                return service.check_task(self.principal, task["task_id"]).body
            command = {"pause": service.pause_task, "resume": service.resume_task, "cancel": service.cancel_task}.get(operation)
            if not command:
                raise SchemaMismatch("无法识别的驻守操作。")
            return command(self.principal, task["task_id"], expected_version=task["version"]).body
        raise SchemaMismatch("无法识别的 Morning 工具。")
