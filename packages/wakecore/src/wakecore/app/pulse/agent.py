"""Conversational task creation. A real model chooses meaning and grounded page observations."""
import json
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

from wakecore.adapters.models.semantic import SemanticModelError
from wakecore.kernel.domain.errors import SchemaMismatch


class DialogueStore:
    """The conversation, plus the vitals of the current agent run.

    Writes used to hit the disk on every single mutation, and `text_delta` mutates once per
    streamed chunk, so one reply rewrote the whole file hundreds of times. Writes are now
    debounced and coalesced; `flush()` is what makes them durable.
    """

    FLUSH_DELAY_SECONDS = 0.25

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        self.turn_lock = threading.Lock()
        self.value = {"messages": [], "pending": None, "replies": {}, "run": None}
        self.revision = 0
        self._flush_timer = None
        if self.path and self.path.exists():
            try:
                stored = json.loads(self.path.read_text())
            except (OSError, ValueError):
                stored = None
            if isinstance(stored, dict):
                for key in ("messages", "pending", "replies", "run"):
                    if key in stored:
                        self.value[key] = stored[key]

    def snapshot(self):
        with self.lock:
            # A JSON round-trip is the cheapest way to hand out a deep copy, but it is not
            # free on an unbounded message list. The revision lets a caller skip the copy
            # entirely when nothing has changed since it last looked.
            return json.loads(json.dumps(self.value))

    def state_revision(self):
        with self.lock:
            return self.revision

    def messages_since(self, since):
        """Messages appended after revision `since`, plus the revision they are current at."""
        with self.lock:
            revision = self.revision
            messages = self.value["messages"]
            if since is None or since <= 0 or since >= revision:
                return (messages if since is None else []), revision
            # Appends and in-place edits both matter; only an append carries a new revision,
            # so a streamed message that grew keeps being re-sent until the turn ends.
            return json.loads(json.dumps(messages)), revision

    def update(self, fn):
        with self.lock:
            fn(self.value)
            self.revision += 1
            self._schedule_flush()

    def flush(self):
        """Write now, cancel any pending write. Safe to call from any thread."""
        with self.lock:
            timer = self._flush_timer
            self._flush_timer = None
        if timer is not None:
            timer.cancel()
        self._write()

    def _schedule_flush(self):
        if not self.path:
            return
        if self._flush_timer is not None:
            self._flush_timer.cancel()
        self._flush_timer = threading.Timer(self.FLUSH_DELAY_SECONDS, self.flush)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def _write(self):
        if not self.path:
            return
        with self.lock:
            payload = json.dumps(self.value, ensure_ascii=False)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(payload)
        temp.chmod(0o600)
        temp.replace(self.path)


def request(body, key=None):
    return SimpleNamespace(json=lambda: body, header=lambda name: key if name == "Idempotency-Key" else None)


def prepare_extractor(candidate, value_type):
    from .server import scalar_extractor

    return scalar_extractor(candidate["selector"], value_type)


