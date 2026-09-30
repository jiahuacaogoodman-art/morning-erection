"""In-memory secret store keyed by (tenant, secret_ref). Cross-tenant lookups return None (T20)."""
from typing import Optional


class StaticSecrets:
    def __init__(self, secrets: Optional[dict[tuple[str, str], str]] = None) -> None:
        self._secrets = dict(secrets or {})

    def put(self, tenant_id: str, secret_ref: str, value: str) -> None:
        self._secrets[(tenant_id, secret_ref)] = value

    def resolve(self, tenant_id: str, secret_ref: str) -> Optional[str]:
        return self._secrets.get((tenant_id, secret_ref))
