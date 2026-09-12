"""Provider-independent, text and function-tool protocol."""
import json
import math
from dataclasses import dataclass, field
from typing import Literal

from .errors import LLMError

ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]
FinishReason = Literal["stop", "tool_calls", "length", "refusal", "content_filter"]


@dataclass(frozen=True)
class ContinuationState:
    """Opaque JSON state: callers preserve it, only its adapter interprets it."""
    provider: str
    model: str
    payload: str = field(repr=False)
    version: int = 1

    def __post_init__(self) -> None:
        try:
            json.loads(self.payload)
        except (ValueError, TypeError):
            raise LLMError("invalid_request", "Continuation must contain JSON.") from None


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.parameters, dict):
            raise LLMError("invalid_request", "Invalid tool definition.")
        if self.parameters.get("type") != "object":
            raise LLMError("invalid_request", "Tool parameters must use an object schema.")
        try:
            json.dumps(self.parameters, allow_nan=False)
        except (ValueError, TypeError):
            raise LLMError("invalid_request", "Tool schema must be JSON serializable.") from None


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    def __post_init__(self) -> None:
        if not self.id or not self.name or not isinstance(self.arguments, str):
            raise LLMError("invalid_request", "Invalid tool call.")
        # Arguments remain raw JSON text, including invalid/incomplete JSON.
        # The tool executor must parse and validate them before execution.


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    continuation: ContinuationState | None = field(default=None, repr=False)
    refusal: str | None = None

    def __post_init__(self) -> None:
        if self.role not in ("system", "user", "assistant", "tool"):
            raise LLMError("invalid_request", "Unsupported message role.")
        if not isinstance(self.content, str):
            raise LLMError("invalid_request", "Message content must be text.")
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        if (self.tool_calls or self.continuation or self.refusal is not None) and self.role != "assistant":
            raise LLMError("invalid_request", "Only assistant messages carry model output.")
        if (self.role == "tool") != bool(self.tool_call_id):
            raise LLMError("invalid_request", "Only tool results require a tool_call_id.")


@dataclass(frozen=True)
class GenerationOptions:
    max_output_tokens: int | None = None
    reasoning: ReasoningEffort | None = None
    temperature: float | None = None

    def __post_init__(self) -> None:
        if self.max_output_tokens is not None and (
            type(self.max_output_tokens) is not int or self.max_output_tokens <= 0
        ):
            raise LLMError("invalid_request", "Output token limit must be a positive integer.")
        if self.reasoning not in (None, "none", "low", "medium", "high", "xhigh", "max"):
            raise LLMError("invalid_request", "Unsupported reasoning effort.")
        if self.temperature is not None and (
            type(self.temperature) not in (int, float)
            or not math.isfinite(self.temperature) or not 0 <= self.temperature <= 2
        ):
            raise LLMError("invalid_request", "Temperature must be between 0 and 2.")


@dataclass(frozen=True)
class LLMRequest:
    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...] = ()
    options: GenerationOptions = field(default_factory=GenerationOptions)

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))
        if not self.messages:
            raise LLMError("invalid_request", "At least one message is required.")
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise LLMError("invalid_request", "Tool names must be unique.")
        pending = set()
        seen = set()
        for message in self.messages:
            if message.role == "tool":
                if message.tool_call_id not in pending:
                    raise LLMError("invalid_request", "Tool result has no pending call.")
                pending.remove(message.tool_call_id)
            else:
                if pending:
                    raise LLMError("invalid_request", "Return all tool results before continuing.")
                for call in message.tool_calls:
                    if call.id in seen:
                        raise LLMError("invalid_request", "Tool call IDs must be unique.")
                    seen.add(call.id)
                    pending.add(call.id)
        if pending:
            raise LLMError("invalid_request", "Tool results are missing.")


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class LLMResponse:
    message: Message
    finish_reason: FinishReason
    usage: TokenUsage | None = None

    @property
    def text(self) -> str:
        return self.message.content
