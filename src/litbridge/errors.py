"""Stable, credential-free errors at the agent boundary."""
from enum import StrEnum
from pydantic import BaseModel


class Code(StrEnum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    NOT_CONFIGURED = "not_configured"
    UNSUPPORTED = "unsupported"
    AUTH_REQUIRED = "auth_required"
    ACCESS_DENIED = "access_denied"
    HUMAN_REQUIRED = "human_required"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK = "network"
    UPSTREAM_CHANGED = "upstream_changed"
    UPSTREAM = "upstream_error"
    CIRCUIT_OPEN = "circuit_open"
    UNSAFE_URL = "unsafe_url"
    INVALID_CONTENT = "invalid_content"
    TOO_LARGE = "too_large"
    STORAGE = "storage_error"
    INCOMPATIBLE = "incompatible_plugin"
    INTERNAL = "internal_error"


class ErrorInfo(BaseModel):
    code: Code
    message: str
    provider: str | None = None
    retryable: bool = False
    action: str | None = None


class BridgeError(Exception):
    def __init__(self, code: Code, message: str, *, provider: str | None = None,
                 retryable: bool = False, action: str | None = None):
        super().__init__(message)
        self.info = ErrorInfo(code=code, message=message, provider=provider,
                              retryable=retryable, action=action)


def safe_error(exc: Exception, provider: str | None = None) -> ErrorInfo:
    # Never serialize repr(exc): HTTP exceptions commonly include credentialed URLs.
    if isinstance(exc, BridgeError):
        return exc.info.model_copy(update={"provider": provider or exc.info.provider})
    if isinstance(exc, TimeoutError):
        return ErrorInfo(code=Code.TIMEOUT, message="Provider deadline exceeded",
                         provider=provider, retryable=True)
    return ErrorInfo(code=Code.INTERNAL, message="Provider failed; inspect its adapter and local tests",
                     provider=provider)
