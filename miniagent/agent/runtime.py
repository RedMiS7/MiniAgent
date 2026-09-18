"""One-shot execution and terminal result ownership around AgentLoop."""
import asyncio
from contextlib import aclosing
from typing import Sequence

from miniagent.models import GenerationOptions, LLMError, Message
from .events import AgentCancelled, AgentCompleted, AgentFailed
from .loop import AgentLoop
from .run_types import RunResult, RunState


class AgentRuntime:
    """Own one Run's state; the injected Loop and its dependencies are borrowed."""

    def __init__(self, loop: AgentLoop):
        self._loop = loop
        self._started = False
        self._result: RunResult | None = None

    @property
    def state(self) -> RunState:
        if self._result is not None:
            return self._result.status
        return "running" if self._started else "pending"

    @property
    def result(self) -> RunResult | None:
        """Return terminal facts after execution and Loop cleanup, otherwise None."""
        return self._result

    async def run(
        self, messages: Sequence[Message], options: GenerationOptions | None = None,
        *, max_steps: int = 12,
    ) -> RunResult:
        """Consume the Loop once; save failures before propagating exceptions."""
        if self._started:
            raise RuntimeError("A Run can only be started once.")
        self._started = True
        terminal = None
        try:
            async with aclosing(self._loop.stream(messages, options, max_steps=max_steps)) as events:
                async for event in events:
                    if terminal is not None:
                        raise RuntimeError("Events followed the Loop's terminal event.")
                    if isinstance(event, AgentCompleted):
                        terminal = RunResult(status="succeeded", reason=event.reason,
                                             messages=event.messages)
                    elif isinstance(event, AgentFailed):
                        terminal = RunResult(status="failed", reason=event.reason)
                    elif isinstance(event, AgentCancelled):
                        terminal = RunResult(status="cancelled", reason=event.reason)
            if terminal is None:
                raise RuntimeError("Loop returned no terminal event.")
            if terminal.status == "cancelled":
                raise asyncio.CancelledError()
        except asyncio.CancelledError:
            self._result = RunResult(status="cancelled", reason="cancelled")
            raise
        except LLMError as exc:
            self._result = RunResult(status="failed", reason="model_error",
                                     error_code=exc.code, error_message="Model call failed.")
            raise
        except Exception:
            self._result = RunResult(status="failed", reason="execution_error",
                                     error_code="execution_error", error_message="Run execution failed.")
            raise
        self._result = terminal
        return terminal
