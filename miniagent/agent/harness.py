"""Reusable, sequential entry point for independent Agent runs."""
from contextlib import asynccontextmanager
from typing import AsyncIterator, Sequence

from miniagent.models import GenerationOptions, LLM, Message
from miniagent.tools import ToolExecutor
from .loop import AgentLoop
from .runtime import AgentRuntime


class AgentHarness:
    """Borrow dependencies unless model ownership is explicitly transferred."""

    def __init__(self, model: LLM, executor: ToolExecutor, *, owns_model: bool = False):
        self._model = model
        self._executor = executor
        self._owns_model = owns_model
        self._active = False
        self._closed = False

    @asynccontextmanager
    async def run(
        self, messages: Sequence[Message], options: GenerationOptions | None = None,
        *, max_steps: int = 12,
    ) -> AsyncIterator[AgentRuntime]:
        """Consume the Run's events inside this scope; early exit cancels it."""
        if self._closed:
            raise RuntimeError("The Harness is closed.")
        if self._active:
            raise RuntimeError("The Harness already has an active Run.")
        self._active = True
        try:
            runtime = AgentRuntime(AgentLoop(self._model, self._executor))
            async with runtime.stream(messages, options, max_steps=max_steps) as run:
                yield run
        finally:
            self._active = False

    async def aclose(self) -> None:
        """Close owned resources after all Run scopes have exited."""
        if self._active:
            raise RuntimeError("Exit the active Run scope before closing the Harness.")
        if self._closed:
            return
        self._closed = True
        if self._owns_model:
            await self._model.aclose()

    async def __aenter__(self) -> "AgentHarness":
        if self._closed:
            raise RuntimeError("The Harness is closed.")
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            await self.aclose()
        except Exception as cleanup_error:
            if exc is not None:
                raise exc from cleanup_error
            raise
