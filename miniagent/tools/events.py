"""Tool execution events; run association is owned by the caller."""
from typing import Annotated, Literal, Union

from pydantic import Field

from miniagent._validation import ContractModel


class ToolStarted(ContractModel):
    type: Literal["tool_started"] = "tool_started"
    call_id: str = Field(pattern=r"\S")
    tool_name: str = Field(pattern=r"\S")


class ToolCompleted(ContractModel):
    type: Literal["tool_completed"] = "tool_completed"
    call_id: str = Field(pattern=r"\S")
    tool_name: str = Field(pattern=r"\S")
    elapsed_seconds: float = Field(default=0.0, ge=0)


class ToolFailed(ContractModel):
    type: Literal["tool_failed"] = "tool_failed"
    call_id: str = Field(pattern=r"\S")
    tool_name: str = Field(pattern=r"\S")
    elapsed_seconds: float = Field(default=0.0, ge=0)
    error_code: str | None = Field(default=None, pattern=r"\S")


class ToolCancelled(ContractModel):
    type: Literal["tool_cancelled"] = "tool_cancelled"
    call_id: str = Field(pattern=r"\S")
    tool_name: str = Field(pattern=r"\S")
    elapsed_seconds: float = Field(default=0.0, ge=0)


ToolEvent = Annotated[
    Union[ToolStarted, ToolCompleted, ToolFailed, ToolCancelled],
    Field(discriminator="type"),
]
