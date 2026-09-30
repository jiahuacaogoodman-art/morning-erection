"""Secret resolution port. Secrets are dereferenced by the gateway per (tenant, ref)."""
from typing import Optional, Protocol


class SecretsPort(Protocol):
    def resolve(self, tenant_id: str, secret_ref: str) -> Optional[str]: ...
