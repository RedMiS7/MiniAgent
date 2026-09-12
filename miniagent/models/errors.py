from typing import Literal

ErrorCode = Literal[
    "authentication", "permission", "rate_limit", "timeout", "connection",
    "invalid_request", "server", "invalid_response", "unsupported_feature", "api",
]


class LLMError(Exception):
    """Provider-independent error; never contains a raw service response."""

    def __init__(self, code: ErrorCode, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
