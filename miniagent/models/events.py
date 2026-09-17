"""Typed model-stream events with stable serialization tags."""
from typing import Annotated, Literal, Union

from pydantic import Field

from miniagent._validation import ContractModel
from .types import LLMResponse


class TextDelta(ContractModel):
    type: Literal["llm_text_delta"] = "llm_text_delta"
    text: str


class ToolCallStarted(ContractModel):
    type: Literal["llm_tool_call_started"] = "llm_tool_call_started"
    index: int = Field(ge=0)
    id: str = Field(pattern=r"\S")
    name: str = Field(pattern=r"\S")


class ToolArgumentsDelta(ContractModel):
    type: Literal["llm_tool_arguments_delta"] = "llm_tool_arguments_delta"
    index: int = Field(ge=0)
    delta: str


class ResponseCompleted(ContractModel):
    type: Literal["llm_response_completed"] = "llm_response_completed"
    response: LLMResponse


LLMEvent = Annotated[
    Union[TextDelta, ToolCallStarted, ToolArgumentsDelta, ResponseCompleted],
    Field(discriminator="type"),
]
