"""Reusable, sequential entry point for independent Agent runs."""
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable, Sequence

from miniagent.models import GenerationOptions, LLM, Message
from miniagent.tools import ToolExecutor
from .approval import ApprovalCallback, ApprovalExecutor
from .loop import AgentLoop, LoopEvent
from .runtime import AgentRuntime
from .retry import RetryCallback, RetryPolicy, RetryingModel


class AgentHarness:
    """Borrow dependencies unless model ownership is explicitly transferred."""

    def __init__(
        self, model: LLM, executor: ToolExecutor, *, owns_model: bool = False,
        approval_required: Sequence[str] = (), approve: ApprovalCallback | None = None,
        on_event: Callable[[AgentRuntime, LoopEvent], None] | None = None,
        retry_policy: RetryPolicy | None = None, on_retry: RetryCallback | None = None,
    ):
        if on_event is not None:
            import inspect
            if (not callable(on_event) or inspect.iscoroutinefunction(on_event)
                    or inspect.iscoroutinefunction(getattr(on_event, "__call__", None))):
                raise TypeError("on_event must be a synchronous callback.")
        self._on_event = on_event
        self._model = model
        self._run_model = RetryingModel(model, retry_policy, on_retry) if retry_policy is not None else model
        required = frozenset(approval_required)
        if required and approve is None:
            raise ValueError("An approval callback is required for protected tools.")
        unknown = required - {tool.name for tool in executor.registry.definitions()}
        if unknown:
            raise ValueError("Approval policy references unregistered tools.")
        self._executor = ApprovalExecutor(executor, required, approve) if required else executor
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
            def notify(event):
                self._on_event(runtime, event)

            runtime = AgentRuntime(AgentLoop(self._run_model, self._executor),
                                   on_event=notify if self._on_event is not None else None)
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
