"""Opt-in live Gateway + native Pi acceptance in an isolated workspace.

Uses the chosen profile's key from macOS Keychain without logging or exporting it.
The source profile/session/watch state is read-only. Test credentials are removed on exit.
Run: uv run python scripts/verify_gateway_lifecycle.py --output /tmp/gateway-proof.json
"""
import argparse
import json
import tempfile
import time
import uuid
from pathlib import Path

from wakecore.adapters.agent_runtime.credentials import MacKeychain
from wakecore.adapters.agent_runtime.native import NativePiHost
from wakecore.app.pulse.gateway_service import GatewayService
from wakecore.app.pulse.gateways import GatewayStore
from wakecore.app.pulse.native_agent import NativeConversation

ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout=65):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.2)
    raise RuntimeError("验收等待超时")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-state", type=Path, default=ROOT / ".pulse")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Do not instantiate a recovering store on the live production directory.
    source = json.loads((args.source_state / "gateways.json").read_text())
    profile = next(p for p in source["profiles"] if p["id"] == source["active_id"])
    secret = MacKeychain(source["installation_id"]).read(profile["provider_id"])
    if not secret or not secret.get("key"):
        raise SystemExit("所选 Gateway 缺少可用 API Key，未发起验收请求。")
    report = {"gateway": profile["base_url"], "model": profile["model"], "protocol": profile["api"], "custom_modules": False}
    with tempfile.TemporaryDirectory(prefix="morning-gateway-live-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        store = GatewayStore(root / "state")
        host = NativePiHost(runtime_path=ROOT / "apps/agent-runtime/native-bootstrap.mjs", workspace=workspace,
                            state_dir=root / "state", gateways=store, agent_dir=root / "agent")
        conversation = NativeConversation(host)
        service = GatewayService(store, conversation, lambda: None)
        try:
            settings = {k: profile[k] for k in ("name", "base_url", "api", "model", "request_format", "model_options") if k in profile}
            item = service.action({"action": "save", **settings, "api_key": "morning-intentionally-invalid-verification-key"})["items"][0]
            def check(key):
                service.action({"action": "test", "id": item["id"]}, key)
                return wait_for(lambda: (value if (value := service.public()["items"][0]["connection_test"])["state"] != "running" else None))
            invalid = check("live-invalid-key")
            assert invalid["state"] == "failed" and invalid["category"] == "authentication", "无效凭据没有被明确拒绝"
            report["invalid_credentials"] = invalid["category"]
            service.action({"action": "save", "id": item["id"], "api_key": secret["key"]})
            valid = check("live-valid-key")
            assert valid["state"] == "succeeded", valid.get("message", "真实调用失败")
            report["generation"] = {"state": valid["state"], "latency_ms": valid.get("latency_ms")}
            host.control("start_agent")
            marker = "MORNING_NATIVE_" + uuid.uuid4().hex[:12]
            service.assert_prompt_ready()
            conversation.prompt({"message": f"请使用工具在当前工作目录创建 proof.txt，内容严格为 {marker}，然后使用工具读回核对。完成后只回复 {marker}。不要读取其他目录。"}, "live-native-tool-proof")
            def finished():
                state = host.rpc("get_state")
                return state if not state["isStreaming"] and (workspace / "proof.txt").exists() and host.control("context")["isIdle"] else None
            wait_for(finished, timeout=100)
            assert (workspace / "proof.txt").read_text().strip() == marker, "工具产物与目标不符"
            messages = host.rpc("get_messages")["messages"]
            assert any(m.get("role") == "toolResult" for m in messages), "缺少原生工具结果"
            last = host.rpc("get_last_assistant_text")
            assert marker in json.dumps(last), "Agent 未回报核验结果"
            saved = conversation.snapshot()["state"]
            report["tools"] = sorted({m.get("toolName") for m in messages if m.get("role") == "toolResult"})
            report["artifact_verified"] = True
            count = len(messages)
            host.close()
            resumed = host.rpc("get_state")
            assert resumed["sessionId"] == saved["sessionId"]
            assert len(host.rpc("get_messages")["messages"]) == count
            assert service.runtime()["state"] == "applied"
            report["restart"] = {"same_session": True, "message_count": count, "configuration_applied": True}
            public = json.dumps(service.public())
            assert secret["key"] not in public and secret["key"] not in store.path.read_text()
            assert all(secret["key"] not in f.read_text(errors="replace") for f in (root / "state/pi").rglob("*.jsonl"))
            report["secret_not_in_public_or_history"] = True
            service.action({"action": "delete_credentials", "id": item["id"]})
            assert not store.public()["items"][0]["has_credentials"]
            report["credential_removal"] = True
            report["ok"] = True
        except Exception as exc:
            report.update(ok=False, error=str(exc).replace(secret["key"], "[redacted]"))
        finally:
            host.close()
            for provider in tuple(store.value["credentials"]):
                store.vault.delete(provider)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False))
    if not report.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
