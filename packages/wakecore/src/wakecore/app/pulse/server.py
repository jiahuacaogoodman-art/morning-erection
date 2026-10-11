"""Local Agent/Pulse API and built-frontend serving (ADR 0009). No browser SDK imports."""
import hashlib
import hmac
import json
import mimetypes
import secrets
import threading
import time
import uuid
from datetime import timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from wakecore.app.api.wsgi import TokenAuth, WakeCoreAPI
from wakecore.kernel.commands import setup as setup_cmd
from wakecore.kernel.commands import tasks as task_cmd
from wakecore.kernel.decision.profiles import condition_holds
from wakecore.kernel.domain.canonical import digest
from wakecore.kernel.domain.errors import NotFound, SchemaMismatch, VersionConflict
from wakecore.kernel.domain.model import IdempotencyRecord, TriggerRecord
from wakecore.kernel.locks import tx
from wakecore.kernel.service import KernelService, Principal, Response

RULES = {"change", "gt", "gte", "lt", "lte", "eq", "contains", "present", "absent"}


def page_origin(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        _ = parsed.port
    except (ValueError, TypeError, AttributeError):
        raise SchemaMismatch("请输入不含密码的 http 或 https 页面链接") from None
    return f"{parsed.scheme}://{parsed.netloc}"


def scalar_extractor(selector, value_type):
    if not isinstance(selector, str) or not 0 < len(selector) <= 500:
        raise SchemaMismatch("请先选择要观察的内容")
    if value_type not in ("text", "number", "exists"):
        raise SchemaMismatch("读取类型应为文字、数值或是否出现")
    column = {"field": "value", "selector": "" if selector == "html" else selector, "type": value_type}
    if value_type == "number":
        column.update(pattern=r"[-+]?\d[\d,]*(?:\.\d+)?", group=0)
    return {"list": {"item": "html"}, "ready": "body" if value_type == "exists" else selector,
            "key_value": "page", "columns": [column],
            "login": {"url_contains": ["/login", "/signin", "/sign-in"], "selector": 'input[type="password"]'}}


class PulseAPI(WakeCoreAPI):
    def __init__(self, kernel, auth, *, client=None, demo=None, interpreter=None, dialogue=None, coding_runtime=None, settings=None, native_host=None, gateways=None):
        super().__init__(KernelService(kernel.ctx), auth)
        self.kernel, self.client, self.demo = kernel, client, demo
        self._runtime_cache = (0, {})
        from .agent import ConversationalAgent

        self.agent = ConversationalAgent(self, interpreter, dialogue, coding_runtime)
        from .native_sessions import NativeSessions

        self.native = NativeSessions(native_host) if native_host else None
        self.gateways = gateways
        from .gateway_service import GatewayService
        self.gateway_service = GatewayService(gateways, self.native, lambda: getattr(self, "gateway_changed", lambda: None)()) if gateways and self.native else None
        self.settings = settings
        self._add("POST", r"/pulse/chat", self._chat)
        self._add("GET", r"/pulse/gateways", self._get_gateways)
        self._add("POST", r"/pulse/gateways", self._gateway_action)
        self._add("POST", r"/pulse/pi", self._pi_command)
        self._add("POST", r"/pulse/pi/control", self._pi_control)
        self._add("POST", r"/pulse/pi/interaction", self._pi_interaction)
        self._add("GET", r"/pulse/state", self._state)
        self._add("GET", r"/pulse/settings", self._get_settings)
        self._add("POST", r"/pulse/settings", self._post_settings)
        self._add("POST", r"/pulse/inspect", self._inspect)
        self._add("POST", r"/pulse/login", self._login)
        self._add("POST", r"/pulse/draft", self._draft)
        self._add("POST", r"/pulse/watches/(?P<id>[^/]+)/start", self._start)
        self._add("POST", r"/pulse/watches/(?P<id>[^/]+)/settings", self._settings)
        self._add("POST", r"/pulse/watches/(?P<id>[^/]+)/end", self._end)
        self._add("POST", r"/pulse/demo", self._demo_control)

    def metadata(self):
        return {"product": "Morning Erection", "demo": bool(self.demo), "platform_available": bool(getattr(self, "platform", None)),
                "demo_url": self.demo.url if self.demo else None, "runtime_configured": bool(self.client),
                "understanding_available": bool(self.agent.interpreter and self.agent.interpreter.configured),
                "coding_available": self.native.host.available if self.native else bool(self.agent.coding_runtime and self.agent.coding_runtime.configured),
                "workspace": self.native.host.info.get("workspace", str(self.native.host.workspace)) if self.native else str(self.agent.coding_runtime.workspace) if self.agent.coding_runtime else None}

    def _native_result(self, operation):
        from wakecore.adapters.agent_runtime.credentials import CredentialStoreError
        from wakecore.adapters.agent_runtime.native import NativePiError

        from .gateways import GatewayError

        try:
            return Response(200, operation())
        except OSError:
            return Response(422, {"error": {"code": "storage_unavailable", "message": "无法保存配置，请检查磁盘和文件权限。原配置已保留。"}})
        except (GatewayError, NativePiError, CredentialStoreError) as exc:
            message = self.native.host.redact(str(exc)) if self.native else str(exc)
            if message == "Nothing to compact (session too small)":
                message = "当前会话较短，Pi 暂不需要整理上下文。"
            return Response(422, {"error": {"code": getattr(exc, "code", "PI_OPERATION_FAILED"), "message": message}})

    def _chat(self, req, principal, args):
        if self.native:
            def prompt():
                return self.native.prompt(req.json(), req.header("Idempotency-Key"), prepare=self.gateway_service.assert_prompt_ready if self.gateway_service else None)
            return self._native_result(prompt)
        return Response(200, self.agent.reply(principal, req.json(), req.header("Idempotency-Key")))

    def _get_gateways(self, req, principal, args):
        return Response(200, self.gateway_service.public() if self.gateway_service else self.gateways.public() if self.gateways else {"items": [], "active_id": None, "secure_storage": "unavailable"})

    def _gateway_action(self, req, principal, args):
        if not self.gateways or not self.native:
            raise SchemaMismatch("Gateway 存储尚未初始化")
        return self._native_result(lambda: self.gateway_service.action(req.json(), req.header("Idempotency-Key")))

    def _pi_command(self, req, principal, args):
        if not self.native:
            raise SchemaMismatch("Pi 原生运行时尚未连接")
        body = req.json()
        command = body.get("command")
        if getattr(self, "platform", None) and self.platform.background_scope is not None and command in ("set_model", "cycle_model"):
            raise SchemaMismatch("后台观察正在使用已确认的模型服务，请先停止或等待本轮结束，再切换模型。")
        commands = {"prompt", "steer", "follow_up", "abort", "clear_queue", "new_session", "get_state", "get_messages",
                    "set_model", "cycle_model", "get_available_models", "set_thinking_level", "cycle_thinking_level", "get_available_thinking_levels",
                    "set_steering_mode", "set_follow_up_mode", "compact", "set_auto_compaction", "set_auto_retry", "abort_retry", "bash", "abort_bash",
                    "get_session_stats", "export_html", "switch_session", "fork", "clone", "get_fork_messages", "get_entries", "get_tree",
                    "get_last_assistant_text", "set_session_name", "get_commands"}
        if command not in commands or not isinstance(body.get("params", {}), dict):
            raise SchemaMismatch("无法识别的 Pi 操作")
        def execute():
            if command in ("prompt", "steer", "follow_up") and self.gateway_service:
                return self.native.command(command, body.get("params", {}), prepare=self.gateway_service.assert_prompt_ready)
            return self.native.command(command, body.get("params", {}))
        return self._native_result(execute)

    def _pi_control(self, req, principal, args):
        if not self.native:
            raise SchemaMismatch("Pi 原生运行时尚未连接")
        body = req.json()
        if getattr(self, "platform", None) and self.platform.background_scope is not None and body.get("command") in ("reload", "package", "import_session", "update_native_preferences", "set_workspace_trust"):
            raise SchemaMismatch("请先停止或等待当前后台观察完成，再修改运行时资源。")
        if body.get("command") not in {"start_agent", "resources", "sessions", "context", "reload", "set_tools", "providers", "models", "auth_login", "auth_logout", "auth_cancel", "package", "navigate_tree", "import_session", "native_preferences", "update_native_preferences", "set_workspace_trust", "stop_session", "set_concurrency"}:
            raise SchemaMismatch("无法识别的 Pi 设置")
        def execute():
            if body['command'] == 'sessions':
                return self.native.sessions()
            if body['command'] == 'stop_session':
                return self.native.stop_session(body.get('params', {}).get('sessionId'))
            if body['command'] == 'set_concurrency':
                return self.native.set_limit(body.get('params', {}).get('limit'))
            if body["command"] in ("update_native_preferences", "set_workspace_trust"):
                with self.native.operations:
                    self.native.assert_idle()
                    return self.native.host.control(body["command"], body.get("params", {}), timeout=300)
            return self.native.host.control(body["command"], body.get("params", {}), timeout=300)
        return self._native_result(execute)

    def _pi_interaction(self, req, principal, args):
        if not self.native:
            raise SchemaMismatch("Pi 原生运行时尚未连接")
        body = req.json()
        return self._native_result(lambda: self.native.host.answer_ui(body.get("id"), body.get("answer", {})) or {"answered": True})

    def _runtime(self):
        if time.monotonic() - self._runtime_cache[0] < 10:
            return self._runtime_cache[1]
        try:
            data = {"available": True, **self.client.health()} if self.client else {"available": False}
        except Exception:
            data = {"available": False}
        # No endpoint keys, tokens or session profile directories are returned.
        result = {k: data.get(k) for k in ("available", "model_configured", "model_egress")}
        try:
            result["sessions"] = self.client.sessions() if self.client and result["available"] else []
        except Exception:
            result["sessions"] = []
        self._runtime_cache = (time.monotonic(), result)
        return result

    def _state(self, req, principal, args):
        # `dialogue_since` lets the client fetch only what it has not seen. It is optional: an
        # absent or unparsable value falls back to the full history so a stale client or a
        # hand-typed URL still gets a complete answer.
        since = None
        raw_since = parse_qs(req.environ.get("QUERY_STRING") or "").get("dialogue_since", [None])[0]
        if raw_since:
            try:
                since = max(0, int(raw_since))
            except ValueError:
                since = None
        items = [self.service.pulse(principal, t["task_id"]) for t in self.service.list_tasks(principal)
                 if t["lifecycle"] not in ("DRAFT", "CANCELLED", "EXPIRED")][:50]
        runtime = self._runtime()
        states = {s["session_ref"]: s["state"] for s in runtime.get("sessions", [])}
        for item in items:
            ref = self.kernel.secrets.resolve(principal.tenant_id, item["task_id"])
            item["session_state"] = states.get(ref, "unknown")
        native = self.native.snapshot() if self.native else None
        dialogue, revision = (native["dialogue"], native["revision"]) if native else self.agent.store.messages_since(since)
        run = self.agent.store.snapshot().get("run")
        return Response(200, {"items": items, "dialogue": dialogue, "dialogue_revision": revision,
                              "dialogue_partial": since is not None and not native, "run": run,
                              "agent_busy": bool(native.get('runtime_busy') or native["state"] and (native["state"].get("isStreaming") or native["state"].get("isCompacting"))) if native else self.agent.store.turn_lock.locked(),
                              "pi": {key: value for key, value in native.items() if key != "dialogue"} if native else None,
                              "runtime": {k: v for k, v in runtime.items() if k != "sessions"}, **self.metadata(),
                              "server_time": self.kernel.ctx.clock.utc_now().isoformat()})

    def polling_defaults(self):
        # New watches start from the user's chosen cadence; an explicit value in a request wins.
        value = self.settings.all() if self.settings else {}
        return {"min_seconds": value.get("polling_min_seconds", 10), "max_seconds": value.get("polling_max_seconds", 120)}

    def _get_settings(self, req, principal, args):
        value = self.settings.all() if self.settings else {}
        # Read-only facts the settings page shows. `model_egress` is deliberately left out: it
        # names the relay host, and the page only needs to know whether a model is configured.
        runtime = self._runtime()
        value["_model_configured"] = bool(runtime.get("model_configured"))
        value["_data_dir"] = str(self.settings.path.parent) if self.settings and self.settings.path else None
        return Response(200, value)

    def _post_settings(self, req, principal, args):
        if not self.settings:
            raise SchemaMismatch("设置存储尚未初始化")
        from .settings import SettingsError
        try:
            updated = self.settings.update(req.json())
        except SettingsError as exc:
            raise SchemaMismatch(str(exc)) from exc
        return Response(200, updated)

    def _inspect(self, req, principal, args):
        body = req.json()
        url = body.get("url")
        origin = page_origin(url)
        if not self.client:
            raise SchemaMismatch("请先连接浏览器运行时，再读取页面")
        session = body.get("session_ref") or default_session(url)
        data = self.client._call("POST", "/v1/inspect", {"url": url, "origins": [origin], "session_ref": session})
        return Response(200, data)

    def _draft(self, req, principal, args):
        body = req.json()
        idem = req.header("Idempotency-Key")
        request_digest = digest({"op": "pulse.draft", "request": body})
        if idem:
            with tx(self.kernel.ctx.store) as repo:
                prior = repo.get(IdempotencyRecord, tenant_id=principal.tenant_id, principal=principal.subject, idem_key=idem)
                if prior:
                    if prior.request_digest != request_digest:
                        from wakecore.kernel.domain.errors import IdempotencyConflict

                        raise IdempotencyConflict("Idempotency-Key was used for a different request")
                    session = body.get("session_ref") or default_session(body.get("url"))
                    self.kernel.secrets.put(principal.tenant_id, prior.response["task_id"], session)
                    if hasattr(self, "save_session"):
                        self.save_session(principal.tenant_id, prior.response["task_id"], session)
                    return Response(prior.status_code, prior.response, replayed=True)
        url, prompt = body.get("url"), str(body.get("prompt") or "")[:2000]
        origin = page_origin(url)
        label = str(body.get("label") or "网页驻守")[:80]
        rule = body.get("rule")
        if rule not in RULES:
            raise SchemaMismatch("请确认你希望等什么：内容变化、数值条件、文字条件或出现/消失")
        value_type = body.get("value_type") or "text"
        threshold = body.get("threshold", "")
        if rule in ("gt", "gte", "lt", "lte") or (rule == "eq" and value_type == "number"):
            try:
                threshold = float(threshold)
                if not (-1e15 < threshold < 1e15):
                    raise ValueError
            except (ValueError, TypeError):
                raise SchemaMismatch("请填写有效的数值条件") from None
        if rule in ("present", "absent"):
            value_type, threshold = "exists", rule == "present"
        demo_course = bool(body.get("demo_course") and self.demo and origin == page_origin(self.demo.url))
        extractor = self.demo.extractor if demo_course else body.get("extractor") or scalar_extractor(body.get("selector"), value_type)
        record_id = str(body.get("record_id") or "page")[:200]
        field = str(body.get("field") or "value")[:100]
        field_label = str(body.get("field_label") or "内容")[:30]
        if demo_course:
            record_id, field, field_label = "SE", "seats", "余量"
        session = str(body.get("session_ref") or default_session(url))
        if not self.client:
            raise SchemaMismatch("浏览器运行时未连接")
        checked = self.client.validate_extractor(extractor)
        if not checked.get("ok"):
            raise SchemaMismatch("读取方式无效，请重新选择内容或修正高级配置")
        result = self.client.observe(session_ref=session, url=url, origins=[origin], extractor=extractor)
        if result.get("outcome") != "SUCCESS" or record_id not in (result.get("records") or {}):
            raise SchemaMismatch("当前没有完整读到目标。请确认登录状态、页面是否加载完成及观察内容")
        if field not in result["records"][record_id]:
            raise SchemaMismatch("目标字段尚未读到，请确认读取配置与目标字段一致")
        task_id = "pulse_" + uuid.uuid4().hex[:16]
        defaults = self.polling_defaults()
        minimum = body.get("min_seconds", defaults["min_seconds"])
        maximum = body.get("max_seconds", max(minimum, defaults["max_seconds"]) if isinstance(minimum, int) else defaults["max_seconds"])
        if not isinstance(minimum, int) or isinstance(minimum, bool) or not 2 <= minimum <= 3600:
            raise SchemaMismatch("最短检查间隔应为 2 到 3600 秒")
        if not isinstance(maximum, int) or isinstance(maximum, bool) or not minimum <= maximum <= 86400:
            raise SchemaMismatch("最长检查间隔应不小于最短间隔")
        op = "eq" if rule in ("present", "absent") else rule
        condition = [] if rule == "change" else [{"field": field, "op": op, "value": threshold, "kind": "condition_met"}]
        symbols = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "eq": "=", "contains": "包含"}
        description = "内容发生变化" if rule == "change" else "出现" if rule == "present" else "消失" if rule == "absent" \
            else f"{field_label} {symbols[rule]} {threshold:g}" if isinstance(threshold, float) \
            else f"{field_label} {symbols[rule]} {threshold}"
        scope = {"url": url, "origins": [origin], "extractor": extractor, "record_ids": [record_id],
                 "label": label, "pulse_field": field, "pulse_field_label": field_label, "condition_label": description,
                 "polling": {"min_seconds": minimum, "max_seconds": maximum,
                             "error_max_seconds": max(900, maximum), "jitter_percent": 10}}
        recipe = None
        if body.get("demo_action") and self.demo and origin == page_origin(self.demo.url):
            recipe = self.demo.recipe(record_id)
        elif body.get("action"):
            action = body["action"]
            runtime = self._runtime()
            if not runtime.get("model_configured"):
                raise SchemaMismatch("代操作需要配置可执行 Computer Use 的模型。目前可以先驻守并通知")
            if not isinstance(action, dict) or not isinstance(action.get("goal"), str) or not action.get("verify"):
                raise SchemaMismatch("代操作必须同时写明具体操作和完成后的验证条件")
            recipe = {"tool_id": "browser.cua", "capability": "browser.operate", "payload": {
                "goal": action["goal"], "start_url": url, "verify": action["verify"]},
                "data_egress": [runtime["model_egress"]]}
            from wakecore.kernel.domain.schema import validate
            from wakecore.kernel.policy.origins import url_in_scope

            tool = self.kernel.ctx.tools["browser.cua"]
            validate(tool.descriptor.input_schema, recipe["payload"])
            verification = action["verify"]
            if verification["record_id"] != record_id or not url_in_scope(verification.get("url") or url, [origin]):
                raise SchemaMismatch("操作结果必须在当前授权页面和目标范围中确认")
            if not verification["conditions"] or any(c["field"] not in result["records"][record_id] for c in verification["conditions"]):
                raise SchemaMismatch("请先在读取配置中包含操作完成后的验证字段")
        egress = ["model:local-watch-plan", *recipe.get("data_egress", [])] if recipe else []
        semantic = body.get("semantic_condition")
        if semantic:
            expected = self.agent.interpreter.data_egress_target if self.agent.interpreter else None
            if not expected or body.get("semantic_egress") != expected:
                raise SchemaMismatch("语义判断模型尚未授权")
            egress = sorted(set([*egress, "model:local-watch-plan", expected]))
            description = body.get("wait_summary") or semantic
        if recipe:
            tool = self.kernel.ctx.tools.get(recipe["tool_id"])
            policy = self.kernel.ctx.system_policy
            if not tool or recipe["capability"] not in policy.allowed_capabilities or not set(recipe.get("data_egress", [])) <= policy.allowed_data_egress:
                raise SchemaMismatch("当前环境尚未允许这类网页操作或模型截图接收方，请先确认运行时配置")
            scope["action"] = recipe
        spec = {"schema_version": 1, "task_id": task_id, "root_task_id": task_id, "spec_version": 1,
                "purpose": prompt or f"等{label}：{description}", "source": {"binding_ref": task_id, "resource_scope": scope},
                "trigger": {"kind": "interval", "every_seconds": minimum, "timezone": "Asia/Shanghai",
                            "catchup_policy": "coalesce_latest"},
                "observation": {"completeness_required": "full_target_scope", "first_snapshot": "baseline_only" if rule == "change" else "notify_existing_results",
                                "ignore_fields": [], "target_key": "record_ids"},
                "completion": {"evaluator": "watch_forever", "evaluator_version": 1, "required_outputs": []},
                "decision": {"profile": "web_watch.v1", "params": {"conditions": condition, "watch_fields": [field],
                  "notify_on_change": rule == "change", "on_met": "plan" if recipe else "notify", "title": label + "有变化"}},
                "authority": {"grant_ref": task_id, "read_capabilities": ["web.observe"],
                              "write_capabilities": [recipe["capability"]] if recipe else [],
                              "notify_capabilities": ["inbox.notify_self"], "model_data_egress": egress},
                "limits": {"min_probe_interval_seconds": minimum, "max_steps_per_run": 8, "max_run_seconds": 120},
                "followup": {"enabled": False},
                "expires_at": (self.kernel.ctx.clock.utc_now() + timedelta(days=7)).isoformat()}
        if semantic:
            spec["decision"] = {"profile": "semantic_watch.v1", "params": {"condition": semantic,
                "watch_fields": [field], "on_met": "plan" if recipe else "notify", "title": label + "等待条件已满足"}}
            spec["observation"]["first_snapshot"] = "notify_existing_results"
            spec["limits"]["max_model_attempts_per_day"] = 12
        if body.get("wait_summary"):
            scope["condition_label"] = body["wait_summary"]
        if recipe and recipe["tool_id"] == "demo.course.enroll":
            spec["completion"] = {"evaluator": "all_targets_match", "evaluator_version": 1, "required_outputs": []}
            spec["decision"]["params"]["complete_when"] = [{"field": "selected", "op": "eq", "value": True}]
        def save(repo):
            setup_cmd.register_source_binding(repo, self.kernel.ctx, tenant_id=principal.tenant_id, owner=principal.subject,
                source_ref=task_id, connector_id="web.playwright", source_uri=url, resource_scope={"origins": [origin]},
                capabilities=["web.observe"], secret_ref=task_id)
            record = task_cmd.create_draft(repo, self.kernel.ctx, tenant_id=principal.tenant_id, principal=principal.subject, raw=spec)
            return {"task_id": task_id, "spec_digest": record.spec_digest, "spec_version": record.spec_version,
                    "label": label, "condition_label": description, "current": result["records"][record_id],
                    "action": recipe["payload"] if recipe else None,
                    "data_egress": recipe.get("data_egress", []) if recipe else [], "url": url,
                    "already_met": any(condition_holds(c, result["records"][record_id]) for c in condition)}
        created = self.service._command(principal, "pulse.draft", body, idem, save, status_code=201)
        saved_id = created.body["task_id"]
        self.kernel.secrets.put(principal.tenant_id, saved_id, session)
        # Opaque session references are durable in a local, private file, never in the task spec.
        if hasattr(self, "save_session"):
            self.save_session(principal.tenant_id, saved_id, session)
        return created

    def _start(self, req, principal, args):
        body, task_id = req.json(), args["id"]
        expected = body.get("spec_digest")
        if not isinstance(expected, str):
            raise SchemaMismatch("开始前需要确认当前驻守计划")

        def confirm(repo):
            rt = self.service._owned_task(repo, principal, task_id)
            spec = task_cmd.load_spec(repo, rt)
            if spec.digest() != expected:
                raise VersionConflict("驻守计划已更新，请重新确认")
            existing = repo.get(TriggerRecord, tenant_id=principal.tenant_id,
                                trigger_id=task_cmd.main_trigger_id(task_id))
            if existing:
                return {"task_id": task_id, "lifecycle": rt.lifecycle}
            setup_cmd.register_grant(repo, self.kernel.ctx, tenant_id=principal.tenant_id, principal=principal.subject,
                                     grant_ref=spec.authority.grant_ref,
                                     capabilities=list(spec.authority.requested_capabilities()),
                                     data_egress=list(spec.authority.model_data_egress),
                                     resource_scope={key: spec.source.resource_scope[key] for key in ("origins", "environment", "apps") if key in spec.source.resource_scope}, expires_at=spec.expires_at)
            rt = task_cmd.activate(repo, self.kernel.ctx, tenant_id=principal.tenant_id, principal=principal.subject,
                                   task_id=task_id, spec_version=spec.spec_version, expected_digest=expected)
            return {"task_id": task_id, "lifecycle": rt.lifecycle}
        response = self.service._command(principal, "pulse.start", {"task_id": task_id, "digest": expected},
                                          req.header("Idempotency-Key"), confirm)
        pending = self.agent.store.snapshot()["pending"]
        if pending and pending["task_id"] == task_id and not response.replayed:
            self.agent._append(self.agent._message("agent", "已开始。我会持续关注相关内容，符合你确认的要求时再处理。", type="started", task_id=task_id))
            self.agent.store.update(lambda data: data.update(pending=None))
        return response

    def _end(self, req, principal, args):
        """End a watch. The kernel already knows how to cancel a task; the local UI had no
        Pulse-level route to reach it, so "结束这个驻守" used to POST into a 404."""
        body, task_id = req.json(), args["id"]
        expected = body.get("expected_version")
        if isinstance(expected, bool) or not isinstance(expected, int):
            raise SchemaMismatch("结束前需要确认当前的驻守版本")
        response = self.service.cancel_task(principal, task_id, reason=str(body.get("reason") or "cancelled_by_user")[:200],
                                            expected_version=expected, idem_key=req.header("Idempotency-Key"))
        if not response.replayed:
            self.agent._append(self.agent._message("agent", "已经结束这个驻守。", type="cancelled", task_id=task_id))
        return response

    def _settings(self, req, principal, args):
        body, task_id = req.json(), args["id"]
        if "extractor" in body and not self.client.validate_extractor(body["extractor"]).get("ok"):
            raise SchemaMismatch("页面读取配置无效")
        def change(repo):
            rt = self.service._owned_task(repo, principal, task_id)
            spec = task_cmd.load_spec(repo, rt).to_dict()
            if body.get("spec_digest") != spec_digest(spec):
                raise VersionConflict("配置已变化，请重新打开设置")
            if rt.lifecycle not in ("ACTIVE", "PAUSED"):
                raise SchemaMismatch("当前任务已结束，请创建新的驻守")
            scope = spec["source"]["resource_scope"]
            for key in ("extractor", "polling"):
                if key in body:
                    scope[key] = body[key]
            spec["spec_version"] += 1
            spec["trigger"]["every_seconds"] = scope["polling"]["min_seconds"]
            spec["limits"]["min_probe_interval_seconds"] = scope["polling"]["min_seconds"]
            created = task_cmd.create_draft(repo, self.kernel.ctx, tenant_id=principal.tenant_id,
                                            principal=principal.subject, raw=spec)
            updated = task_cmd.activate(repo, self.kernel.ctx, tenant_id=principal.tenant_id,
                principal=principal.subject, task_id=task_id, spec_version=created.spec_version,
                expected_digest=created.spec_digest, expected_version=rt.version)
            if rt.lifecycle == "PAUSED":
                task_cmd.pause(repo, self.kernel.ctx, tenant_id=principal.tenant_id, principal=principal.subject,
                               task_id=task_id, expected_version=updated.version)
                return {"task_id": task_id, "lifecycle": "PAUSED", "version": updated.version + 1}
            return {"task_id": task_id, "lifecycle": updated.lifecycle, "version": updated.version}
        return self.service._command(principal, "pulse.settings", {"task_id": task_id, **body},
                                      req.header("Idempotency-Key"), change)

    def _login(self, req, principal, args):
        body = req.json()
        url = body.get("url")
        session = body.get("session_ref") or default_session(url)
        if body.get("task_id"):
            with tx(self.kernel.ctx.store) as repo:
                rt = self.service._owned_task(repo, principal, body["task_id"])
                url = task_cmd.load_spec(repo, rt).source.resource_scope["url"]
            session = self.kernel.secrets.resolve(principal.tenant_id, rt.source_ref)
        page_origin(url)
        if not self.client:
            raise SchemaMismatch("浏览器运行时未连接")
        result = self.client._call("POST", "/v1/sessions/login", {
            "session_ref": session, "url": url, "finish": bool(body.get("finish"))})
        self._runtime_cache = (0, {})
        return Response(200, result)

    def _demo_control(self, req, principal, args):
        if not self.demo:
            raise NotFound("demo is disabled")
        self.demo.control(req.json())
        return Response(200, {"ok": True})


