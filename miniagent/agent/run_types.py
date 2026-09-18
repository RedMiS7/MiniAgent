"""Single-run state names and validated terminal results."""
from typing import Literal

from pydantic import Field, field_validator, model_validator

from miniagent._validation import ContractModel
from miniagent.models import LLMRequest, Message
from miniagent.models.types import NonEmptyText

RunState = Literal["pending", "running", "succeeded", "failed", "cancelled"]


class RunResult(ContractModel):
    """Terminal facts; only successful runs expose continuation-ready history."""
    status: Literal["succeeded", "failed", "cancelled"]
    reason: NonEmptyText
    messages: tuple[Message, ...] = Field(default=(), repr=False)
    error_code: NonEmptyText | None = None
    error_message: NonEmptyText | None = Field(default=None, repr=False)

    @field_validator("messages", mode="before")
    @classmethod
    def normalize_messages(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_result(self):
        if (self.error_code is None) != (self.error_message is None):
            raise ValueError("Error code and message must be supplied together.")
        if self.status == "succeeded":
            if self.reason != "stop" or self.error_code is not None:
                raise ValueError("Success requires stop and no error.")
            LLMRequest(messages=self.messages)
            if self.messages[-1].role != "assistant" or self.messages[-1].tool_calls:
                raise ValueError("Success requires a final assistant answer.")
        else:
            if self.messages:
                raise ValueError("Only success exposes continuation-ready history.")
            if self.status == "cancelled":
                if self.reason != "cancelled" or self.error_code is not None:
                    raise ValueError("Cancellation requires cancelled and no error.")
            elif self.reason in ("stop", "cancelled", "tool_calls"):
                raise ValueError("Failure requires a terminal failure reason.")
        return self
