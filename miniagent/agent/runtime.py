"""Scoped, single-run execution and terminal result ownership around AgentLoop."""
import asyncio
from contextlib import aclosing, asynccontextmanager
from typing import AsyncIterator, Sequence

from miniagent.models import GenerationOptions, LLMError, Message
from .events import AgentCancelled, AgentCompleted, AgentFailed
from .loop import AgentLoop, LoopEvent
from .run_types import RunResult


class AgentRuntime:
    """Own one Run; the injected Loop and its dependencies are borrowed."""

    def __init__(self, loop: AgentLoop):
        self._loop = loop
        self._started = False
        self._result: RunResult | None = None
        self._task: asyncio.Task | None = None
        self._worker_entered = False
        self._closing = False
        self._cancel_requested = False
        self._scope_active = False
        self._consumer_started = False
        self._demand = asyncio.Event()
        self._ready = asyncio.Event()
        self._event: LoopEvent | None = None
        self._error: BaseException | None = None

    @property
    def result(self) -> RunResult | None:
        """Return terminal facts after execution and Loop cleanup, otherwise None."""
        return self._result

    def cancel(self) -> None:
        """Request cancellation once; scope exit waits for cleanup."""
        if self._result is not None or self._cancel_requested:
            return
        self._cancel_requested = True
        if not self._started:
            self._started = True
            self._result = RunResult(status="cancelled", reason="cancelled")
        elif self._worker_entered and not self._closing and self._task is not None:
            self._task.cancel()

    @asynccontextmanager
    async def stream(
        self, messages: Sequence[Message], options: GenerationOptions | None = None,
        *, max_steps: int = 12,
    ) -> AsyncIterator["AgentRuntime"]:
        """Keep event consumption inside this scope; early exit cancels the Run."""
        if self._started:
            raise RuntimeError("A Run can only be started once.")
        self._started = True
        self._scope_active = True
        self._task = asyncio.create_task(self._execute(messages, options, max_steps))
        try:
            yield self
        finally:
            self.cancel()
            interrupted = False
            # Repeated caller cancellation must not interrupt the worker's cleanup.
            while not self._task.done():
                try:
                    await asyncio.shield(self._task)
                except asyncio.CancelledError:
                    interrupted = True
            self._scope_active = False
            self._task.result()
            if interrupted:
                raise asyncio.CancelledError()
            if self._error is not None and not isinstance(self._error, asyncio.CancelledError):
                raise self._error

    async def events(self) -> AsyncIterator[LoopEvent]:
        """Consume once, with at most one event in flight and no read-ahead calls."""
        if not self._scope_active or self._consumer_started:
            raise RuntimeError("Events require an active scope and a single consumer.")
        self._consumer_started = True
        while True:
            if not self._scope_active:
                raise RuntimeError("The Run scope is closed.")
            if self._result is not None and self._event is None:
                if self._error is not None:
                    raise self._error
                return
            self._demand.set()
            await self._ready.wait()
            self._ready.clear()
            event = self._event
            self._event = None
            if event is not None:
                yield event

    async def run(
        self, messages: Sequence[Message], options: GenerationOptions | None = None,
        *, max_steps: int = 12,
    ) -> RunResult:
        """Consume all events; retain the same result and exception semantics."""
        async with self.stream(messages, options, max_steps=max_steps) as run:
            async for _ in run.events():
                pass
        return self._result

    def _check_cancelled(self):
        if self._cancel_requested:
            raise asyncio.CancelledError()

    async def _execute(self, messages, options, max_steps):
        self._worker_entered = True
        terminal = None
        try:
            async with aclosing(self._loop.stream(messages, options, max_steps=max_steps)) as events:
                try:
                    while terminal is None:
                        self._check_cancelled()
                        await self._demand.wait()
                        self._demand.clear()
                        self._check_cancelled()
                        try:
                            event = await anext(events)
                        except StopAsyncIteration:
                            raise RuntimeError("Loop returned no terminal event.") from None
                        self._check_cancelled()
                        if isinstance(event, AgentCompleted):
                            terminal = RunResult(status="succeeded", reason=event.reason,
                                                 messages=event.messages)
                        elif isinstance(event, AgentFailed):
                            terminal = RunResult(status="failed", reason=event.reason)
                        elif isinstance(event, AgentCancelled):
                            terminal = RunResult(status="cancelled", reason=event.reason)
                        else:
                            self._event = event
                            self._ready.set()
                    self._closing = True
                    # Finish cleanup and collect any exception before exposing a terminal event.
                    try:
                        await anext(events)
                    except StopAsyncIteration:
                        pass
                    else:
                        raise RuntimeError("Events followed the Loop's terminal event.")
                finally:
                    self._closing = True
            self._check_cancelled()
            if terminal.status == "cancelled":
                raise asyncio.CancelledError()
        except asyncio.CancelledError as exc:
            self._error = exc
            self._result = RunResult(status="cancelled", reason="cancelled")
        except LLMError as exc:
            self._error = exc
            self._result = RunResult(status="failed", reason="model_error",
                                     error_code=exc.code, error_message="Model call failed.")
        except Exception as exc:
            self._error = exc
            self._result = RunResult(status="failed", reason="execution_error",
                                     error_code="execution_error", error_message="Run execution failed.")
        else:
            self._result = terminal
        if self._result.status == "succeeded":
            self._event = AgentCompleted(messages=self._result.messages)
        elif self._result.status == "cancelled":
            self._event = AgentCancelled()
        else:
            self._event = AgentFailed(reason=self._result.reason)
        self._ready.set()
