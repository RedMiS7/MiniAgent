from dataclasses import dataclass
from typing import Union

from .types import LLMResponse


@dataclass(frozen=True)
class TextDelta:
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("Text delta must be text.")


@dataclass(frozen=True)
class ToolCallStarted:
    index: int
    id: str
    name: str

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 0:
            raise ValueError("Tool call index must be a non-negative integer.")
        if any(not isinstance(value, str) or not value.strip() for value in (self.id, self.name)):
            raise ValueError("Tool call ID and name must be non-empty text.")


@dataclass(frozen=True)
class ToolArgumentsDelta:
    index: int
    delta: str

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 0:
            raise ValueError("Tool call index must be a non-negative integer.")
        if not isinstance(self.delta, str):
            raise ValueError("Tool arguments delta must be text.")


@dataclass(frozen=True)
class ResponseCompleted:
    response: LLMResponse

    def __post_init__(self) -> None:
        if not isinstance(self.response, LLMResponse):
            raise ValueError("Response completion requires an LLMResponse.")


LLMEvent = Union[TextDelta, ToolCallStarted, ToolArgumentsDelta, ResponseCompleted]
