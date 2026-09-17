"""Provider-independent, text and function-tool protocol."""
import json
from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from miniagent._validation import ContractModel

ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]
FinishReason = Literal["stop", "tool_calls", "length", "refusal", "content_filter"]
NonEmptyText = Annotated[str, Field(pattern=r"\S")]


class ContinuationState(ContractModel):
    """Opaque JSON state: callers preserve it, only its adapter interprets it."""
    provider: NonEmptyText
    model: NonEmptyText
    payload: str = Field(repr=False)
    version: int = Field(default=1, ge=1)

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value):
        try:
            json.dumps(json.loads(value), allow_nan=False)
        except (ValueError, TypeError):
            raise ValueError("Continuation must contain JSON.") from None
        return value


class ToolDefinition(ContractModel):
    name: NonEmptyText
    description: str
    parameters: dict[str, JsonValue]

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, value):
        if value.get("type") != "object":
            raise ValueError("Tool parameters must use an object schema.")
        try:
            json.dumps(value, allow_nan=False)
        except (ValueError, TypeError):
            raise ValueError("Tool schema must be JSON serializable.") from None
        return value


class ToolCall(ContractModel):
    id: NonEmptyText
    name: NonEmptyText
    # Preserve raw JSON, including invalid/incomplete arguments, for the executor.
    arguments: str


class Message(ContractModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: NonEmptyText | None = None
    continuation: ContinuationState | None = Field(default=None, repr=False)
    refusal: str | None = None

    @field_validator("tool_calls", mode="before")
    @classmethod
    def normalize_calls(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_role_fields(self):
        if (self.tool_calls or self.continuation or self.refusal is not None) and self.role != "assistant":
            raise ValueError("Only assistant messages carry model output.")
        if (self.role == "tool") != (self.tool_call_id is not None):
            raise ValueError("Only tool results require a tool_call_id.")
        return self


class GenerationOptions(ContractModel):
    max_output_tokens: int | None = Field(default=None, gt=0)
    reasoning: ReasoningEffort | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)


class LLMRequest(ContractModel):
    messages: tuple[Message, ...] = Field(min_length=1)
    tools: tuple[ToolDefinition, ...] = ()
    options: GenerationOptions = Field(default_factory=GenerationOptions)

    @field_validator("messages", "tools", mode="before")
    @classmethod
    def normalize_sequences(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_tool_history(self):
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("Tool names must be unique.")
        pending = set()
        seen = set()
        for message in self.messages:
            if message.role == "tool":
                if message.tool_call_id not in pending:
                    raise ValueError("Tool result has no pending call.")
                pending.remove(message.tool_call_id)
            else:
                if pending:
                    raise ValueError("Return all tool results before continuing.")
                for call in message.tool_calls:
                    if call.id in seen:
                        raise ValueError("Tool call IDs must be unique.")
                    seen.add(call.id)
                    pending.add(call.id)
        if pending:
            raise ValueError("Tool results are missing.")
        return self


class TokenUsage(ContractModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class LLMResponse(ContractModel):
    message: Message
    finish_reason: FinishReason
    usage: TokenUsage | None = None

    @model_validator(mode="after")
    def validate_response(self):
        if self.message.role != "assistant":
            raise ValueError("Response must contain an assistant message.")
        if self.finish_reason == "tool_calls" and not self.message.tool_calls:
            raise ValueError("Tool finish requires a tool call.")
        if self.finish_reason == "stop" and self.message.tool_calls:
            raise ValueError("Tool calls cannot finish with stop.")
        return self

    @property
    def text(self) -> str:
        return self.message.content
