from dataclasses import dataclass
from typing import Union

from .types import LLMResponse


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallStarted:
    index: int
    id: str
    name: str


@dataclass(frozen=True)
class ToolArgumentsDelta:
    index: int
    delta: str


@dataclass(frozen=True)
class ResponseCompleted:
    response: LLMResponse


LLMEvent = Union[TextDelta, ToolCallStarted, ToolArgumentsDelta, ResponseCompleted]
