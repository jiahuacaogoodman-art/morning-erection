"""GUI projection of Pi's native session. Pi alone owns history and execution."""
import json
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from wakecore.adapters.agent_runtime.native import NativePiError


def tool_label(name, args=None):
    args = args if isinstance(args, dict) else {}
    if name == "morning_computer":
        return {"observe": "观察应用", "screen": "观察屏幕", "inventory": "读取应用清单", "devices": "查看设备",
                "propose_action": "准备操作确认", "status": "读取操作记录"}.get((args or {}).get("operation"), "电脑工具")
    if name == 'morning_terminal':
        return {'start': '启动任务终端', 'read': '读取终端输出', 'input': '输入终端', 'stop': '停止终端', 'list': '查看任务终端'}.get(args.get('operation'), '任务终端')
    if name == 'morning_preview':
        return '打开任务预览'
    return {"morning_inspect_source": "读取监控来源", "morning_prepare_source_watch": "准备环境驻守",
            "morning_inspect_page": "查看网页", "morning_prepare_watch": "准备网页驻守", "morning_watches": "管理驻守"}.get(name, name or "工具")


def content_text(content, kind="text"):
    if isinstance(content, str):
        return content if kind == "text" else ""
    return "\n".join(part.get("text" if kind == "text" else "thinking", "")
                     for part in content or [] if part.get("type") == kind)


def project_messages(messages, streaming=None, running_tools=None):
    """Presentation only: never write this projection back to a Pi session."""
    messages = list(messages)
    if streaming and not any(m.get("timestamp") == streaming.get("timestamp") and m.get("role") == streaming.get("role") for m in messages):
        messages.append(streaming)
    calls = {p["id"]: p for m in messages if isinstance(m.get("content"), list)
             for p in m["content"] if p.get("type") == "toolCall"}
    result = []
    for index, message in enumerate(messages):
        role = message.get("role")
        timestamp = message.get("timestamp") or 0
        at = datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat() if isinstance(timestamp, (int, float)) else timestamp
        item = {"id": message.get("entryId") or f"pi-{role}-{timestamp}-{index}", "at": at, "role": "agent", "text": content_text(message.get("content")), "type": "reply"}
        if role == "system":
            continue
        if role == "custom" and message.get("customType") == "morning.legacy":
            item.update(message.get("details", {}).get("legacy_message", {}))
        elif role == "custom" and message.get("customType") == "morning.watch-event":
            item.update(type="watch_event", text="后台任务已唤醒 Agent。\n" + message.get("details", {}).get("goal", "正在核验相关变化。"))
        elif role == "custom" and not message.get("display", True):
            continue
        elif role == "user":
            item["role"] = "user"
            item["images"] = [p for p in message.get("content", []) if isinstance(p, dict) and p.get("type") == "image"]
            skill = re.match(r'<skill name="([^"\n]+)"', item["text"])
            if skill:
                item["expanded_prompt"] = item["text"]
                item["text"] = "/skill:" + skill[1]
        elif role == "assistant":
            item["thinking"] = content_text(message.get("content"), "thinking")
            if message.get("stopReason") == "aborted" or "operation was aborted" in message.get("errorMessage", "").lower():
                item.update(type="stopped", text=item["text"] or "已停止本轮任务。")
            elif message.get("stopReason") == "error":
                item.update(type="error", text=message.get("errorMessage") or "模型请求未完成。")
            if not item["text"] and not item["thinking"]:
                continue
        elif role == "toolResult":
            item["images"] = [p for p in message.get("content", []) if isinstance(p, dict) and p.get("type") == "image"]
            call = calls.get(message.get("toolCallId"), {})
            item.update(id="tool-" + message.get("toolCallId", str(index)), role="tool", tool=message.get("toolName"),
                        text=tool_label(message.get("toolName"), call.get("arguments")), state="error" if message.get("isError") else "done",
                        output=item["text"], arguments=call.get("arguments"))
            if isinstance(message.get("details"), dict) and message["details"].get("diff"):
                item["diff"] = message["details"]["diff"]
            if isinstance(message.get('details'), dict):
                details = message['details']
                if isinstance(details.get('computer_action'), dict):
                    item['computer_action_id'] = details['computer_action'].get('id')
                if isinstance(details.get('terminal'), dict):
                    item['terminal_id'] = details['terminal'].get('id')
                if isinstance(details.get('preview'), dict):
                    item['preview_id'] = details['preview'].get('id')
        elif role == "bashExecution":
            item.update(role="tool", tool="bash", text=message.get("command", "终端"), output=message.get("output", ""),
                        state="error" if message.get("exitCode") or message.get("cancelled") else "done")
        elif role in ("compactionSummary", "branchSummary"):
            item.update(type="context_summary", text=message.get("summary", ""))
        result.append(item)
        if role == "toolResult" and isinstance(message.get("details"), dict) and message["details"].get("draft"):
            result.append({"id": item["id"] + "-proposal", "at": at, "role": "agent", "type": "proposal",
                           "text": message["details"].get("message", "驻守计划已准备好。"), "draft": message["details"]["draft"]})
    ids = {item["id"] for item in result}
    for call_id, event in (running_tools or {}).items():
        if "tool-" + call_id not in ids:
            result.append({"id": "tool-" + call_id, "role": "tool", "text": tool_label(event.get("toolName"), event.get("args")), "tool": event.get("toolName"),
                           "state": "working", "at": datetime.now(timezone.utc).isoformat(),
                           "output": content_text(event.get("partialResult", {}).get("content")), "arguments": event.get("args")})
    return result


