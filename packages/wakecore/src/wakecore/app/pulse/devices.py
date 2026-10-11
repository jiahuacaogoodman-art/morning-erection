"""Authenticated Computer transport. Device tokens are isolated from Pi provider auth."""
import hashlib
import json
import os
import secrets
import selectors
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from wakecore.adapters.agent_runtime.credentials import MacKeychain


class DeviceError(ValueError):
    def __init__(self, message, code="device_unavailable"):
        super().__init__(message)
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DeviceError("设备地址重定向被拒绝，请填写节点的直接地址。", "redirect_refused")


class DeviceManager:
    def __init__(self, store, root, vault=None):
        self.store, self.root = store, Path(root)
        self.vault = vault
        self.vault_namespace = "computer:" + hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:24]
        self.process = None
        self.local_endpoint = None
        self.lock = threading.RLock()
        if not self.store.get("device", "local"):
            self.store.save("device", "local", {"name": "这台 Mac", "endpoint": "local", "local": True, "enabled": True})

    def secure(self):
        if self.vault is None:
            self.vault = MacKeychain(self.vault_namespace)
        return self.vault

    def public(self):
        return self.store.list("device")

    def save(self, body):
        with self.lock:
            return self._save(body)

    def _save(self, body):
        key = body.get("id") or self.store.new_id("device")
        old = self.store.get("device", key)
        if body.get("id") and old is None:
            raise DeviceError("这个设备已不存在。")
        if key == "local":
            return self.store.save("device", key, {**old, "enabled": bool(body.get("enabled", old["enabled"]))})
        address = str(body.get("endpoint", (old or {}).get("endpoint", ""))).rstrip("/")
        parsed = urlsplit(address)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise DeviceError("设备地址必须为不含凭据的 HTTP(S) 地址。", "invalid_address")
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise DeviceError("远程节点需要 HTTPS，或通过 SSH 隧道连接本地地址。", "https_required")
        token = body.get("token")
        if not old and not token or old and old["endpoint"] != address and not token:
            raise DeviceError("新增设备或更换地址时，请输入对应节点的认证令牌。", "credentials_required")
        previous_credential = self.secure().read(key) if token else None
        if token:
            if not isinstance(token, str) or len(token) < 16 or len(token) > 16384 or any(c.isspace() for c in token):
                raise DeviceError("设备令牌格式无效。")
        try:
            if token:
                self.secure().write(key, {"token": token})
            return self.store.save("device", key, {"name": str(body.get("name") or "远程设备")[:80], "endpoint": address,
                                                   "local": False, "enabled": bool(body.get("enabled", True)), "credential_present": bool(token or old)})
        except Exception:
            if token:
                if previous_credential is None:
                    self.secure().delete(key)
                else:
                    self.secure().write(key, previous_credential)
            raise

    def delete(self, key):
        if key == "local":
            raise DeviceError("本机节点可关闭；远程节点可移除。")
        self.secure().delete(key)
        self.store.remove("device", key)

    def _start(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                return
            if self.process:
                self._stop(self.process)
                self.process = None
            item = self.secure().read("local")
            if not item:
                item = {"token": secrets.token_urlsafe(32)}
                self.secure().write("local", item)
            env = dict(os.environ)
            # The node has no reason to inherit any model provider credentials.
            for key in list(env):
                if any(part in key for part in ("API_KEY", "AUTH_TOKEN", "ACCESS_TOKEN")):
                    env.pop(key, None)
            env["MORNING_DEVICE_TOKEN"] = item["token"]
            process = subprocess.Popen([sys.executable, "-m", "wakecore_ui_runtime.desktop.node", "--state", str(self.root / "computer-node"), "--port", "0"],
                                       env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, umask=0o077)
            try:
                # select.select fails above FD_SETSIZE; a partial line must not
                # turn the bounded readiness wait into an unbounded readline.
                output, deadline = b'', time.monotonic() + 15
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while b'\n' not in output:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            raise DeviceError('本机节点启动超时。', 'device_start_timeout')
                        chunk = os.read(process.stdout.fileno(), 4096)
                        if not chunk or len(output) + len(chunk) > 16384:
                            raise ValueError('invalid readiness response')
                        output += chunk
                ready = json.loads(output.split(b'\n', 1)[0])
                if ready.get('protocol') != 'morning.computer-node/1' or type(ready.get('port')) is not int or not 1 <= ready['port'] <= 65535:
                    raise ValueError('invalid node protocol')
                self.local_endpoint = f"http://127.0.0.1:{ready['port']}"
            except DeviceError:
                self._stop(process)
                raise
            except Exception:
                self._stop(process)
                raise DeviceError("本机节点启动失败，请检查 Computer 依赖。") from None
            self.process = process

    def call(self, key, route, body=None):
        device = self.store.get("device", key)
        if not device or not device["enabled"]:
            raise DeviceError("设备未连接或已关闭。", "device_disabled")
        if device["local"]:
            self._start()
        credential = self.secure().read(key) or {}
        if not credential.get("token"):
            raise DeviceError("设备认证令牌缺失，请更新凭据。", "credentials_required")
        address = self.local_endpoint if device["local"] else device["endpoint"]
        req = urllib.request.Request(address + route, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + credential["token"], "Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=100) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                value = json.load(exc).get("error", {})
            except ValueError:
                value = {}
            raise DeviceError(value.get("message", "设备请求失败。"), value.get("code", "device_http_error")) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise DeviceError("无法连接设备，请检查节点是否启动、地址和网络。", "device_unreachable") from None

    def close(self):
        with self.lock:
            if self.process:
                self._stop(self.process)
                self.process = None
                self.local_endpoint = None

    @staticmethod
    def _stop(process):
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if process.stdout:
            process.stdout.close()
