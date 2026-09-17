"""Agent lifecycle events; run association is owned by the caller."""
from typing import Annotated, Literal, Union

from pydantic import Field, field_validator

from miniagent._validation import ContractModel
from miniagent.models.types import Message


class AgentStarted(ContractModel):
    type: Literal["agent_started"] = "agent_started"


class AgentProgress(ContractModel):
    """Progress within a numbered model round; completed is the legacy default."""
    type: Literal["agent_progress"] = "agent_progress"
    step: int = Field(default=1, ge=1)
    phase: Literal["model", "tools", "completed"] = "completed"


class AgentCompleted(ContractModel):
    type: Literal["agent_completed"] = "agent_completed"
    reason: Literal["stop"] = "stop"
    messages: tuple[Message, ...] = Field(default=(), repr=False)


class AgentFailed(ContractModel):
    type: Literal["agent_failed"] = "agent_failed"
    reason: str = Field(pattern=r"\S")

    @field_validator("reason")
    @classmethod
    def validate_failure_reason(cls, value):
        if value in ("stop", "cancelled"):
            raise ValueError("Failure requires a failure reason.")
        return value


class AgentCancelled(ContractModel):
    type: Literal["agent_cancelled"] = "agent_cancelled"
    reason: Literal["cancelled"] = "cancelled"


AgentEvent = Annotated[
    Union[AgentStarted, AgentProgress, AgentCompleted, AgentFailed, AgentCancelled],
    Field(discriminator="type"),
]