class NativeConversation:
    def __init__(self, host):
        self.host = host
        self.lock = threading.RLock()
        self.operations = threading.RLock()
        self.streaming = None
        self.running_tools = {}
        self.notices = []
        self.widgets = {}
        self.statuses = {}
        self.editor = None
        self.title = None
        self.accepted = {}
        self.exports = {}
        self.host.listeners.append(self._event)

    def _event(self, event):
        kind = event.get("type")
        with self.lock:
            if kind in ("message_start", "message_update") and event.get("message", {}).get("role") == "assistant":
                self.streaming = event["message"]
            elif kind in ("message_end", "agent_settled", "process_ended"):
                self.streaming = None
                if kind in ("agent_settled", "process_ended"):
                    self.running_tools.clear()
            elif kind in ("tool_execution_start", "tool_execution_update"):
                self.running_tools[event["toolCallId"]] = event
            elif kind == "tool_execution_end":
                self.running_tools.pop(event["toolCallId"], None)
            elif kind in ("auth_event", "auth_notice"):
                self.notices = (self.notices + [{"id": self.host.revision, **event["data"]}])[-12:]
            elif kind == "extension_ui_request":
                method = event.get("method")
                if method == "setWidget":
                    self.widgets[event["widgetKey"]] = event.get("widgetLines") or []
                elif method == "setStatus":
                    self.statuses[event["statusKey"]] = event.get("statusText")
                elif method == "set_editor_text":
                    self.editor = {"id": event["id"], "text": event.get("text", "")}
                elif method == "setTitle":
                    self.title = event.get("title")
                elif method == "notify":
                    self.notices = (self.notices + [event])[-12:]

    def snapshot(self):
        status = self.host.status_snapshot()
        result = {**status, "state": None, "stats": None, "dialogue": self.host.legacy_messages,
                  "notices": list(self.notices), "widgets": dict(self.widgets), "statuses": dict(self.statuses), "editor": self.editor, "title": self.title}
        if not status["agent_ready"]:
            return result
        try:
            state = self.host.rpc("get_state", timeout=5)
            # Full native messages are retained by Pi; this is only a rendering projection.
            history = self.host.control("history", timeout=5)
            if history.get("sessionId") and history["sessionId"] != state["sessionId"]:
                state = self.host.rpc("get_state", timeout=5)
                history = self.host.control("history", timeout=5)
                if history.get("sessionId") != state["sessionId"]:
                    raise NativePiError("会话正在切换，请稍后。")
            messages = history.get("messages", [])
            self.host.info["workspace"] = history.get("workspace", str(self.host.workspace))
            self.host.remember_session(state.get("sessionFile"), state=state, workspace=self.host.info["workspace"])
            with self.lock:
                result["dialogue"] = project_messages(messages, self.streaming, self.running_tools)
            result["state"] = state
            result["stats"] = self.host.rpc("get_session_stats", timeout=5)
        except NativePiError as exc:
            result["error"] = str(exc)
        return result

    def prompt(self, body, key=None):
        text = body.get("message")
        if not isinstance(text, str) or (not text.strip() and not body.get("images")):
            raise NativePiError("请填写消息或添加图片。")
        with self.operations:
            if key and key in self.accepted:
                return self.accepted[key]
            self.host.current_prompt = self.host.redact(text[:160])
            params = {"message": text}
            for field in ("images", "streamingBehavior"):
                if field in body:
                    params[field] = body[field]
            result = self.host.rpc("prompt", params)
            if key:
                self.accepted[key] = result
                if len(self.accepted) > 128:
                    self.accepted.pop(next(iter(self.accepted)))
            return result

    def command(self, command, params):
        # Pass native commands through unchanged; requests that change the selected session
        # share a short acceptance lock, never a lock over the whole agent run.
        if command in ("new_session", "switch_session", "fork", "clone"):
            with self.operations:
                result = self.host.rpc(command, params)
                if not result.get("cancelled"):
                    with self.lock:
                        self.streaming = None
                        self.running_tools.clear()
                        self.widgets.clear()
                        self.statuses.clear()
                    selected = self.host.rpc("get_state")
                    workspace = self.host.control("history").get("workspace", str(self.host.workspace))
                    self.host.remember_session(selected.get("sessionFile"), state=selected, workspace=workspace)
                return result
        if command == "export_html" and not params.get("outputPath"):
            directory = self.host.state_dir / "pi" / "exports"
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            params = {**params, "outputPath": str(directory / ("morning-session-" + secrets.token_hex(6) + ".html"))}
        result = self.host.rpc(command, params, timeout=300 if command in ("compact", "bash") else 60)
        if command == "export_html":
            if not Path(result["path"]).is_absolute():
                result["path"] = self.host.control("resolve_path", {"path": result["path"]})["path"]
            token = secrets.token_urlsafe(24)
            self.exports[token] = result["path"]
            if len(self.exports) > 16:
                self.exports.pop(next(iter(self.exports)))
            result = {**result, "download_url": "/api/pulse/pi/export/" + token}
        return result

    def assert_idle(self):
        if self.host.agent_ready and not self.host.control("context").get("isIdle"):
            raise NativePiError("请先停止或等待当前任务完成，再修改 Gateway。")

    def safe_json(self, value):
        return json.loads(json.dumps(self.host.sanitize(value)))
