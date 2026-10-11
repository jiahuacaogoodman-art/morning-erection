"""Pi's native RPC transport, with a separate inherited socket for secret storage."""
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path


class NativePiError(RuntimeError):
    pass


class NativePiHost:
    def __init__(self, *, runtime_path, workspace, state_dir, gateways, legacy_messages=None, agent_dir=None, selection_path=None):
        self.runtime_path = Path(runtime_path).resolve()
        self.workspace = Path(workspace).resolve()
        self.state_dir = Path(state_dir).resolve()
        self.selection_path = Path(selection_path) if selection_path else self.state_dir / 'pi' / 'selected-session.json'
        self.gateways = gateways
        self.legacy_messages = legacy_messages or []
        self.agent_dir = str(Path(agent_dir).resolve()) if agent_dir else None
        self.process = None
        self.private = None
        self.private_writer = None
        self.start_lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.private_lock = threading.Lock()
        self.lock = threading.RLock()
        self.ready = threading.Event()
        self.agent_ready = False
        self.agent_start_lock = threading.Lock()
        self.pending = {}
        self.pending_control = {}
        self.pending_ui = {}
        self.ui_expiry = {}
        self.events = deque(maxlen=256)
        self.stderr = deque(maxlen=30)
        self.listeners = []
        self.secrets = set()
        self.status = "stopped"
        self.error = None
        self.info = {}
        self.revision = 0
        self.tool_handler = None
        self.watch_proposal_schema = None
        self.platform_tools = False

    @property
    def available(self):
        return bool(shutil.which("node") and self.runtime_path.exists()
                    and (self.runtime_path.parent / "node_modules" / "@earendil-works" / "pi-coding-agent").exists())

    def _remember_secrets(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("key", "apiKey", "api_key", "access", "refresh", "token") and isinstance(item, str) and item:
                    self.secrets.add(item)
                elif key == "env" and isinstance(item, dict):
                    self.secrets.update(entry for entry in item.values() if isinstance(entry, str) and entry)
                elif isinstance(item, (dict, list)):
                    self._remember_secrets(item)
        elif isinstance(value, list):
            for item in value:
                self._remember_secrets(item)

    def redact(self, text):
        text = str(text)
        for value in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(value, "[凭据已隐藏]")
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[凭据已隐藏]", text)
        return re.sub(r"Bearer\s+[^\s\"'<>]+", "Bearer [凭据已隐藏]", text, flags=re.I)

    def sanitize(self, value):
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, list):
            return [self.sanitize(item) for item in value]
        if isinstance(value, dict):
            return {key: self.sanitize(item) for key, item in value.items()}
        return value

    def _publish(self, event):
        safe = self.sanitize(event)
        with self.lock:
            self.revision += 1
            self.events.append(safe)
            listeners = tuple(self.listeners)
        for listener in listeners:
            try:
                listener(safe)
            except Exception:
                pass  # a disconnected GUI must not kill Pi

    def start(self, *, session_file=None, fresh=False):
        with self.start_lock:
            if self.process and self.process.poll() is None:
                return
            if self.process:
                self.close()
            if not self.available:
                raise NativePiError("Pi 运行时尚未安装，请先安装项目依赖。")
            self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.ready.clear()
            self.agent_ready = False
            self.status, self.error = "starting", None
            configuration = self.gateways.runtime_configuration()
            credentials = configuration["credentials"]
            self._remember_secrets(credentials)
            empty_session = None
            selected = self.selection_path
            if not session_file and not fresh and selected.exists():
                try:
                    saved = json.loads(selected.read_text())
                    saved_path = saved.get("path")
                    if saved_path and Path(saved_path).is_file():
                        session_file = saved_path
                    elif saved.get("empty") and saved.get("session_id"):
                        empty_session = saved
                except (OSError, ValueError):
                    pass  # native continueRecent remains the recovery path
            parent, child = socket.socketpair()
            env = dict(os.environ)
            env["MORNING_CONTROL_FD"] = str(child.fileno())
            prepared = subprocess.run([shutil.which("node"), str(self.runtime_path.parent / "prepare-runtime.mjs")],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            if prepared.returncode:
                parent.close()
                child.close()
                raise NativePiError("Pi 依赖检查没有通过。")
            process = subprocess.Popen([shutil.which("node"), str(self.runtime_path)], cwd=self.workspace, env=env,
                pass_fds=(child.fileno(),), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", bufsize=1, umask=0o077)
            child.close()
            self.process, self.private = process, parent
            self.private_writer = parent.makefile("w", encoding="utf-8", buffering=1)
            threading.Thread(target=self._read_public, args=(process,), daemon=True).start()
            threading.Thread(target=self._read_private, args=(process, parent), daemon=True).start()
            threading.Thread(target=self._read_errors, args=(process,), daemon=True).start()
            self._private_send({"type": "configure", "data": {"workspace": str(self.workspace),
                "sessionDir": str(self.state_dir / "pi" / "sessions"), "sessionFile": session_file, "freshSession": fresh,
                "agentDir": self.agent_dir, "emptySession": empty_session,
                "profiles": configuration["profiles"], "activeProfileId": configuration["activeProfileId"],
                "configurationRevision": configuration["configurationRevision"],
                "credentials": credentials, "legacyMessages": self.legacy_messages, "watchProposalSchema": self.watch_proposal_schema,
                "platformTools": self.platform_tools}})

    def _read_errors(self, process):
        for line in process.stderr:
            if self.process is process:
                self.stderr.append(self.redact(line.strip())[:2000])

    def _read_public(self, process):
        try:
            for line in process.stdout:
                if self.process is not process:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "response":
                    with self.lock:
                        channel = self.pending.get(event.get("id"))
                    if channel:
                        channel.put(self.sanitize(event))
                else:
                    if event.get("type") == "extension_ui_request":
                        if event.get("method") in ("select", "confirm", "input", "editor"):
                            with self.lock:
                                self.pending_ui[event["id"]] = {**self.sanitize(event), "channel": "native"}
                                if event.get("timeout"):
                                    self.ui_expiry[event["id"]] = time.monotonic() + event["timeout"] / 1000
                    self._publish(event)
        finally:
            if self.process is process:
                self.status = "stopped"
                self.agent_ready = False
                self.pending_ui.clear()
                self.ui_expiry.clear()
                if not self.ready.is_set():
                    self.error = "\n".join(self.stderr)[-1500:] or "Pi 启动未完成。"
                self.ready.set()
                failure = {"success": False, "ok": False, "error": "Pi 进程已结束。"}
                with self.lock:
                    for channel in (*self.pending.values(), *self.pending_control.values()):
                        channel.put(failure)
                self._publish({"type": "process_ended", "error": self.error})

    def _read_private(self, process, connection):
        try:
            with connection.makefile("r", encoding="utf-8") as source:
                for line in source:
                    if self.process is not process:
                        break
                    try:
                        frame = json.loads(line)
                    except ValueError:
                        continue
                    kind = frame.get("type")
                    if kind == "ready":
                        self.info = self.sanitize(frame.get("data") or {})
                        self.status = "ready"
                        self.ready.set()
                        self._publish({"type": "ready", "data": self.info})
                    elif kind == "agent_ready":
                        self.agent_ready = True
                        self.info = self.sanitize(frame.get("data") or {})
                        self._publish({"type": "agent_ready", "data": self.info})
                    elif kind == "control_response":
                        with self.lock:
                            channel = self.pending_control.get(frame.get("id"))
                        if channel:
                            channel.put(self.sanitize(frame))
                    elif kind == "host_request":
                        self._host_request(frame)
                    elif kind == "host_cancel":
                        with self.lock:
                            self.pending_ui.pop(frame.get("id"), None)
                        self._publish({"type": "ui_cancelled", "id": frame.get("id")})
                    elif kind == "auth_event":
                        self._publish(frame)
                    elif kind in ("background_started", "background_finished"):
                        self._publish(frame)
                    elif kind == "agent_error":
                        self.error = self.redact(frame.get("message", "Pi 进程遇到错误。"))
                        self._publish({"type": "agent_error", "message": self.error})
        except (OSError, ValueError):
            pass

    def _host_request(self, frame):
        method, params = frame.get("method"), frame.get("params") or {}
        request_id = frame["id"]
        try:
            if method == "morning_tool":
                threading.Thread(target=self._run_tool, args=(frame,), daemon=True).start()
                return
            if method == "credential_write":
                self._remember_secrets(params.get("credential"))
                self.gateways.write_credential(params["providerId"], params["credential"])
            elif method == "credential_delete":
                self.gateways.remove_credential(params["providerId"])
            elif method in ("project_trust", "auth_prompt"):
                if method == "auth_prompt" and params.get("type") in ("info", "progress", "auth_url", "device_code"):
                    self._publish({"type": "auth_notice", "data": params})
                    self._private_send({"type": "host_response", "id": request_id, "ok": True, "data": ""})
                    return
                with self.lock:
                    self.pending_ui[request_id] = {**self.sanitize(params), "id": request_id, "channel": "private", "method": method}
                self._publish({"type": "ui_request", "id": request_id})
                return
            else:
                raise NativePiError("暂不支持这个宿主操作。")
            self._private_send({"type": "host_response", "id": request_id, "ok": True, "data": True})
        except Exception:
            self._private_send({"type": "host_response", "id": request_id, "ok": False, "error": "系统凭据存储操作失败，凭据尚未更新。"})

    def _run_tool(self, frame):
        try:
            if not self.tool_handler:
                raise NativePiError("Morning 浏览器扩展尚未连接。")
            params = dict(frame["params"].get("args") or {})
            if frame["params"].get('tool_call_id'):
                params['_tool_call_id'] = frame['params']['tool_call_id']
            result = self.tool_handler(frame["params"]["name"], params)
            self._private_send({"type": "host_response", "id": frame["id"], "ok": True, "data": self.sanitize(result)})
        except Exception as exc:
            self._private_send({"type": "host_response", "id": frame["id"], "ok": False, "error": self.redact(str(exc))})

    def _private_send(self, frame):
        with self.private_lock:
            if not self.private_writer:
                raise NativePiError("Pi 私有连接尚未建立。")
            self.private_writer.write(json.dumps(frame, ensure_ascii=False) + "\n")
            self.private_writer.flush()

    def _send(self, frame):
        with self.write_lock:
            if not self.process or self.process.poll() is not None:
                raise NativePiError("Pi 进程尚未启动。")
            self.process.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def wait_ready(self, timeout=40):
        self.start()
        deadline = time.monotonic() + timeout
        while not self.ready.wait(.25):
            if self.pending_ui:
                deadline = time.monotonic() + timeout
            if time.monotonic() > deadline:
                raise NativePiError("Pi 启动超时，请查看运行诊断。")
        if self.status != "ready":
            raise NativePiError(self.error or "Pi 启动失败。")

    def _request(self, command, params, *, private=False, timeout=60):
        self.wait_ready()
        request_id = uuid.uuid4().hex
        channel = queue.Queue()
        pending = self.pending_control if private else self.pending
        with self.lock:
            pending[request_id] = channel
        try:
            if private:
                self._private_send({"type": "control", "id": request_id, "method": command, "params": params})
            else:
                self._send({"id": request_id, "type": command, **params})
            deadline = time.monotonic() + timeout
            while True:
                try:
                    result = channel.get(timeout=.25)
                    break
                except queue.Empty:
                    if self.pending_ui:
                        deadline = time.monotonic() + timeout
                    if time.monotonic() > deadline:
                        raise
            if not result.get("ok" if private else "success"):
                raise NativePiError(self.redact(result.get("error", "Pi 请求没有完成。")))
            return result.get("data", {})
        except queue.Empty:
            raise NativePiError("Pi 请求超时；正在运行的任务仍可通过停止按钮中断。") from None
        finally:
            with self.lock:
                pending.pop(request_id, None)

    def rpc(self, command, params=None, *, timeout=60):
        self.wait_ready()
        if not self.agent_ready:
            with self.agent_start_lock:
                if not self.agent_ready:
                    self.control("start_agent", timeout=120)
        return self._request(command, params or {}, timeout=timeout)

    def control(self, command, params=None, *, timeout=60):
        return self._request(command, params or {}, private=True, timeout=timeout)

    def update_gateways(self, *, select_active=False):
        if not self.process or self.process.poll() is not None:
            return
        configuration = self.gateways.runtime_configuration()
        self._remember_secrets(configuration["credentials"])
        return self.control("configure_gateways", {**configuration, "selectActive": select_active})

    def answer_ui(self, request_id, answer):
        with self.lock:
            self._expire_ui()
            request = self.pending_ui.pop(request_id, None)
            self.ui_expiry.pop(request_id, None)
        if request is None:
            raise NativePiError("这个交互请求已经结束。")
        if request["channel"] == "native":
            self._send({"type": "extension_ui_response", "id": request_id, **answer})
        else:
            result = answer.get("confirmed", False) if request.get("kind") == "confirm" else answer.get("value", "")
            self._private_send({"type": "host_response", "id": request_id, "ok": not answer.get("cancelled", False), "data": result, "error": "用户取消了操作。"})

    def remember_session(self, path, *, state=None, workspace=None):
        if not state and (not path or not Path(path).is_file()):
            return
        target = self.selection_path
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {"path": path}
        if state and (not path or not Path(path).is_file()):
            model = state.get("model") or {}
            data.update(empty=True, session_id=state["sessionId"], workspace=workspace or str(self.workspace),
                        model={key: model[key] for key in ("provider", "id") if key in model},
                        thinking_level=state.get("thinkingLevel"), name=state.get("sessionName"))
        value = json.dumps(data)
        with self.lock:
            if target.exists() and target.read_text() == value:
                return
            temporary = target.with_suffix(".tmp")
            fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write(value)
            temporary.replace(target)

    def _expire_ui(self):
        for request_id, deadline in list(self.ui_expiry.items()):
            if time.monotonic() >= deadline:
                self.pending_ui.pop(request_id, None)
                self.ui_expiry.pop(request_id, None)

    def status_snapshot(self):
        with self.lock:
            self._expire_ui()
            return {"available": self.available, "status": self.status, "error": self.error, "info": dict(self.info),
                    "agent_ready": self.agent_ready, "pending_ui": list(self.pending_ui.values()), "revision": self.revision}

    def close(self):
        with self.start_lock:
            process = self.process
            if process and process.poll() is None:
                try:
                    if self.status == "ready" and self.agent_ready:
                        self.rpc("abort", timeout=10)
                        process.stdin.close()
                    elif self.status == "ready":
                        self.control("shutdown", timeout=5)
                    process.wait(timeout=8)
                except (OSError, NativePiError, subprocess.TimeoutExpired):
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
            self.process = None
            if self.private_writer:
                self.private_writer.close()
            if self.private:
                self.private.close()
            self.private = self.private_writer = None
            self.status = "stopped"
            self.agent_ready = False
