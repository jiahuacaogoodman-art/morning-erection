"""Gateway profiles contain public configuration; credentials live in the OS vault."""
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from wakecore.adapters.agent_runtime.credentials import CredentialStoreError, MacKeychain

APIS = ("openai-responses", "openai-completions", "anthropic-messages")


class GatewayError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def base_url(value):
    if not isinstance(value, str) or len(value) > 2000:
        raise GatewayError("invalid_address", "请填写 Gateway 的 API 根地址")
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise GatewayError("invalid_address", "Gateway 地址格式不正确") from None
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise GatewayError("invalid_address", "地址需要是 HTTP(S) API 根地址，不能包含凭据、查询参数或片段")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise GatewayError("https_required", "远程 Gateway 需要使用 HTTPS")
    if parsed.path.endswith(("/responses", "/chat/completions", "/messages", "/models")):
        raise GatewayError("root_address_required", "请填写 API 根地址，而不是具体的请求接口")
    return value


def _text(value, field, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise GatewayError("invalid_field", f"请填写有效的{field}")
    return value.strip()


class GatewayStore:
    def __init__(self, state_dir, *, vault=None):
        self.path = Path(state_dir) / "gateways.json"
        self.lock = threading.RLock()
        self.value = {"version": 1, "installation_id": uuid.uuid4().hex, "profiles": [], "active_id": None, "credentials": {}}
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text())
                if not isinstance(saved, dict) or saved.get("version") != 1 or not isinstance(saved.get("profiles"), list) or not isinstance(saved.get("installation_id"), str) or not isinstance(saved.get("credentials"), dict):
                    raise ValueError
                self.value = saved
            except (OSError, ValueError, TypeError):
                raise GatewayError("configuration_invalid", "Gateway 配置文件无法读取，原文件已保留") from None
        self.value.setdefault("revision", 0)
        self.value.setdefault("tests", [])
        # A read-only connection check may be retried by a person, never silently on boot.
        interrupted = False
        for test in self.value["tests"]:
            if test["state"] == "running":
                test.update(state="interrupted", finished_at=self._now(), message="服务重启，连接测试已中断，请重新测试。")
                interrupted = True
        if interrupted:
            self._save()
        self.vault = vault
        self.vault_error = None
        if vault is None:
            try:
                self.vault = MacKeychain(self.value["installation_id"])
            except CredentialStoreError as exc:
                self.vault_error = str(exc)

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(self.value, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _vault(self):
        if self.vault is None:
            raise GatewayError("secure_store_unavailable", "系统凭据存储暂时不可用；凭据尚未保存")
        return self.vault

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @contextmanager
    def transaction(self):
        """Public metadata transaction; secret mutations retain their explicit vault rollback."""
        with self.lock:
            original = deepcopy(self.value)
            try:
                yield
                self._save()
            except Exception:
                self.value = original
                raise

    def check_revision(self, expected):
        if expected is not None and (type(expected) is not int or expected != self.value["revision"]):
            raise GatewayError("configuration_conflict", "连接配置已在别处更新。请重新载入配置后再修改；你的输入仍保留。")

    def _changed(self, provider=None):
        self.value["revision"] += 1
        if provider:
            for profile in self.value["profiles"]:
                if profile["provider_id"] == provider:
                    profile["revision"] += 1
                    profile.pop("last_test", None)

    @staticmethod
    def provider_id(profile_id):
        if not isinstance(profile_id, str) or not re.fullmatch(r"[a-f0-9]{32}", profile_id):
            raise GatewayError("invalid_profile", "Gateway 标识无效")
        return "gateway-" + profile_id

    def profile(self, profile_id=None):
        with self.lock:
            selected = profile_id or self.value["active_id"]
            result = next((p for p in self.value["profiles"] if p["id"] == selected), None)
            if result is None:
                raise GatewayError("gateway_not_configured", "请先添加并选择一个 Gateway")
            return deepcopy(result)

    def public(self):
        with self.lock:
            items = []
            for profile in self.value["profiles"]:
                item = deepcopy(profile)
                try:
                    credential = self._vault().read(profile["provider_id"])
                    present = bool(credential and (credential.get("key") or credential.get("access") or credential.get("refresh")))
                    state = "available" if present else "missing"
                except (GatewayError, CredentialStoreError):
                    present, state = False, "unavailable"
                item.update(has_credentials=present, credential_state=state)
                if not present:
                    item.pop("last_test", None)
                latest = next((t for t in reversed(self.value["tests"]) if t["profile_id"] == item["id"]), None)
                if latest:
                    item["connection_test"] = {**latest, "current": latest["revision"] == item["revision"] and present}
                items.append(item)
            return {"items": items, "active_id": self.value["active_id"], "revision": self.value["revision"],
                    "secure_storage": "macOS Keychain" if isinstance(self.vault, MacKeychain) else "configured" if self.vault else "unavailable"}

    def save(self, body):
        if not isinstance(body, dict):
            raise GatewayError("invalid_body", "Gateway 配置需要是对象。")
        allowed = {"id", "name", "base_url", "api", "model", "model_options", "request_format", "api_key", "activate", "expected_revision"}
        if set(body) - allowed:
            raise GatewayError("unknown_field", "Gateway 配置包含无法识别的字段")
        with self.lock:
            self.check_revision(body.get("expected_revision"))
            profile_id = body.get("id") or uuid.uuid4().hex
            provider = self.provider_id(profile_id)
            previous = next((p for p in self.value["profiles"] if p["id"] == profile_id), None)
            if body.get("id") and previous is None:
                raise GatewayError("gateway_not_found", "这个 Gateway 已不存在")
            old = previous or {}
            record = {"id": profile_id, "provider_id": provider,
                      "name": _text(body.get("name", old.get("name")), "名称", 80),
                      "base_url": base_url(body.get("base_url", old.get("base_url"))),
                      "api": body.get("api", old.get("api", "openai-responses")),
                      "model": _text(body.get("model", old.get("model")), "模型名称"),
                      "model_options": body.get("model_options", old.get("model_options", {})),
                      "request_format": body.get("request_format", old.get("request_format", "native")),
                      "revision": old.get("revision", 0) + 1}
            if record["api"] not in APIS:
                raise GatewayError("unsupported_protocol", "请选择受支持的 Gateway 协议")
            if record["request_format"] not in ("native", "instructions") or (record["api"] != "openai-responses" and record["request_format"] != "native"):
                raise GatewayError("invalid_request_format", "这个协议不支持所选请求格式")
            options = record["model_options"]
            if not isinstance(options, dict) or set(options) - {"reasoning", "input", "contextWindow", "maxTokens"}:
                raise GatewayError("invalid_model_options", "模型能力配置格式不正确")
            if "reasoning" in options and not isinstance(options["reasoning"], bool):
                raise GatewayError("invalid_model_options", "推理能力需要是开关")
            if "input" in options and (not isinstance(options["input"], list) or not options["input"] or any(value not in ("text", "image") for value in options["input"])):
                raise GatewayError("invalid_model_options", "模型输入能力只能包含文字或图片")
            for field in ("contextWindow", "maxTokens"):
                if field in options and (isinstance(options[field], bool) or not isinstance(options[field], int) or not 1 <= options[field] <= 100_000_000):
                    raise GatewayError("invalid_model_options", "模型 token 上限需要是有效正整数")
            key = body.get("api_key")
            if key is not None:
                key = _text(key, "认证凭据", 16384)
            if previous and (urlsplit(previous["base_url"]).scheme, urlsplit(previous["base_url"]).netloc) != (urlsplit(record["base_url"]).scheme, urlsplit(record["base_url"]).netloc) and not key:
                raise GatewayError("credentials_required_for_new_host", "更换 Gateway 主机时，请重新输入对应凭据")
            if not previous and not key:
                raise GatewayError("credentials_required", "请填写 API Key 或 Bearer Token")
            vault = self._vault()
            old_credential = vault.read(provider)
            original = json.loads(json.dumps(self.value))
            try:
                if key:
                    vault.write(provider, {"type": "api_key", "key": key})
                    self.value["credentials"][provider] = {"type": "api_key"}
                self.value["profiles"] = [record if p["id"] == profile_id else p for p in self.value["profiles"]]
                if not previous:
                    self.value["profiles"].append(record)
                if body.get("activate") or not self.value["active_id"]:
                    self.value["active_id"] = profile_id
                self._changed()
                self._save()
            except Exception:
                self.value = original
                if key:
                    if old_credential is None:
                        vault.delete(provider)
                    else:
                        vault.write(provider, old_credential)
                raise
            return self.public()

    def activate(self, profile_id, *, expected_revision=None):
        with self.transaction():
            self.check_revision(expected_revision)
            profile = self.profile(profile_id)
            if not self.read_credential(profile["provider_id"]):
                raise GatewayError("credentials_required", "请先为这个 Gateway 保存凭据。")
            self.value["active_id"] = profile_id
            self._changed()
        return self.public()

    def record_test(self, profile_id, revision, result):
        with self.transaction():
            profile = next((p for p in self.value["profiles"] if p["id"] == profile_id), None)
            if not profile or profile.get("revision") != revision:
                return False
            profile["last_test"] = {key: result.get(key) for key in ("ok", "category", "message", "latency_ms", "tested_at")}
            return True

    def delete(self, profile_id, *, credentials_only=False, expected_revision=None):
        with self.lock:
            self.check_revision(expected_revision)
            profile = self.profile(profile_id)
            provider = profile["provider_id"]
            vault = self._vault()
            secret = vault.read(provider)
            original = json.loads(json.dumps(self.value))
            try:
                vault.delete(provider)
                self.value["credentials"].pop(provider, None)
                if credentials_only:
                    for item in self.value["profiles"]:
                        if item["id"] == profile_id:
                            item.pop("last_test", None)
                            item["revision"] = item.get("revision", 0) + 1
                else:
                    self.value["profiles"] = [p for p in self.value["profiles"] if p["id"] != profile_id]
                    if self.value["active_id"] == profile_id:
                        self.value["active_id"] = None
                self._changed()
                self._save()
            except Exception:
                self.value = original
                if secret is not None:
                    vault.write(provider, secret)
                raise
            return self.public()

    def read_credential(self, provider):
        return self._vault().read(provider)

    def write_credential(self, provider, credential):
        """Pi's native credential refresh/login path, kept outside conversation records."""
        if not isinstance(provider, str) or not provider or len(provider) > 200 or any(ord(c) < 32 for c in provider):
            raise GatewayError("invalid_provider", "凭据提供方标识无效")
        if not isinstance(credential, dict) or credential.get("type") not in ("api_key", "oauth"):
            raise GatewayError("invalid_credential", "认证凭据格式无效")
        with self.lock:
            vault = self._vault()
            previous = vault.read(provider)
            original = deepcopy(self.value)
            try:
                vault.write(provider, credential)
                self.value["credentials"][provider] = {"type": credential["type"]}
                if any(p["provider_id"] == provider for p in self.value["profiles"]):
                    self._changed(provider)
                self._save()
            except Exception:
                self.value = original
                if previous is None:
                    vault.delete(provider)
                else:
                    vault.write(provider, previous)
                raise

    def remove_credential(self, provider):
        with self.lock:
            vault = self._vault()
            previous = vault.read(provider)
            original = deepcopy(self.value)
            try:
                vault.delete(provider)
                self.value["credentials"].pop(provider, None)
                if any(p["provider_id"] == provider for p in self.value["profiles"]):
                    self._changed(provider)
                self._save()
            except Exception:
                self.value = original
                if previous is not None:
                    vault.write(provider, previous)
                raise

    def credential_snapshot(self):
        """Private IPC only. Callers must never return or log this value."""
        with self.lock:
            return {provider: value for provider in self.value["credentials"] if (value := self._vault().read(provider)) is not None}

    def runtime_configuration(self):
        """One consistent private snapshot, containing secrets. Never return it over HTTP."""
        with self.lock:
            return {"profiles": deepcopy(self.value["profiles"]), "activeProfileId": self.value["active_id"],
                    "configurationRevision": self.value["revision"], "credentials": self.credential_snapshot()}

    def begin_test(self, profile_id, request_id):
        with self.transaction():
            profile = self.profile(profile_id)
            for item in self.value["tests"]:
                if item["id"] == request_id:
                    if item["profile_id"] != profile_id:
                        raise GatewayError("test_conflict", "这个测试标识已属于另一个 Gateway。")
                    return deepcopy(item), False
            running = next((t for t in self.value["tests"] if t["state"] == "running" and t["profile_id"] == profile_id), None)
            if running:
                return deepcopy(running), False
            if not self.read_credential(profile["provider_id"]):
                raise GatewayError("credentials_required", "请先保存认证凭据，再测试连接。")
            item = {"id": request_id, "profile_id": profile_id, "revision": profile["revision"], "state": "running", "started_at": self._now()}
            self.value["tests"] = [t for t in self.value["tests"] if t["state"] == "running" or t in self.value["tests"][-49:]] + [item]
            return deepcopy(item), True

    def finish_test(self, request_id, result=None, *, state=None):
        with self.transaction():
            item = next((t for t in self.value["tests"] if t["id"] == request_id), None)
            if not item or item["state"] != "running":
                return deepcopy(item)
            profile = next((p for p in self.value["profiles"] if p["id"] == item["profile_id"]), None)
            if not state and (not profile or profile["revision"] != item["revision"]):
                state = "superseded"
            state = state or ("succeeded" if result.get("ok") else "cancelled" if result.get("category") == "cancelled" else "failed")
            item.update(state=state, finished_at=self._now())
            if result and state in ("succeeded", "failed"):
                clean = {key: result.get(key) for key in ("ok", "category", "message", "latency_ms", "tested_at")}
                item.update(clean)
                profile["last_test"] = clean
            else:
                item["message"] = {"cancelled": "连接测试已取消。", "superseded": "配置已变化，请重新测试。", "interrupted": "测试已中断，请重新测试。"}.get(state, "测试未完成。")
            return deepcopy(item)
