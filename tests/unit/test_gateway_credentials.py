import json

import pytest

from wakecore.app.pulse.gateways import GatewayError, GatewayStore


class VaultFixture:
    def __init__(self):
        self.values = {}

    def read(self, provider):
        return self.values.get(provider)

    def write(self, provider, credential):
        self.values[provider] = dict(credential)

    def delete(self, provider):
        self.values.pop(provider, None)


def profile(**updates):
    return {"name": "Test Gateway", "base_url": "https://gateway.example/v1", "model": "test-model",
            "api": "openai-responses", "api_key": "private-fixture-value", **updates}


def test_profile_secret_separation_rotation_restart_and_removal(tmp_path):
    vault = VaultFixture()
    store = GatewayStore(tmp_path, vault=vault)
    public = store.save(profile())
    item = public["items"][0]
    assert item["has_credentials"]
    assert "private-fixture-value" not in json.dumps(public)
    assert "private-fixture-value" not in store.path.read_text()
    assert store.path.stat().st_mode & 0o777 == 0o600
    restored = GatewayStore(tmp_path, vault=vault)
    assert restored.profile()["id"] == item["id"]
    assert restored.read_credential(item["provider_id"])["key"] == "private-fixture-value"
    restored.save({"id": item["id"], "api_key": "rotated-private-value", "name": "Renamed"})
    assert restored.read_credential(item["provider_id"])["key"] == "rotated-private-value"
    assert "rotated-private-value" not in restored.path.read_text()
    restored.delete(item["id"], credentials_only=True)
    assert restored.public()["items"][0]["has_credentials"] is False
    assert restored.read_credential(item["provider_id"]) is None
    restored.delete(item["id"])
    assert restored.public()["items"] == [] and restored.public()["active_id"] is None
    assert GatewayStore(tmp_path, vault=vault).public()["items"] == []


def test_host_change_needs_new_credential_and_does_not_send_old_one(tmp_path):
    vault = VaultFixture()
    store = GatewayStore(tmp_path, vault=vault)
    item = store.save(profile())["items"][0]
    with pytest.raises(GatewayError, match="重新输入"):
        store.save({"id": item["id"], "base_url": "https://another.example/v1"})
    assert store.profile()["base_url"] == "https://gateway.example/v1"
    assert store.read_credential(item["provider_id"])["key"] == "private-fixture-value"


@pytest.mark.parametrize("address", ["https://key:password@gateway.example/v1", "https://gateway.example/v1?key=private", "http://remote.example/v1", "https://gateway.example/v1/responses"])
def test_reject_unsafe_or_incorrect_addresses_without_persisting(tmp_path, address):
    vault = VaultFixture()
    store = GatewayStore(tmp_path, vault=vault)
    with pytest.raises(GatewayError):
        store.save(profile(base_url=address))
    assert not vault.values
    assert not store.path.exists()


def test_profile_write_failure_restores_credential(tmp_path, monkeypatch):
    vault = VaultFixture()
    store = GatewayStore(tmp_path, vault=vault)
    item = store.save(profile())["items"][0]

    def fail():
        raise OSError("fixture storage failure")

    monkeypatch.setattr(store, "_save", fail)
    with pytest.raises(OSError):
        store.save({"id": item["id"], "api_key": "must-not-remain", "name": "Changed"})
    assert store.profile()["name"] == "Test Gateway"
    assert store.read_credential(item["provider_id"])["key"] == "private-fixture-value"


def test_unavailable_secure_store_fails_without_plaintext_fallback(tmp_path):
    store = GatewayStore(tmp_path, vault=VaultFixture())
    store.vault = None
    with pytest.raises(GatewayError, match="系统凭据存储"):
        store.save(profile())
    assert not store.path.exists()


def test_vault_loss_and_native_auth_changes_invalidate_connection_evidence(tmp_path):
    vault = VaultFixture()
    store = GatewayStore(tmp_path, vault=vault)
    item = store.save(profile())["items"][0]
    store.record_test(item["id"], item["revision"], {"ok": True, "category": "connected"})
    store.write_credential(item["provider_id"], {"type": "api_key", "key": "rotated-through-pi"})
    assert store.profile()["revision"] > item["revision"]
    assert "last_test" not in store.profile()
    revision = store.profile()["revision"]
    store.record_test(item["id"], revision, {"ok": True, "category": "connected"})
    vault.delete(item["provider_id"])
    public = store.public()["items"][0]
    assert public["has_credentials"] is False
    assert public["credential_state"] == "missing"
    assert public.get("last_test") is None
    store.remove_credential(item["provider_id"])
    assert store.profile()["revision"] > revision


def test_stale_editor_cannot_overwrite_newer_gateway(tmp_path):
    store = GatewayStore(tmp_path, vault=VaultFixture())
    before = store.save(profile())
    item = before["items"][0]
    store.save({"id": item["id"], "name": "Newer", "expected_revision": before["revision"]})
    with pytest.raises(GatewayError) as caught:
        store.save({"id": item["id"], "name": "Stale", "expected_revision": before["revision"]})
    assert caught.value.code == "configuration_conflict"
    assert GatewayStore(tmp_path, vault=store.vault).profile()["name"] == "Newer"


def test_test_result_write_failure_does_not_change_in_memory_state(tmp_path, monkeypatch):
    store = GatewayStore(tmp_path, vault=VaultFixture())
    item = store.save(profile())["items"][0]
    monkeypatch.setattr(store, "_save", lambda: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        store.record_test(item["id"], item["revision"], {"ok": True})
    assert "last_test" not in store.profile()


def test_same_host_scheme_change_requires_a_new_credential(tmp_path):
    store = GatewayStore(tmp_path, vault=VaultFixture())
    item = store.save(profile(base_url="https://localhost:9000/v1"))["items"][0]
    with pytest.raises(GatewayError) as caught:
        store.save({"id": item["id"], "base_url": "http://localhost:9000/v1"})
    assert caught.value.code == "credentials_required_for_new_host"
    assert store.profile()["base_url"] == "https://localhost:9000/v1"