def spec_digest(spec):
    from wakecore.kernel.domain.taskspec import parse_task_spec

    return parse_task_spec(spec).digest()


def default_session(url):
    return "pulse_" + hashlib.sha256(page_origin(url).encode()).hexdigest()[:16]


class LocalPulseApp:
    """Loopback browser session facade; this is deliberately not remote authentication."""
    def __init__(self, kernel, *, client=None, demo=None, interpreter=None, coding_runtime=None, native_host=None, gateways=None, dist=None, allowed_hosts=None, state_dir=None, platform_enabled=False):
        self.token = secrets.token_urlsafe(32)
        self.principal = Principal("pulse_local", "user:local")
        from .agent import DialogueStore
        from .settings import SettingsStore

        dialogue = DialogueStore(Path(state_dir) / "agent-dialogue.json" if state_dir else None)
        if native_host:
            native_host.legacy_messages = dialogue.snapshot()["messages"]
        settings = SettingsStore(Path(state_dir) / "settings.json" if state_dir else None)
        self.api = PulseAPI(kernel, TokenAuth.from_tokens({self.token: self.principal}), client=client, demo=demo,
                            interpreter=interpreter, dialogue=dialogue, coding_runtime=coding_runtime, settings=settings, native_host=native_host, gateways=gateways)
        self.dist = Path(dist).resolve() if dist else None
        self.allowed_hosts = set(allowed_hosts or ("127.0.0.1:8788", "localhost:8788", "127.0.0.1:5173", "localhost:5173"))
        self.session_path = Path(state_dir) / "session-refs.json" if state_dir else None
        self.sessions = {}
        self.session_lock = threading.RLock()
        if self.session_path and self.session_path.exists():
            self.sessions = json.loads(self.session_path.read_text())
            for tenant, refs in self.sessions.items():
                for ref, value in refs.items():
                    kernel.secrets.put(tenant, ref, value)
        self.api.save_session = self.save_session
        self.platform = None
        if platform_enabled and state_dir:
            from .platform import Platform

            self.platform = Platform(self.api, self.principal, state_dir)
            self.api.platform = self.platform
            if native_host:
                native_host.platform_tools = True
        if native_host and (client or self.platform):
            from .native_watch import NativeWatchTools, watch_schema

            self.api.native.configure_tools(lambda: NativeWatchTools(self.api, self.principal))
            native_host.watch_proposal_schema = watch_schema()

    def save_session(self, tenant, ref, session):
        with self.session_lock:
            self.sessions.setdefault(tenant, {})[ref] = session
            if self.session_path:
                self.session_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                temp = self.session_path.with_suffix(".tmp")
                temp.write_text(json.dumps(self.sessions))
                temp.chmod(0o600)
                temp.replace(self.session_path)

    def __call__(self, environ, start_response):
        host = environ.get("HTTP_HOST", "")
        if host not in self.allowed_hosts:
            return self._json(start_response, 403, {"error": {"message": "不允许的本地地址"}})
        path = environ.get("PATH_INFO", "/")
        cookie_name = "pulse_session_" + hashlib.sha256(host.encode()).hexdigest()[:12]
        if path == "/api/bootstrap" and environ.get("REQUEST_METHOD") == "GET":
            origin = environ.get("HTTP_ORIGIN")
            if origin and origin != f"http://{host}":
                return self._json(start_response, 403, {"error": {"message": "不允许的来源"}})
            return self._json(start_response, 200, self.api.metadata(), cookie=cookie_name)
        if path.startswith("/api/"):
            cookies = SimpleCookie()
            try:
                cookies.load(environ.get("HTTP_COOKIE", ""))
            except Exception:
                pass
            got = cookies.get(cookie_name)
            if not got or not hmac.compare_digest(got.value, self.token):
                return self._json(start_response, 401, {"error": {"code": "UNAUTHENTICATED", "message": "请重新连接本地会话"}})
            if environ.get("REQUEST_METHOD") != "GET":
                if environ.get("HTTP_ORIGIN") != f"http://{host}" or environ.get("HTTP_X_PULSE_CLIENT") != "1":
                    return self._json(start_response, 403, {"error": {"message": "不允许的来源"}})
            local = dict(environ)
            local["HTTP_AUTHORIZATION"] = f"Bearer {self.token}"
            route = path.removeprefix("/api")
            if route.startswith("/pulse/pi/export/") and environ.get("REQUEST_METHOD") == "GET":
                export = self.api.native.exports.get(route.rsplit("/", 1)[-1]) if self.api.native else None
                if not export or not Path(export).is_file():
                    return self._json(start_response, 404, {"error": {"message": "导出链接已失效，请重新导出。"}})
                content = Path(export).read_bytes()
                start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(content))),
                    ("Content-Disposition", "attachment; filename*=UTF-8''" + quote(Path(export).name)),
                    ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), ("Content-Security-Policy", "sandbox")])
                return [content]
            if route in ("/pulse/chat", "/pulse/pi"):
                local["wakecore.max_body"] = 16 * 1024 * 1024
            local["PATH_INFO"] = route if route.startswith("/pulse/") else "/v1" + route
            if self.api.native:
                from wakecore.adapters.agent_runtime.native import NativePiError
                try:
                    with self.api.native.request_scope(environ.get('HTTP_X_MORNING_SESSION')):
                        return self.api(local, start_response)
                except NativePiError as exc:
                    return self._json(start_response, 409, {'error': {'code': 'session_unavailable', 'message': str(exc)}})
            return self.api(local, start_response)
        if environ.get("REQUEST_METHOD") == "GET" and self.dist:
            file = (self.dist / path.lstrip("/")).resolve()
            if not file.is_relative_to(self.dist):
                return self._json(start_response, 404, {})
            if not file.is_file():
                file = self.dist / "index.html"
            if file.is_file():
                data = file.read_bytes()
                start_response("200 OK", [("Content-Type", mimetypes.guess_type(file.name)[0] or "application/octet-stream"),
                                          ("Content-Length", str(len(data))), ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
                                          ("Content-Security-Policy", "frame-ancestors 'none'; object-src 'none'; base-uri 'self'")])
                return [data]
        return self._json(start_response, 404, {"error": {"message": "请先构建 Pulse 前端"}})

    def _json(self, start_response, status, value, *, cookie=None):
        data = json.dumps(value, ensure_ascii=False).encode()
        headers = [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(data))),
                   ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")]
        if cookie:
            headers.append(("Set-Cookie", f"{cookie}={self.token}; HttpOnly; SameSite=Strict; Path=/api"))
        start_response(f"{status} { {200: 'OK', 401: 'Unauthorized', 403: 'Forbidden', 404: 'Not Found'}.get(status, 'Error')}", headers)
        return [data]
