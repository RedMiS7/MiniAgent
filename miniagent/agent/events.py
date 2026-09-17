"""Run context wraps component events without changing their interfaces."""
from dataclasses import dataclass, field
from typing import Literal

from miniagent.models.events import (
    LLMEvent, ResponseCompleted, TextDelta, ToolArgumentsDelta, ToolCallStarted,
)
from miniagent.tools import ToolEvent


@dataclass(frozen=True)
class AgentEvent:
    """Run lifecycle notification; progress marks a completed model/tool step."""
    type: Literal["started", "progress", "completed", "failed", "cancelled"]
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.type not in ("started", "progress", "completed", "failed", "cancelled"):
            raise ValueError("Unsupported agent event type.")
        if self.type in ("started", "progress"):
            if self.reason is not None:
                raise ValueError("Only terminal agent events carry a reason.")
        else:
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("Terminal agent events require a reason code.")
            if self.type == "completed" and self.reason != "stop":
                raise ValueError("Successful completion requires the stop reason.")
            if self.type == "cancelled" and self.reason != "cancelled":
                raise ValueError("Cancellation requires the cancelled reason.")
            if self.type == "failed" and self.reason in ("stop", "cancelled"):
                raise ValueError("Failure requires a failure reason.")


@dataclass(frozen=True)
class RunEvent:
    """Tool events reference the model call that produced the tool request.

    IDs are assigned by the caller; uniqueness and event ordering belong to
    the future Loop/Runtime. Payloads may contain sensitive model output.
    """
    run_id: str
    event: AgentEvent | LLMEvent | ToolEvent = field(repr=False)
    model_call_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("Run events require a non-empty run ID.")
        if isinstance(self.event, AgentEvent):
            if self.model_call_id is not None:
                raise ValueError("Agent events belong to a run, not a model call.")
        elif isinstance(self.event, (TextDelta, ToolCallStarted, ToolArgumentsDelta,
                                     ResponseCompleted, ToolEvent)):
            if not isinstance(self.model_call_id, str) or not self.model_call_id.strip():
                raise ValueError("Component events require a non-empty model call ID.")
        else:
            raise ValueError("Unsupported run event payload.")