class ConversationalAgent:
    def __init__(self, api, interpreter, store=None, coding_runtime=None):
        self.api, self.interpreter = api, interpreter
        self.store = store or DialogueStore()
        self.coding_runtime = coding_runtime

    def _message(self, role, text, **extras):
        return {"id": uuid.uuid4().hex, "role": role, "text": text,
                "at": self.api.kernel.ctx.clock.utc_now().isoformat(), **extras}

    def _append(self, message):
        self.store.update(lambda data: data["messages"].append(message))

    def reply(self, principal, body, key):
        text = body.get("message")
        if not isinstance(text, str) or not 0 < len(text.strip()) <= 8000:
            raise SchemaMismatch("请说说你希望 Agent 做什么")
        if not self.interpreter or not self.interpreter.configured:
            raise SchemaMismatch("任务理解模型还没有连接，这个请求尚未开始执行")
        with self.store.turn_lock:
            saved = self.store.snapshot()
            if key and key in saved["replies"]:
                return saved["replies"][key]
            self._append(self._message("user", text.strip()))
            history = self.store.snapshot()["messages"]
            tasks = [self.api.service.pulse(principal, t["task_id"]) for t in self.api.service.list_tasks(principal)
                     if t["lifecycle"] not in ("DRAFT", "CANCELLED", "EXPIRED")]
            task_context = [{"task_id": t["task_id"], "label": t["label"], "url": t["url"], "state": t["state"]} for t in tasks]
            current_task = body.get("current_task")
            if current_task not in {t["task_id"] for t in tasks}:
                current_task = None
            runtime = self.api._runtime()
            try:
                understood = self.interpreter.interpret(history, tasks=task_context, current_task=current_task,
                    can_operate=bool(runtime.get("model_configured") or self.api.demo))
                page = None
                if understood["status"] == "inspect":
                    from .server import default_session, page_origin

                    url = understood["url"]
                    page_origin(url)
                    if url not in "\n".join(m["text"] for m in history if m["role"] == "user") and not any(t["url"] == url for t in tasks):
                        raise SchemaMismatch("还需要你提供要查看的页面链接")
                    session = body.get("session_ref") or default_session(url)
                    page = self.api.client._call("POST", "/v1/inspect", {"url": url, "origins": [page_origin(url)], "session_ref": session})
                    if page.get("outcome") != "SUCCESS":
                        message = "这个页面需要你先登录，完成后我再确认要关注的内容。" if page.get("outcome") == "AUTH_REQUIRED" else "暂时没有完整读到页面，我还不能确定驻守计划。"
                        reply = self._message("agent", message, type="login" if page.get("outcome") == "AUTH_REQUIRED" else "question", url=url)
                        return self._finish(reply, key)
                    page["candidates"] = [{**candidate, "id": i} for i, candidate in enumerate(page["candidates"])]
                    understood = self.interpreter.interpret(history, tasks=task_context, current_task=current_task, page=page,
                        can_operate=bool(runtime.get("model_configured") or self.api.demo))
                if understood["status"] == "agent":
                    reply = self._run_agent(text.strip())
                elif understood["status"] == "proposal":
                    if page is None or understood["confidence"] < .8:
                        reply = self._message("agent", "我还不能确定你所指的页面内容。可以再说明你关注的对象吗？", type="question")
                    else:
                        draft = self._prepare(principal, understood, page, body, key)
                        reply = self._message("agent", understood["message"], type="proposal", draft={
                            **draft, "wait_summary": understood["wait_summary"], "semantic": understood["mode"] == "semantic"})
                        self.store.update(lambda data: data.update(pending=reply["draft"]))
                elif understood["status"] == "control":
                    known = next((t for t in tasks if t["task_id"] == understood["task_id"]), None)
                    if known is None or understood["command"] not in ("pause", "resume", "cancel", "check"):
                        reply = self._message("agent", "你希望我处理哪一个正在进行的任务？", type="question")
                    else:
                        operation = understood["command"]
                        service = self.api.service
                        if operation == "check":
                            result = service.check_task(principal, known["task_id"], idem_key=key).body
                            answer = "已经安排重新确认页面。" if result["queued"] else "会在允许的检查时间重新确认页面。"
                        else:
                            fn = {"pause": service.pause_task, "resume": service.resume_task, "cancel": service.cancel_task}[operation]
                            fn(principal, known["task_id"], expected_version=known["version"], idem_key=key)
                            answer = {"pause": "已经暂停这个任务。", "resume": "已经恢复这个任务。", "cancel": "已经结束这个任务。"}[operation]
                        reply = self._message("agent", answer, type="reply")
                else:
                    reply = self._message("agent", understood["message"], type="question" if understood["status"] in ("question", "inspect") else "reply")
            except SemanticModelError:
                reply = self._message("agent", "这次没有完成理解，任务尚未开始。模型连接暂时不可用，请稍后再试。", type="error")
            return self._finish(reply, key)

    def _run_agent(self, text):
        from wakecore.adapters.agent_runtime.pi import PiRuntimeError

        if not self.coding_runtime or not self.coding_runtime.configured:
            return self._message("agent", "编程执行引擎还没有连接，这项任务尚未开始执行。", type="error")
        stream_id = None
        final_text = ""
        tool_messages = {}

        def start_run(data):
            data["run"] = {"started_at": self.api.kernel.ctx.clock.utc_now().isoformat(),
                           "finished_at": None, "usage": None, "error": None}

        def finish_run(usage, error=None):
            def write(data):
                run = data.get("run") or {}
                run["finished_at"] = self.api.kernel.ctx.clock.utc_now().isoformat()
                if usage is not None:
                    run["usage"] = usage
                if error:
                    run["error"] = error
                data["run"] = run
            self.store.update(write)

        def update(event):
            nonlocal stream_id, final_text
            kind = event["type"]
            if kind == "text_delta":
                if stream_id is None:
                    message = self._message("agent", "", type="stream")
                    stream_id = message["id"]
                    self._append(message)
                def append(data):
                    item = next(m for m in data["messages"] if m["id"] == stream_id)
                    item["text"] += event["text"]
                self.store.update(append)
            elif kind == "assistant_message":
                final_text = event.get("text") or final_text
                stream_id = None
            elif kind == "tool_execution_start":
                args = event.get("args") or {}
                subject = args.get("path") or args.get("command") or ""
                labels = {"read": "读取文件", "write": "写入文件", "edit": "编辑文件", "bash": "执行命令"}
                tool = event["tool"].removeprefix("morning_")
                message = self._message("tool", labels.get(tool, tool) + " · " + subject,
                                        type="tool", tool=tool, state="working", output="",
                                        subject=subject, started_at=self.api.kernel.ctx.clock.utc_now().isoformat())
                tool_messages[event["call_id"]] = message["id"]
                self._append(message)
            elif kind in ("tool_execution_update", "tool_execution_end"):
                item_id = tool_messages.get(event["call_id"])
                if not item_id:
                    return
                def finish(data):
                    item = next(m for m in data["messages"] if m["id"] == item_id)
                    item["output"] = event.get("output") or ""
                    if kind == "tool_execution_end":
                        item["state"] = "error" if event.get("is_error") else "done"
                        item["finished_at"] = self.api.kernel.ctx.clock.utc_now().isoformat()
                        diff = (event.get("details") or {}).get("diff")
                        if isinstance(diff, str) and diff:
                            item["diff"] = diff[:20000]
                self.store.update(finish)
        self.store.update(start_run)
        try:
            result = self.coding_runtime.run(text, update)
            finish_run((result or {}).get("usage"),
                       error="execution_failed" if result.get("error") else None)
            if result.get("error"):
                return self._message("agent", "这次执行没有完成，已执行的步骤和错误保留在上方。", type="error")
        except PiRuntimeError:
            finish_run(None, error="runtime_interrupted")
            return self._message("agent", "执行引擎暂时中断，尚不能确认任务完成。已执行的步骤保留在上方。", type="error")
        return self._message("agent", final_text or "本轮执行已结束。", type="completion", rendered_by_stream=bool(final_text))

    def _finish(self, reply, key):
        self._append(reply)
        if key:
            def save(data):
                data["replies"][key] = reply
                while len(data["replies"]) > 40:
                    del data["replies"][next(iter(data["replies"]))]
            self.store.update(save)
        return reply

    def _prepare(self, principal, proposal, page, body, key):
        candidates = page["candidates"]
        index = proposal["observation_id"]
        if not isinstance(index, int) or not 0 <= index < len(candidates):
            raise SchemaMismatch("还没有准确找到你关注的内容，请再说明目标")
        selected = candidates[index]
        value_type = proposal["value_type"] or "text"
        rule = proposal["op"] or "change"
        extractor = prepare_extractor(selected, value_type)
        action = None
        if proposal["action_goal"]:
            if not self.api._runtime().get("model_configured") and not self.api.demo:
                raise SchemaMismatch("我能理解这项操作，但网页操作模型尚未连接，暂时不能替你提交")
            verify_id = proposal["verification_observation_id"]
            if not isinstance(verify_id, int) or not 0 <= verify_id < len(candidates) or not proposal["verification_op"]:
                raise SchemaMismatch("还需要明确页面上的完成标记，才能准备代操作")
            verify_candidate = candidates[verify_id]
            verify_type = proposal["verification_type"] or "text"
            verification = prepare_extractor(verify_candidate, verify_type)["columns"][0]
            verification["field"] = "done"
            extractor["columns"].append(verification)
            action = {"goal": proposal["action_goal"], "verify": {"record_id": "page",
                "conditions": [{"field": "done", "op": proposal["verification_op"], "value": proposal["verification_value"]}]}}
        defaults = self.api.polling_defaults()
        interval = proposal["interval_seconds"]
        interval = max(2, min(3600, interval)) if isinstance(interval, int) else defaults["min_seconds"]
        purpose = proposal["wait_summary"] + ("；之后：" + proposal["action_goal"] if proposal["action_goal"] else "；符合要求时通知我")
        config = {"url": page["url"], "prompt": purpose, "label": proposal["label"] or page["title"],
            "selector": selected["selector"], "field_label": selected["label"][:30], "field": "value", "value_type": value_type,
            "rule": rule, "threshold": proposal["value"], "extractor": extractor, "action": action,
            "min_seconds": interval, "max_seconds": max(interval, defaults["max_seconds"]), "session_ref": body.get("session_ref"),
            "wait_summary": proposal["wait_summary"]}
        if proposal["mode"] == "semantic":
            if not proposal["semantic_condition"]:
                raise SchemaMismatch("等待条件还没有理解完整")
            config["semantic_condition"] = proposal["semantic_condition"]
            config["semantic_egress"] = self.interpreter.data_egress_target
        # Test-only local tool preserves the existing real-browser approval harness.
        if self.api.demo and page["url"] == self.api.demo.url and action:
            config.update(demo_course=True, demo_action=True, action=None)
        return self.api._draft(request(config, (key + ":draft") if key else None), principal, {}).body
