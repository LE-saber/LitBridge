from __future__ import annotations
from litbridge.errors import BridgeError, Code
from litbridge.models import Candidate, Paper, ProviderInfo, Query, SearchPage


class Provider:
    """Trusted async plugin interface, protocol 1.0. No implicit capability inference."""
    info: ProviderInfo
    cache_scope: str = "public"

    def require(self, capability: str) -> None:
        if capability not in self.info.capabilities:
            raise BridgeError(Code.UNSUPPORTED, f"Capability {capability} is not implemented")
        if self.info.state == "not_configured":
            raise BridgeError(Code.NOT_CONFIGURED, "Provider needs local configuration",
                              action="; ".join(self.info.requirements))

    async def search(self, query: Query) -> SearchPage:
        raise BridgeError(Code.UNSUPPORTED, "Search not implemented")

    async def resolve(self, identifier: str) -> Paper | None:
        raise BridgeError(Code.UNSUPPORTED, "Resolution not implemented")

    async def access(self, paper: Paper) -> list[Candidate]:
        return []

    async def retrieve(self, candidate: Candidate) -> bytes:
        raise BridgeError(Code.UNSUPPORTED, "Retrieval not implemented")

    async def references(self, paper: Paper) -> list[dict]:
        raise BridgeError(Code.UNSUPPORTED, "References not implemented")

    async def import_url(self, url: str) -> Paper:
        raise BridgeError(Code.UNSUPPORTED, "URL import not implemented")

    async def health(self, live: bool = False) -> dict:
        return {"state": self.info.state, "check": "configuration", "live_checked": False}

    async def close(self) -> None:
        pass
