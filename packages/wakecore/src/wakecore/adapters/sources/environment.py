"""Source-neutral, read-only observations for WakeCore's existing durable scheduler."""
import hashlib
import json
import os
import socket
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

from wakecore.kernel.domain.enums import Completeness, ObservationMode, ObservationOutcome
from wakecore.kernel.ports.observation import ConnectorDescriptor, ObservationResult

KINDS = {"file", "folder", "log", "process", "http", "service", "desktop", "screen"}
PRIVATE_PARTS = {".ssh", ".secrets", ".pulse", ".aws", ".gnupg", "Keychains", "Cookies", "Login Data"}


class SourceUnavailable(Exception):
    def __init__(self, code, retry_after=60):
        super().__init__("来源暂时不可用，请稍后重试。")
        self.code, self.retry_after = code, retry_after


def retry_after(headers, now, default=0):
    raw = str((headers or {}).get("Retry-After", ""))
    if not raw:
        return default
    try:
        seconds = int(raw) if raw.isdigit() else int((parsedate_to_datetime(raw) - now).total_seconds())
        return max(1, min(2**31 - 1, seconds))
    except (ValueError, TypeError, OverflowError):
        return max(60, default)


def validate_source(source):
    if not isinstance(source, dict) or source.get("kind") not in KINDS:
        raise ValueError("请选择支持的观察来源。")
    kind = source["kind"]
    target = source.get("target", "")
    if not isinstance(target, str) or len(target) > 4096:
        raise ValueError("观察目标格式无效。")
    if kind in ("file", "folder", "log"):
        path = Path(target).expanduser()
        if not path.is_absolute():
            raise ValueError("文件观察需要绝对路径。")
        path = path.resolve()
        if any(p in PRIVATE_PARTS or p.startswith(".env") for p in path.parts):
            raise ValueError("凭据和应用私有状态不作为 Watch 数据源。")
        target = str(path)
    elif kind == "http":
        if source.get("response_mode", "content") not in ("content", "status"):
            raise ValueError("API 观察模式需要是内容或状态码。")
        parsed = urlsplit(target)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("API 观察需要不含凭据的 HTTP(S) 链接。")
        if any(word in parsed.query.lower() for word in ("token=", "key=", "secret=", "password=")):
            raise ValueError("请通过安全凭据配置认证，不要把凭据放在链接中。")
    elif kind == "service":
        host, sep, port = target.rpartition(":")
        if not sep or not host or not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError("网络服务请使用主机:端口。")
    elif kind == "process" and (not target.strip() or len(target) > 120):
        raise ValueError("请指定进程名称。")
    elif kind == "desktop" and (not target or "." not in target):
        raise ValueError("桌面来源需要应用 bundle ID。")
    elif kind == "screen" and (type(source.get("display")) is not int or not 1 <= source["display"] <= 32):
        raise ValueError("请选择显示器。")
    return {**source, "target": target}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # retain the actual status without sending credentials to a new URL


