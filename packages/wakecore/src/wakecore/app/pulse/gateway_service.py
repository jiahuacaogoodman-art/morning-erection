"""Coordinate saved Gateway configuration with Pi, without owning an agent loop.

Connection checks are read-only Pi requests. Their receipts survive page changes;
conversation acceptance never waits for the model request to finish.
"""
import threading
import uuid

from wakecore.adapters.agent_runtime.credentials import CredentialStoreError
from wakecore.adapters.agent_runtime.native import NativePiError

from .gateways import GatewayError


class GatewayService:
    def __init__(self, store, conversation, on_change):
        self.store, self.native, self.on_change = store, conversation, on_change
        self.apply_error = None
        self.test_errors = {}
        self.test_hosts = {}

    def runtime(self, desired_revision=None):
        host = self.native.host
        process = host.process
        if not process or process.poll() is not None:
            if self.apply_error:
                return {"state": "unavailable", "selected": None, "message": self.apply_error}
            return {"state": "not_started", "selected": None, "message": "配置已保存，启动 Pi 时载入。"}
        try:
            value = host.control("gateway_status", timeout=5)
            pending = value["revision"] != (self.store.value["revision"] if desired_revision is None else desired_revision)
            return {**value, "state": "pending" if pending else "applied", "message": self.apply_error if pending else None}
        except (NativePiError, OSError):
            return {"state": "unavailable", "selected": None, "message": "Pi 连接状态暂不可用。配置已保留，可以重新应用。"}

    def public(self):
        result = self.store.public()
        result["runtime"] = self.runtime(result["revision"])
        result["test_errors"] = dict(self.test_errors)
        return result

    def _apply(self, *, select_active=False):
        host = self.native.host
        host.wait_ready()
        applied = host.update_gateways(select_active=select_active)
        if not applied or applied.get("revision") != self.store.value["revision"]:
            raise NativePiError("Pi 未确认当前配置版本。")
        if hasattr(self.native, 'sync_gateways'):
            self.native.sync_gateways()
        self.apply_error = None
        # Optional extensions failing to reconfigure must not undo a valid Pi setup.
        try:
            self.on_change()
        except Exception:
            return "Pi 已更新；后台扩展未能同步连接，请检查扩展状态。"
        return None

    def ensure_applied(self):
        status = self.runtime()
        if status["state"] == "not_started":
            self.native.host.wait_ready()
        elif status["state"] != "applied":
            self.native.assert_idle()
            self._apply()

    def assert_prompt_ready(self):
        """Check the selected profile; a native provider remains entirely Pi's concern."""
        with self.native.operations:
            self.ensure_applied()
            status = self.runtime()
            if status.get("selectionError"):
                raise GatewayError("gateway_selection_unavailable", status["selectionError"])
            selected = status.get("selected") or {}
            if selected.get("profile_id"):
                item = next((p for p in self.store.public()["items"] if p["id"] == selected["profile_id"]), None)
                if item and not item["has_credentials"]:
                    raise GatewayError("credentials_unavailable", "当前 Gateway 的凭据无法从系统安全存储读取，请更新凭据后继续。")

    def _cancel_stale(self):
        with self.store.lock:
            running = [dict(t) for t in self.store.value["tests"] if t["state"] == "running"]
            revisions = {p["id"]: p["revision"] for p in self.store.value["profiles"]}
        for item in running:
            if revisions.get(item["profile_id"]) != item["revision"]:
                try:
                    self.store.finish_test(item["id"], state="superseded")
                except OSError:
                    self.test_errors[item["profile_id"]] = "配置已保存，但旧测试的状态未能保存。旧版本结果不会用于当前配置。"
                finally:
                    self._cancel_remote(item["id"])

    def _cancel_remote(self, key):
        host = self.test_hosts.get(key) or self.native.host
        if host.process and host.process.poll() is None:
            try:
                host.control("cancel_gateway_test", {"requestId": key}, timeout=5)
            except (NativePiError, OSError):
                pass  # the persistent cancelled receipt still fences off any late reply

    def action(self, body, request_id=None):
        if not isinstance(body, dict):
            raise GatewayError("invalid_body", "Gateway 操作需要有效的配置对象。")
        action = body.get("action", "save")
        if action == "cancel_test":
            key = body.get("test_id")
            item = self.store.finish_test(key, state="cancelled")
            if item is None:
                raise GatewayError("test_not_found", "没有找到这次连接测试，请刷新。")
            self._cancel_remote(key)
            return {**self.public(), "message": "连接测试已取消。"}
        if action == "test":
            # This lock covers acceptance only, never the remote model request.
            with self.native.operations:
                self.ensure_applied()
                profile = self.store.profile(body.get("id"))
                key = request_id or uuid.uuid4().hex
                if not isinstance(key, str) or not 1 <= len(key) <= 160:
                    raise GatewayError("invalid_test_id", "连接测试标识无效。")
                receipt, created = self.store.begin_test(profile["id"], key)
                if created:
                    host = self.native.host
                    self.test_hosts[receipt['id']] = host
                    threading.Thread(target=self._test, args=(receipt, host), daemon=True).start()
            return {**self.public(), "test": receipt}
        with self.native.operations:
            self.native.assert_idle()
            if action == "save":
                self.store.save({k: v for k, v in body.items() if k != "action"})
            elif action == "activate":
                self.store.activate(body.get("id"), expected_revision=body.get("expected_revision"))
            elif action in ("delete", "delete_credentials"):
                self.store.delete(body.get("id"), credentials_only=action == "delete_credentials", expected_revision=body.get("expected_revision"))
            elif action != "apply":
                raise GatewayError("unknown_action", "无法识别的 Gateway 操作。")
            self._cancel_stale()
            warning = None
            try:
                warning = self._apply(select_active=action == "activate" or bool(body.get("activate")))
            except (NativePiError, CredentialStoreError, GatewayError, OSError):
                self.apply_error = "配置已安全保存，但尚未应用到 Pi。请重新应用连接；无需重新输入凭据。"
            result = self.public()
            # A transport timeout can happen after Pi applied the configuration. Read the
            # revision back before claiming a failure; never blindly roll credentials back.
            result["message"] = (self.apply_error if result["runtime"]["state"] != "applied" else warning) or (
                "凭据已删除，配置已保留。" if action == "delete_credentials" else "Gateway 已移除。" if action == "delete"
                else "连接配置已应用到 Pi。" if action in ("activate", "apply") else "已安全保存并应用到 Pi。请进行连接测试。")
            return result

    def _test(self, receipt, host=None):
        key = receipt["id"]
        try:
            result = (host or self.native.host).control("test_gateway", {"id": receipt["profile_id"], "revision": receipt["revision"], "requestId": key}, timeout=55)
        except (NativePiError, CredentialStoreError, OSError):
            result = {"ok": False, "category": "runtime", "message": "Pi 连接测试未完成，请检查运行时状态后重新测试。", "tested_at": self.store._now()}
        try:
            self.store.finish_test(key, result)
            self.test_errors.pop(receipt["profile_id"], None)
        except OSError:
            self.test_errors[receipt["profile_id"]] = "测试结果未能保存，请检查磁盘状态。未标记为连接成功。"
        finally:
            self.test_hosts.pop(key, None)
