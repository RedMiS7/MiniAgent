"""Shared SDK boundary handling; not part of the public protocol."""
import json
from contextlib import contextmanager

import httpx
from openai import (
    APIConnectionError, APIError, APIResponseValidationError,
    APIStatusError, APITimeoutError,
)

from .errors import LLMError
from .types import ContinuationState, TokenUsage, ToolCall


@contextmanager
def sdk_errors():
    try:
        yield
    except (APITimeoutError, httpx.TimeoutException):
        raise LLMError("timeout", "Model request timed out.", True) from None
    except (APIConnectionError, httpx.TransportError):
        raise LLMError("connection", "Could not connect to model service.", True) from None
    except APIResponseValidationError:
        raise LLMError("invalid_response", "Invalid model response.") from None
    except APIStatusError as exc:
        code = {
            401: "authentication", 403: "permission", 429: "rate_limit",
        }.get(exc.status_code, "server" if exc.status_code >= 500 else "invalid_request")
        raise LLMError(
            code, "Model service rejected the request.",
            exc.status_code in (408, 409, 429) or exc.status_code >= 500,
        ) from None
    except APIError:
        raise LLMError("api", "Model API request failed.") from None
    except (AttributeError, KeyError, TypeError, ValueError, IndexError):
        raise LLMError("invalid_response", "Invalid model response structure.") from None


def state_payload(state: ContinuationState, provider: str, model: str):
    if state.provider != provider or state.model != model or state.version != 1:
        raise LLMError("invalid_request", "Continuation belongs to another model or format.")
    return json.loads(state.payload)


def usage_from(data: dict | None, input_key: str, output_key: str) -> TokenUsage | None:
    if data is None:
        return None
    values = (data[input_key], data[output_key])
    if any(type(value) is not int or value < 0 for value in values):
        raise LLMError("invalid_response", "Invalid token usage.")
    return TokenUsage(input_tokens=values[0], output_tokens=values[1])


def require_text(value) -> str:
    if not isinstance(value, str):
        raise LLMError("invalid_response", "Expected text in model response.")
    return value


def tool_call_from(call_id, name, arguments) -> ToolCall:
    if not isinstance(call_id, str) or not call_id.strip() or not isinstance(name, str) or not name.strip():
        raise LLMError("invalid_response", "Invalid tool call identity.")
    return ToolCall(id=call_id, name=name, arguments=require_text(arguments))