class EnvironmentSource:
    descriptor = ConnectorDescriptor(
        connector_id="environment.local", version="1.0.0", supported_resource_types=("environment_record",),
        observation_mode=ObservationMode.SNAPSHOT, supports_history_replay=False, supports_entity_revision=True,
        authentication_model="device_or_source_vault", required_scopes=("environment.observe",), rate_limit_per_minute=60,
        max_payload_bytes=128 * 1024, snapshot_completeness_contract="bounded_explicit_target", read_capability="environment.observe")

    def __init__(self, devices=None, visual_store=None):
        self.devices = devices
        self.visual = {}
        self.visual_store = visual_store

    def read(self, raw, *, metadata=None):
        source = validate_source(raw)
        kind, target = source["kind"], source["target"]
        if kind in ("file", "log"):
            path = Path(target)
            if not path.exists():
                return {"exists": False, "value": "", "size": 0}
            if not path.is_file():
                raise ValueError("观察目标不是普通文件。")
            with path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                if kind == "log":
                    stream.seek(max(0, stat.st_size - 32000))
                elif stat.st_size > 64000:
                    raise ValueError("文本文件超过 64 KB；请改为日志尾部观察或指定较小文件。")
                content = stream.read(64001)
                end = os.fstat(stream.fileno())
            if (stat.st_mtime_ns, stat.st_size) != (end.st_mtime_ns, end.st_size):
                raise ValueError("文件正在写入，将在下次检查重读。")
            return {"exists": True, "value": content.decode("utf-8", errors="replace"), "size": stat.st_size}
        if kind == "folder":
            path = Path(target)
            if not path.exists():
                return {"exists": False, "value": "", "count": 0}
            entries = []
            for entry in path.iterdir():
                if entry.is_symlink() or entry.name.startswith(".env") or entry.name in PRIVATE_PARTS:
                    continue
                if len(entries) >= 500:
                    raise ValueError("文件夹超过 500 项，请缩小观察范围。")
                stat = entry.stat()
                entries.append({"name": entry.name, "size": stat.st_size, "modified": stat.st_mtime_ns, "folder": entry.is_dir()})
            return {"exists": True, "value": json.dumps(sorted(entries, key=lambda x: x["name"]), ensure_ascii=False), "count": len(entries)}
        if kind == "process":
            listing = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True, timeout=5, check=True).stdout
            found = []
            for line in listing.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2 and (Path(parts[1]).name == target or parts[1] == target or parts[0] == target):
                    found.append(int(parts[0]))
            return {"value": bool(found), "count": len(found), "pids": sorted(found)}
        if kind == "service":
            host, port = target.rsplit(":", 1)
            try:
                with socket.create_connection((host, int(port)), timeout=5):
                    return {"value": True}
            except OSError:
                return {"value": False}
        if kind == "http":
            headers = {"Accept": "application/json,text/plain", "User-Agent": "Morning-Watch/1"}
            if source.get("credential_ref"):
                if urlsplit(target).scheme != "https" and urlsplit(target).hostname not in ("localhost", "127.0.0.1", "::1"):
                    raise ValueError("带凭据的远程 API 观察需要 HTTPS。")
                stored = self.devices.secure().read("source:" + source["credential_ref"]) or {}
                if stored.get("origin") != f"{urlsplit(target).scheme}://{urlsplit(target).netloc}":
                    raise ValueError("API 凭据未授权给此来源。")
                headers["Authorization"] = "Bearer " + stored["token"]
            request = urllib.request.Request(target, headers=headers)
            try:
                with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
                    if metadata is not None:
                        metadata["retry_after"] = retry_after(response.headers, metadata.get("at") or datetime.now(timezone.utc))
                    if source.get("response_mode") == "status":
                        return {"value": response.status, "status": response.status}
                    content = response.read(64001)
                    if len(content) > 64000:
                        raise ValueError("API 响应超过 64 KB，请缩小观察目标。")
                    text = content.decode("utf-8", errors="replace")
                    if source.get("credential_ref"):
                        text = text.replace(stored["token"], "[凭据已隐藏]")
                    pointer = source.get("json_pointer")
                    if pointer is not None:
                        value = json.loads(text)
                        for part in pointer.strip("/").split("/") if pointer else []:
                            part = part.replace("~1", "/").replace("~0", "~")
                            value = value[int(part)] if isinstance(value, list) else value[part]
                        text = value if isinstance(value, (str, float, int, bool)) or value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)
                    return {"value": text, "status": response.status}
            except urllib.error.HTTPError as exc:
                delay = retry_after(exc.headers, (metadata or {}).get("at") or datetime.now(timezone.utc), default=60)
                if source.get("response_mode") == "status":
                    if metadata is not None:
                        metadata["retry_after"] = delay
                    return {"value": exc.code, "status": exc.code}
                if exc.code in (401, 403):
                    raise PermissionError("API 认证已失效。") from None
                raise SourceUnavailable("http_" + str(exc.code), delay) from None
        if not self.devices:
            raise ValueError("Computer 节点未启用。")
        device = source.get("device_id", "local")
        if kind == "desktop":
            result = self.devices.call(device, "/observe", {"app": target})
            return {"value": result["text"], "window": result.get("window") or ""}
        result = self.devices.call(device, "/screen", {"display": source["display"], "region": source.get("region")})
        key = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()
        previous = self.visual.get(key) or (self.visual_store.get("visual", key) if self.visual_store else None)
        signature = result["signature"]
        delta = sum(abs(a-b) for a, b in zip(signature, previous["signature"])) / (15 * len(signature)) if previous else 0
        threshold = source.get("visual_threshold", .02)
        if not isinstance(threshold, (int, float)) or not .001 <= threshold <= 1:
            raise ValueError("视觉变化阈值需要在 0.001 到 1 之间。")
        if previous and delta < threshold:
            self.visual[key] = {**previous, "image": result["image"]}
            return previous["record"]
        record = {"value": hashlib.sha256(bytes(signature)).hexdigest(), "display": source["display"]}
        self.visual[key] = {"signature": signature, "record": record, "image": result["image"]}
        if self.visual_store:
            self.visual_store.save("visual", key, {"signature": signature, "record": record})
        return record

    def fetch(self, request):
        try:
            metadata = {"at": request.requested_at}
            record = self.read(request.resource_scope["environment"], metadata=metadata)
            records = {"target": record}
            return ObservationResult(ObservationOutcome.SUCCESS, Completeness.COMPLETE, {"record_ids": ["target"]}, records, request.requested_at,
                                     source_revision=hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(),
                                     retry_after_seconds=metadata.get("retry_after"))
        except SourceUnavailable as exc:
            return ObservationResult(ObservationOutcome.UNAVAILABLE, Completeness.UNKNOWN, {}, {}, request.requested_at,
                                     error_code=exc.code, retry_after_seconds=exc.retry_after)
        except PermissionError:
            outcome, code = ObservationOutcome.AUTH_REQUIRED, "source_auth_required"
        except (ValueError, KeyError, TypeError):
            outcome, code = ObservationOutcome.SCHEMA_INVALID, "source_configuration_invalid"
        except Exception:
            outcome, code = ObservationOutcome.UNAVAILABLE, "source_unavailable"
        return ObservationResult(outcome, Completeness.UNKNOWN, {}, {}, request.requested_at, error_code=code, retry_after_seconds=30)

    def semantic_images(self, scope, event):
        source = scope.get("environment", {})
        if source.get("kind") != "screen":
            return []
        key = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()
        if key not in self.visual:
            self.read(source)
        cached = self.visual[key]
        expected = [c["new"]["value"] for c in event.get("changes", []) if c.get("new") and "value" in c["new"]]
        if not expected or cached["record"]["value"] not in expected:
            raise ValueError("visual_evidence_changed")
        return [cached["image"]]
