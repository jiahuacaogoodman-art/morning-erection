"""Optional product modules around native Pi and the existing WakeCore kernel."""
import json
import threading
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from wakecore.adapters.agent_runtime.credentials import CredentialStoreError
from wakecore.adapters.sources.environment import EnvironmentSource, validate_source
from wakecore.app.worker.loop import serve
from wakecore.kernel.commands import setup as setup_cmd
from wakecore.kernel.commands import tasks as task_cmd
from wakecore.kernel.decision.profiles import OPS, condition_holds
from wakecore.kernel.domain.canonical import digest
from wakecore.kernel.domain.model import Grant, InboxMessage
from wakecore.kernel.locks import tx
from wakecore.kernel.service import Response
from wakecore.protocol.computer_actions import validate_action

from . import notifications
from .devices import DeviceError, DeviceManager
from .file_events import FileEvents
from .platform_store import PlatformStore
from .workspace import TaskWorkspace, mount_workspace


class Platform:
    def __init__(self, api, principal, root, *, devices=None):
        self.api, self.principal, self.root = api, principal, Path(root)
        self.store = PlatformStore(root)
        self.devices = devices or DeviceManager(self.store, root)
        self.source = EnvironmentSource(self.devices, self.store)
        ctx = api.kernel.ctx
        ctx.connectors[self.source.descriptor.connector_id] = self.source
        ctx.system_policy = replace(ctx.system_policy, allowed_capabilities=ctx.system_policy.allowed_capabilities | {"environment.observe"})
        with tx(ctx.store) as repo:
            approved = {destination for grant in repo.find(Grant, {"tenant_id": principal.tenant_id})
                        for destination in grant.data_egress if destination.startswith("model:pi:")}
        ctx.system_policy = replace(ctx.system_policy, allowed_data_egress=ctx.system_policy.allowed_data_egress | approved)
        if hasattr(ctx.reasoning, "visual_resolver"):
            ctx.reasoning.visual_resolver = self.source.semantic_images
        self.modules = self.store.get("configuration", "modules") or {"watch": True, "computer": True}
        self.inspections, self.frames, self.previews = {}, {}, {}
        self.observations, self.target_frames = {}, {}
        self.lock = threading.RLock()
        self._runtime_values = {}
        self.device_execution = {}
        self.worker_stop, self.stop = threading.Event(), threading.Event()
        self.worker = self.background = None
        self.native_run = None
        self.background_scope = None
        self.background_goal = None
        self.file_events = FileEvents(api, principal)
        self.background_error = None
        if api.native:
            if hasattr(api.native, 'add_listener'):
                api.native.add_listener(self.pi_event)
            else:
                api.native.host.listeners.append(self.pi_event)
        api._add("GET", r"/pulse/platform", self.get)
        api._add("POST", r"/pulse/platform", self.post)
        api._add("GET", r"/pulse/events", self.events)
        self.workspace = TaskWorkspace(self.store, self.work_context, lambda value: api.native.host.redact(value) if api.native else value, runtime_dir=api.native.host.runtime_path.parent if api.native else None)
        mount_workspace(api, self.workspace)

    def runtime_values(self):
        key = id(self.api.native.host) if self.api.native else 0
        return self._runtime_values.setdefault(key, {})

    @property
    def native_run(self):
        return self.runtime_values().get('run')

    @native_run.setter
    def native_run(self, value):
        self.runtime_values()['run'] = value

    @property
    def background_scope(self):
        return self.runtime_values().get('scope')

    @background_scope.setter
    def background_scope(self, value):
        self.runtime_values()['scope'] = value

    @property
    def background_goal(self):
        return self.runtime_values().get('goal')

    @background_goal.setter
    def background_goal(self, value):
        self.runtime_values()['goal'] = value

    def work_context(self):
        host = self.api.native.host if self.api.native else None
        state = getattr(self.api.native, 'slot', {}).get('state', {}) if self.api.native else {}
        if not state and host and host.agent_ready:
            state = host.rpc('get_state', timeout=5)
        return {'session_id': state.get('sessionId'), 'session_path': state.get('sessionFile'),
                'workspace': str(host.info.get('workspace', host.workspace)) if host else str(self.root)}

    def remember_frame(self, device, frame):
        with self.lock:
            self.frames[device] = frame
            target = frame.get('app') or 'screen:' + str(frame.get('display'))
            self.target_frames[(device, target)] = frame
            if frame.get('id'):
                self.observations[(device, frame['id'])] = frame
            self.observations = {key: value for key, value in self.observations.items() if time.time() - value.get('at', 0) < 300}
            if len(self.observations) > 200:
                self.observations.pop(next(iter(self.observations)))

    def assert_enabled(self, name):
        if not self.modules.get(name):
            raise DeviceError(f"{name} 模块已关闭，可在设置中重新启用。", "module_disabled")

    def snapshot(self):
        items = [self.api.service.pulse(self.principal, t["task_id"]) for t in self.api.service.list_tasks(self.principal)]
        hidden = {r["id"] for r in self.store.list("archived")}
        return {"modules": {"watch": bool(self.modules.get("watch")), "computer": bool(self.modules.get("computer")), "desktop_notifications": bool(self.modules.get("desktop_notifications")),
                            "computer_autonomy": bool(self.modules.get("computer_autonomy"))},
                "desktop_notifications_available": notifications.available(),
                "watch_service": {"running": bool(self.worker and self.worker.is_alive() and not self.worker_stop.is_set()), "independent_of_page": True,
                                  "file_events": self.file_events.error or "系统事件与定时观察", "error": self.background_error},
                "devices": self.devices.public(), "watches": [w for w in items if w["task_id"] not in hidden],
                "computer": self.store.list("computer", limit=60), "runs": self.store.list("pi_run", limit=60),
                "terminals": [self.workspace.public(item) for item in self.store.list('terminal', limit=60)],
                "wakeups": self.store.list("wakeup", limit=60), "notifications": self.store.list("notification", limit=40),
                "events": self.store.events(max(0, self.latest_seq() - 50))}

    def latest_seq(self):
        with self.store.db() as conn:
            return conn.execute("SELECT coalesce(max(seq),0) FROM events").fetchone()[0]

    def get(self, req, principal, args):
        return Response(200, self.snapshot())

    def events(self, req, principal, args):
        from urllib.parse import parse_qs
        try:
            after = max(0, int(parse_qs(req.environ.get("QUERY_STRING", "")).get("after", [0])[0]))
        except ValueError:
            after = 0
        events = self.store.events(after)
        cursor = events[-1]["seq"] if events else after
        return Response(200, {"events": events, "cursor": cursor, "has_more": self.latest_seq() > cursor})

    def post(self, req, principal, args):
        try:
            return Response(200, self.command(req.json()))
        except CredentialStoreError:
            return Response(422, {"error": {"code": "secure_store_unavailable", "message": "无法访问系统凭据存储，请检查钥匙串权限。未回退到明文存储。"}})
        except (ValueError, KeyError, OSError) as exc:
            message = self.api.native.host.redact(str(exc)) if self.api.native else str(exc)
            return Response(422, {"error": {"code": getattr(exc, "code", "platform_operation_failed"), "message": message}})

    def command(self, body):
        op = body.get("operation")
        device = body.get("device_id", "local")
        if op in ("observe", "screen", "overview", "propose_action", "approve_action"):
            self.assert_enabled("computer")
        if op == "device_save":
            return self.devices.save(body["device"])
        if op == "device_delete":
            self.devices.delete(device)
            return {"deleted": True}
        if op in ("device_test", "inventory"):
            result = self.devices.call(device, "/health" if op == "device_test" else "/inventory")
            if op == "device_test":
                if result.get("protocol") != "morning.computer-node/1":
                    raise DeviceError("连接到了不兼容的设备服务。")
                self.store.event("device.connected", device, {"bridge_ok": result.get("bridge", {}).get("ok", False)})
            return result
        if op in ("permissions", "takeover", "resume_computer"):
            value = body["permissions"] if op == "permissions" else {"paused": op == "takeover"}
            result = self.devices.call(device, "/permissions", value)
            self.store.event("computer.permissions_changed", device, {"paused": result["paused"]})
            if op == "takeover":
                for job in self.store.list("computer", states=("approval", "queued")):
                    if job["device_id"] == device:
                        self.store.save("computer", job["id"], {**job, "state": "cancelled"})
            return result
        if op == "overview":
            return self.devices.call(device, "/overview", {"limit": body.get("limit", 6)})
        if op == "computer_autonomy":
            if type(body.get("enabled")) is not bool:
                raise ValueError("自主执行需要是开关。")
            self.modules = self.store.save("configuration", "modules", {**self.modules, "computer_autonomy": body["enabled"]})
            self.store.event("module.changed", "computer_autonomy", {"enabled": body["enabled"]})
            return {"computer_autonomy": body["enabled"]}
        if op in ("observe", "screen"):
            result = self.devices.call(device, "/" + op, {k: body[k] for k in ("app", "display", "region") if k in body})
            self.remember_frame(device, result)
            return result
        if op == "last_frame":
            if body.get('app') or body.get('display'):
                return self.target_frames.get((device, body.get('app') or 'screen:' + str(body['display']))) or {}
            return self.frames.get(device) or {}
        if op == "action_preview":
            return {"image": self.previews.get(body.get("id"))}
        if op == "propose_action":
            validate_action(body.get("method"), body.get("arguments"))
            observation = self.observations.get((device, body.get('observation_id')))
            if not observation or observation.get("id") != body.get("observation_id") or observation.get("app") != body.get("app"):
                raise DeviceError("请先观察当前应用，再准备操作。", "observation_required")
            if not isinstance(body.get("goal"), str) or not body["goal"].strip():
                raise ValueError("操作需要清楚说明目的。")
            # Refresh before asking a person to review. Model latency must not
            # consume their approval window or silently change the target.
            fresh = self.devices.call(device, "/observe", {"app": body["app"]})
            self.remember_frame(device, fresh)
            if fresh["fingerprint"] != observation["fingerprint"]:
                raise DeviceError("窗口在准备操作时发生了变化，请使用新观察重新决定动作。", "screen_changed")
            coordinates = body.get("method") == "drag" or body.get("method") == "click" and "element_index" not in body["arguments"]
            if coordinates and fresh.get("image") != observation.get("image"):
                raise DeviceError("截图在准备坐标操作时发生了变化，请重新观察。", "screen_changed")
            body = {**body, "observation_id": fresh["id"]}
            payload = {key: body[key] for key in ("device_id", "app", "observation_id", "method", "arguments") if key in body}
            payload["device_id"] = device
            key = self.store.new_id("computer")
            if fresh.get("image"):
                self.previews[key] = fresh["image"]
                if len(self.previews) > 60:
                    self.previews.pop(next(iter(self.previews)))
            target = next((element["label"] for element in fresh.get("elements", []) if element["index"] == str(body["arguments"].get("element_index"))), None)
            # Opt-in, user-only switch (Pi cannot change it). The node still enforces app
            # scope, exclusions, credential windows, system shortcuts and fresh observations.
            auto = bool(self.modules.get("computer_autonomy"))
            job = self.store.save("computer", key, {**payload, "goal": body["goal"][:500], "state": "queued" if auto else "approval", "auto": auto, "created": time.time(),
                "expires": fresh["at"] + 120, "digest": digest(payload), "session_path": self.session_path(), "target": target, "has_preview": bool(fresh.get("image")), "model_binding": self.model_binding() if self.api.native else None,
                "tool_call_id": body.get('_tool_call_id'), "run_id": self.native_run})
            if auto:
                self.execute(job)
                done = self.store.get("computer", key)
                observed = done["state"] == "observed"
                after = self.frames.get(device) if observed else {}
                return {"computer_action": done, "image": after.get("image"),
                        "observation": {k: v for k, v in after.items() if k not in ("image", "actions")} or None,
                        "message": "已执行并重新观察；请根据新观察核验目标。" if observed else "操作没有完成：" + str(done.get("error") or "结果未知，请重新观察。")}
            self.store.event("computer.approval_requested", key, {"goal": job["goal"], "device_id": device})
            return {"computer_action": job, "message": "操作已准备好，等待你在界面批准。"}
        if op in ("approve_action", "reject_action"):
            with self.lock:
                job = self.store.get("computer", body["id"])
                if not job or job["digest"] != body.get("digest"):
                    raise DeviceError("操作已变化，请刷新后确认。")
                if job["state"] != "approval":
                    return job
                if op == "reject_action":
                    return self.store.save("computer", job["id"], {**job, "state": "rejected"})
                if time.time() > job["expires"]:
                    self.store.save("computer", job["id"], {**job, "state": "expired"})
                    raise DeviceError("操作确认已过期，请重新观察。")
                job = self.store.save("computer", job["id"], {**job, "state": "queued"})
                threading.Thread(target=self.execute, args=(job,), daemon=True).start()
                return job
        if op == "inspect_source":
            self.assert_enabled("watch")
            source = validate_source(body["source"])
            current = self.source.read(source)
            key = self.store.new_id("inspection")
            self.inspections[key] = {"source": source, "current": current, "at": time.time()}
            self.inspections = {k: v for k, v in self.inspections.items() if time.time() - v["at"] < 600}
            return {"inspection_id": key, "source": source, "current": current}
        if op == "prepare_watch":
            self.assert_enabled("watch")
            return self.prepare_watch(body)
        if op == "update_watch":
            self.assert_enabled("watch")
            return self.update_watch(body)
        if op == "watch_archive":
            task = self.api.service.get_task(self.principal, body["task_id"])
            if task["lifecycle"] not in ("COMPLETED", "CANCELLED", "EXPIRED"):
                self.api.service.cancel_task(self.principal, body["task_id"], expected_version=task["version"])
            self.store.save("archived", body["task_id"], {"state": "archived"})
            return {"archived": True}
        if op == "module":
            name = body.get("name")
            if name not in ("watch", "computer") or type(body.get("enabled")) is not bool:
                raise ValueError("模块设置无效。")
            self.modules = self.store.save("configuration", "modules", {**self.modules, name: body["enabled"]})
            if name == "watch":
                self.set_worker(body["enabled"])
            elif not body["enabled"]:
                self.devices.close()
            self.store.event("module.changed", name, {"enabled": body["enabled"]})
            return {"modules": self.modules}
        if op == "desktop_notifications":
            if type(body.get("enabled")) is not bool or body["enabled"] and not notifications.available():
                raise ValueError("当前宿主无法启用系统提醒。")
            self.modules = self.store.save("configuration", "modules", {**self.modules, "desktop_notifications": body["enabled"]})
            return {"enabled": body["enabled"]}
        if op == "test_notification":
            return notifications.send("后台提醒测试：关闭网页后，驻守服务仍可在这里提醒你。")
        if op == "dismiss_notification":
            note = self.store.get("notification", body["id"])
            if note:
                self.store.save("notification", body["id"], {**note, "state": "read"})
            return {"read": True}
        if op == "dismiss_action":
            job = self.store.get("computer", body["id"])
            if job and job["state"] not in ("approval", "running"):
                self.store.save("computer", job["id"], {**job, "dismissed": True})
            return {"dismissed": True}
        if op == "continue_wakeup":
            return self.dispatch_wakeup(body["id"], user_requested=True)
        if op == "source_credential":
            source = validate_source({"kind": "http", "target": body["url"]})
            from urllib.parse import urlsplit
            parsed = urlsplit(source["target"])
            key = body.get("id") or self.store.new_id("api")
            token = body.get("token")
            if token is None:
                self.devices.secure().delete("source:" + key)
            elif not isinstance(token, str) or not 1 <= len(token) <= 16384:
                raise ValueError("API 凭据格式无效。")
            else:
                self.devices.secure().write("source:" + key, {"origin": f"{parsed.scheme}://{parsed.netloc}", "token": token})
            return {"credential_ref": key, "saved": token is not None}
        raise ValueError("无法识别的平台操作。")

    def session_path(self):
        if self.api.native:
            saved = getattr(self.api.native.host, 'selection_path', self.root / "pi/selected-session.json")
            if saved.exists():
                return json.loads(saved.read_text()).get("path")
        return None

    def model_binding(self):
        if not self.api.native or not self.api.native.host.agent_ready:
            raise ValueError("请先启动 Pi，再确认后台唤醒的模型服务。")
        binding = self.api.native.host.control("context").get("modelBinding")
        if not binding:
            raise ValueError("Pi 的当前模型服务尚未就绪。")
        return binding

    def execute(self, job):
        with self.lock:
            gate = self.device_execution.setdefault(job['device_id'], threading.Lock())
        with gate:
            latest = self.store.get('computer', job['id'])
            if not latest or latest['state'] != 'queued':
                return
            if time.time() > latest['expires']:
                self.store.save('computer', job['id'], {**latest, 'state': 'expired', 'error': '等待设备期间观察已过期，请重新观察。'})
                return
            job = self.store.save('computer', job['id'], {**latest, 'state': 'running'})
            self._execute_action(job)

    def _execute_action(self, job):
        try:
            self.assert_enabled("computer")
            result = self.devices.call(job["device_id"], "/act", {"key": job["id"], **{k: job[k] for k in ("app", "method", "arguments", "observation_id")}})
            if result.get("after"):
                self.remember_frame(job["device_id"], result["after"])
            state = "observed" if result.get("status") == "observed" else "unknown"
            job = self.store.save("computer", job["id"], {**job, "state": state, "result": {k: v for k, v in result.items() if k != "after"}})
            self.store.event("computer." + state, job["id"], {"device_id": job["device_id"]})
            if state == "observed" and job.get("session_path") and not job.get("auto"):
                self.enqueue_wakeup("computer:" + job["id"], job["session_path"], "已批准的电脑动作已执行，请使用 morning_computer 重新观察并核验目标，不能仅凭发送成功宣称完成。", job["goal"], {"computer": {"device_id": job["device_id"], "app": job["app"]}, "wake_model_binding": job.get("model_binding")})
        except Exception as exc:
            # A lost HTTP response can still mean the action reached the node.
            known_no_effect = isinstance(exc, DeviceError) and exc.code in {"invalid_action", "outside_window", "input_too_long", "human_credentials", "system_shortcut", "permission_required", "stale_observation", "screen_changed", "invalid_element", "module_disabled", "device_disabled", "taken_over", "observation_incomplete"}
            self.store.save("computer", job["id"], {**job, "state": "failed" if known_no_effect else "unknown", "error": str(exc)[:300]})

    def prepare_watch(self, body):
        inspection = self.inspections.get(body.get("inspection_id"))
        if not inspection or time.time() - inspection["at"] > 600:
            raise ValueError("请先读取目标来源，再根据实际内容准备驻守。")
        source, current = inspection["source"], inspection["current"]
        semantic = body.get("semantic_condition")
        field, op = body.get("field", "value"), body.get("op", "change")
        if field not in current or op not in {*OPS, "change"}:
            raise ValueError("等待条件没有对应当前来源中的实际字段。")
        interval = body.get("interval_seconds", 15)
        if type(interval) is not int or not 2 <= interval <= 3600:
            raise ValueError("检查间隔需要是 2 到 3600 秒。")
        label = str(body.get("label") or "环境驻守")[:80]
        condition = str(body.get("condition") or "目标内容发生变化")[:1000]
        on_event = body.get("on_event", "notify")
        if on_event not in ("notify", "wake_agent"):
            raise ValueError("后续动作只能是通知或唤醒当前 Agent。")
        if on_event == "wake_agent" and not self.session_path():
            raise ValueError("请先启动 Pi 会话，才能绑定唤醒后的任务。")
        conditions = [] if op == "change" else [{"field": field, "op": op, "value": body.get("value"), "kind": "condition_met"}]
        key = self.store.new_id("watch")
        scope = {"environment": source, "record_ids": ["target"], "label": label, "kind": source["kind"],
                 "pulse_field": field, "pulse_field_label": "当前状态", "condition_label": condition,
                 "on_event": on_event, "session_path": self.session_path(), "action_goal": str(body.get("action_goal") or "报告观察到的变化")[:1000],
                 "polling": {"min_seconds": interval, "max_seconds": interval if "interval_seconds" in body else max(interval, 120), "error_max_seconds": max(900, interval), "jitter_percent": 0 if "interval_seconds" in body else 10}}
        egress = []
        if on_event == "wake_agent":
            scope["wake_model_binding"] = self.model_binding()
            egress = ["model:pi:" + scope["wake_model_binding"]["origin"]]
            self.api.kernel.ctx.system_policy = replace(self.api.kernel.ctx.system_policy, allowed_data_egress=self.api.kernel.ctx.system_policy.allowed_data_egress | set(egress))
        decision = {"profile": "web_watch.v1", "params": {"conditions": conditions, "watch_fields": [field], "notify_on_change": op == "change", "on_met": "notify", "title": label + "有变化"}}
        if semantic:
            interpreter = self.api.agent.interpreter
            if not interpreter or not interpreter.configured:
                raise ValueError("语义驻守需要先连接模型网关。")
            egress = sorted(set([*egress, interpreter.data_egress_target]))
            decision = {"profile": "semantic_watch.v1", "params": {"condition": str(semantic)[:2000], "watch_fields": [field], "on_met": "notify", "title": label + "等待条件已满足"}}
        spec = {"schema_version": 1, "task_id": key, "root_task_id": key, "spec_version": 1, "purpose": condition,
            "source": {"binding_ref": key, "resource_scope": scope}, "trigger": {"kind": "interval", "every_seconds": interval, "timezone": "Asia/Shanghai", "catchup_policy": "coalesce_latest"},
            "observation": {"completeness_required": "full_target_scope", "first_snapshot": "baseline_only" if op == "change" and not semantic else "notify_existing_results", "ignore_fields": [], "target_key": "record_ids"},
            "completion": {"evaluator": "watch_forever", "evaluator_version": 1, "required_outputs": []}, "decision": decision,
            "authority": {"grant_ref": key, "read_capabilities": ["environment.observe"], "write_capabilities": [], "notify_capabilities": ["inbox.notify_self"], "model_data_egress": egress},
            "limits": {"min_probe_interval_seconds": interval, "max_steps_per_run": 8, "max_run_seconds": 120, "max_model_attempts_per_day": 24},
            "followup": {"enabled": False}, "expires_at": (self.api.kernel.ctx.clock.utc_now() + timedelta(days=30)).isoformat()}
        with tx(self.api.kernel.ctx.store) as repo:
            setup_cmd.register_source_binding(repo, self.api.kernel.ctx, tenant_id=self.principal.tenant_id, owner=self.principal.subject,
                source_ref=key, connector_id=self.source.descriptor.connector_id, source_uri=source["target"] or "screen", resource_scope={"environment": source}, capabilities=["environment.observe"], secret_ref=None)
            record = task_cmd.create_draft(repo, self.api.kernel.ctx, tenant_id=self.principal.tenant_id, principal=self.principal.subject, raw=spec)
        draft = {"task_id": key, "spec_digest": record.spec_digest, "label": label, "condition_label": condition, "wait_summary": condition,
                 "current": current, "action": {"goal": scope["action_goal"]} if on_event == "wake_agent" else None,
                 "url": None, "source": source, "data_egress": egress, "already_met": any(condition_holds(c, current) for c in conditions)}
        return {"draft": draft, "message": "驻守计划已准备好；确认后由后台服务持续观察。"}

    def pi_event(self, event):
        kind = event.get("type")
        with self.lock:
            if kind in ("background_finished", "process_ended"):
                self.background_scope = None
                self.background_goal = None
            if kind == "agent_start":
                self.native_run = self.store.new_id("pi")
                title = self.background_goal or getattr(self.api.native.host, "current_prompt", None) or "Agent 任务"
                self.store.save("pi_run", self.native_run, {"state": "running", "title": title, "created": time.time(), "session_path": self.session_path(), "tools": []})
            if self.native_run:
                job = self.store.get("pi_run", self.native_run)
                if kind == "tool_execution_start":
                    job["tools"] = (job["tools"] + [event.get("toolName")])[-80:]
                elif kind == "message_end" and event.get("message", {}).get("role") == "assistant":
                    message = event["message"]
                    job["result"] = "\n".join(p.get("text", "") for p in message.get("content", []) if p.get("type") == "text")[:3000]
                    if message.get("stopReason") in ("error", "aborted"):
                        job["state"] = "failed" if message["stopReason"] == "error" else "cancelled"
                        if message["stopReason"] == "aborted":
                            for action in self.store.list("computer", states=("approval",)):
                                if action.get("session_path") == job.get("session_path"):
                                    self.store.save("computer", action["id"], {**action, "state": "cancelled"})
                elif kind == "message_start" and event.get("message", {}).get("role") == "user":
                    job["title"] = "\n".join(p.get("text", "") for p in event["message"].get("content", []) if p.get("type") == "text")[:120]
                elif kind in ("agent_settled", "process_ended"):
                    if job["state"] == "running":
                        job["state"] = "finished" if kind == "agent_settled" else "interrupted"
                self.store.save("pi_run", job["id"], job)
                if kind in ("agent_settled", "process_ended"):
                    self.store.event("pi." + job["state"], job["id"])
                    self.native_run = None

    def update_watch(self, body):
        interval = body.get("interval_seconds")
        if type(interval) is not int or not 2 <= interval <= 3600:
            raise ValueError("检查间隔需要是 2 到 3600 秒。")
        condition = body.get("condition")
        if not isinstance(condition, str) or not 1 <= len(condition.strip()) <= 2000:
            raise ValueError("请写明等待条件。")
        on_event = body.get("on_event", "notify")
        if on_event not in ("notify", "wake_agent"):
            raise ValueError("后续动作无效。")
        if on_event == "wake_agent" and not self.session_path():
            raise ValueError("请先启动 Pi 会话，再绑定后续任务。")
        binding = self.model_binding() if on_event == "wake_agent" else None
        with tx(self.api.kernel.ctx.store) as repo:
            rt = self.api.service._owned_task(repo, self.principal, body["task_id"])
            spec = task_cmd.load_spec(repo, rt)
            if spec.digest() != body.get("spec_digest") or rt.lifecycle not in ("ACTIVE", "PAUSED"):
                raise ValueError("驻守计划已变化或已结束，请刷新后重新编辑。")
            value = spec.to_dict()
            scope = value["source"]["resource_scope"]
            if condition != scope.get("condition_label"):
                interpreter = self.api.agent.interpreter
                if not interpreter or not interpreter.configured:
                    raise ValueError("理解新的等待条件需要连接模型网关。")
                value["decision"] = {"profile": "semantic_watch.v1", "params": {"condition": condition,
                    "watch_fields": [scope.get("pulse_field", "value")], "on_met": "plan" if scope.get("action") else "notify", "title": scope.get("label", "驻守") + "等待条件已满足"}}
                value["authority"]["model_data_egress"] = sorted(set([*value["authority"]["model_data_egress"], interpreter.data_egress_target]))
                value["limits"]["max_model_attempts_per_day"] = 24
            scope.update(condition_label=condition, on_event=on_event, action_goal=str(body.get("action_goal") or "报告变化")[:1000])
            if on_event == "wake_agent":
                scope["session_path"] = self.session_path()
                scope["wake_model_binding"] = binding
                value["authority"]["model_data_egress"] = sorted(set([*value["authority"]["model_data_egress"], "model:pi:" + binding["origin"]]))
                self.api.kernel.ctx.system_policy = replace(self.api.kernel.ctx.system_policy, allowed_data_egress=self.api.kernel.ctx.system_policy.allowed_data_egress | set(value["authority"]["model_data_egress"]))
            scope["polling"].update(min_seconds=interval, max_seconds=max(interval, scope["polling"]["max_seconds"]), error_max_seconds=max(900, interval, scope["polling"]["max_seconds"]))
            value["spec_version"] += 1
            value["trigger"]["every_seconds"] = interval
            value["limits"]["min_probe_interval_seconds"] = interval
            value["purpose"] = condition
            created = task_cmd.create_draft(repo, self.api.kernel.ctx, tenant_id=self.principal.tenant_id, principal=self.principal.subject, raw=value)
            setup_cmd.register_grant(repo, self.api.kernel.ctx, tenant_id=self.principal.tenant_id, principal=self.principal.subject,
                grant_ref=spec.authority.grant_ref, capabilities=list(spec.authority.requested_capabilities()), data_egress=value["authority"]["model_data_egress"],
                resource_scope={key: scope[key] for key in ("origins", "environment", "apps") if key in scope}, expires_at=spec.expires_at)
            changed = task_cmd.activate(repo, self.api.kernel.ctx, tenant_id=self.principal.tenant_id, principal=self.principal.subject, task_id=rt.task_id,
                spec_version=created.spec_version, expected_digest=created.spec_digest, expected_version=rt.version)
            if rt.lifecycle == "PAUSED":
                task_cmd.pause(repo, self.api.kernel.ctx, tenant_id=self.principal.tenant_id, principal=self.principal.subject, task_id=rt.task_id, expected_version=changed.version)
        self.store.event("watch.updated", body["task_id"])
        return {"updated": True}

    def enqueue_wakeup(self, key, session_path, context, goal, scope=None):
        if self.store.get("wakeup", key):
            return
        self.store.save("wakeup", key, {"state": "pending", "session_path": session_path, "context": context[:5000], "goal": goal[:1000], "scope": scope or {}})

    def dispatch_wakeup(self, key, user_requested=False):
        item = self.store.get('wakeup', key)
        if item and self.api.native and hasattr(self.api.native, 'find'):
            slot = self.api.native.find(path=item.get('session_path'), load=False)
            if slot:
                with self.api.native.use(slot):
                    return self._dispatch_wakeup_current(key, user_requested)
        return self._dispatch_wakeup_current(key, user_requested)

    def _dispatch_wakeup_current(self, key, user_requested=False):
        item = self.store.get("wakeup", key)
        if not item or item["state"] not in ("pending", "waiting"):
            return item or {}
        if key.startswith("watch:") and not self.modules.get("watch"):
            return item
        if not self.api.native or self.session_path() != item["session_path"]:
            return self.store.save("wakeup", key, {**item, "state": "waiting"})
        if self.background_scope is not None or not self.api.native.host.agent_ready:
            return item
        # Pi event delivery must remain unblocked while we wait for RPC/control.
        context = self.api.native.host.control("context")
        if not context.get("isIdle"):
            return item
        binding = context.get("modelBinding") if user_requested else item.get("scope", {}).get("wake_model_binding")
        if not binding or binding != context.get("modelBinding"):
            return self.store.save("wakeup", key, {**item, "state": "waiting", "error": "模型服务已变化，请确认是否使用当前模型继续。"})
        with self.lock:
            latest = self.store.get("wakeup", key)
            if latest["state"] not in ("pending", "waiting") or self.background_scope is not None:
                return latest
            self.store.save("wakeup", key, {**item, "state": "dispatching"})
            self.background_scope = item.get("scope", {})
            self.background_goal = item["goal"]
        try:
            result = self.api.native.host.control("background_turn", {"id": key, "goal": item["goal"], "context": item["context"], "modelBinding": binding})
            if not result.get("accepted"):
                self.background_scope = None
                self.background_goal = None
                return self.store.save("wakeup", key, {**item, "state": "waiting" if result.get("reason") == "model_changed" else "pending"})
            return self.store.save("wakeup", key, {**item, "state": "delivered"})
        except Exception:
            return self.store.save("wakeup", key, {**item, "state": "unknown", "error": "Pi 接收结果未知，未自动重发。"})

    def sync_notifications(self):
        with tx(self.api.kernel.ctx.store) as repo:
            messages = repo.find(InboxMessage, {"tenant_id": self.principal.tenant_id}, order_by=("-created_at",), limit=200)
        for note in reversed(messages):
            if self.store.get("notification", note.message_id):
                continue
            task = self.api.service.pulse(self.principal, note.task_id)
            scope = task["configuration"]["spec"]["source"]["resource_scope"]
            wakeup = None
            if scope.get("on_event") == "wake_agent" and task["lifecycle"] == "ACTIVE":
                wakeup = {"state": "pending", "session_path": scope.get("session_path"), "context": note.body[:5000], "goal": scope.get("action_goal", "报告变化")[:1000], "scope": scope}
            enabled = bool(self.modules.get("desktop_notifications")) and (not self.api.settings or self.api.settings.all().get("notify_on_change", True))
            inserted = self.store.deliver_notification(note.message_id, {"state": "unread", "task_id": note.task_id, "title": note.title, "body": note.body,
                                                                         "desktop_delivery": "dispatching" if enabled else "disabled"}, wakeup)
            if inserted and enabled:
                result = notifications.send(note.title)
                stored = self.store.get("notification", note.message_id)
                self.store.save("notification", note.message_id, {**stored, "desktop_delivery": result["status"]})

    def set_worker(self, enabled):
        if enabled:
            if self.worker and self.worker.is_alive():
                if not self.worker_stop.is_set():
                    return
                raise ValueError("Watch 正在停止，请稍后再启动。")
            self.worker_stop = threading.Event()
            self.worker = threading.Thread(target=serve, args=(self.api.kernel.ctx, "morning-watch"), kwargs={"stop": self.worker_stop, "idle_sleep": .5, "max_steps": 1}, daemon=True)
            self.worker.start()
        else:
            self.worker_stop.set()

    def start(self):
        self.set_worker(self.modules.get("watch", True))
        def loop():
            rounds = 0
            while not self.stop.wait(1):
                try:
                    rounds += 1
                    if rounds % 5 == 1:
                        self.file_events.sync(self.modules.get("watch", False))
                    self.file_events.flush()
                    self.sync_notifications()
                    for item in self.store.list("wakeup", states=("pending", "waiting")):
                        self.dispatch_wakeup(item["id"])
                    self.background_error = None
                except Exception:
                    self.background_error = "后台事件同步未完成，正在重连；原始任务记录保留。"
        self.background = threading.Thread(target=loop, daemon=True)
        self.background.start()

    def close(self):
        self.stop.set()
        self.worker_stop.set()
        self.file_events.close()
        if self.worker:
            self.worker.join(timeout=30)
        if self.background:
            self.background.join(timeout=5)
        self.devices.close()
        self.workspace.close()
