"""A single run's streaming model/tool loop; dependencies are caller-owned."""
import asyncio
from contextlib import aclosing, suppress
from typing import Annotated, AsyncIterator, Sequence, Union

from pydantic import Field

from miniagent.models import (
    GenerationOptions, LLM, LLMError, LLMEvent, LLMRequest, Message, ResponseCompleted,
)
from miniagent.tools import ToolEvent, ToolExecutor
from .events import (
    AgentCancelled, AgentCompleted, AgentEvent, AgentFailed, AgentProgress, AgentStarted,
)

LoopEvent = Annotated[Union[AgentEvent, LLMEvent, ToolEvent], Field(discriminator="type")]


class AgentLoop:
    def __init__(self, model: LLM, executor: ToolExecutor):
        self.model = model
        self.executor = executor

    async def stream(
        self, messages: Sequence[Message], options: GenerationOptions | None = None,
        *, max_steps: int = 12,
    ) -> AsyncIterator[LoopEvent]:
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1:
            raise ValueError("max_steps must be a positive integer.")
        request = LLMRequest(messages=tuple(messages), tools=self.executor.registry.definitions(),
                             options=options or GenerationOptions())
        history = list(request.messages)
        seen = {call.id for message in history for call in message.tool_calls}
        yield AgentStarted()
        try:
            for step in range(1, max_steps + 1):
                yield AgentProgress(step=step, phase="model")
                response = None
                async with aclosing(self.model.stream(request)) as events:
                    async for event in events:
                        if response is not None:
                            raise LLMError("invalid_response", "Events followed the final response.")
                        if isinstance(event, ResponseCompleted):
                            response = event.response
                        yield event
                if response is None:
                    raise LLMError("invalid_response", "Stream returned no final response.")
                history.append(response.message)
                if response.finish_reason == "stop":
                    yield AgentCompleted(messages=tuple(history))
                    return
                if response.finish_reason != "tool_calls":
                    yield AgentFailed(reason=response.finish_reason)
                    return
                calls = response.message.tool_calls
                ids = [call.id for call in calls]
                if len(ids) != len(set(ids)) or seen.intersection(ids):
                    raise LLMError("invalid_response", "Tool call IDs must be unique.")
                seen.update(ids)
                # Leave a model round to consume results before performing side effects.
                if step == max_steps:
                    yield AgentFailed(reason="step_limit")
                    return
                yield AgentProgress(step=step, phase="tools")
                for call in calls:
                    queue = asyncio.Queue()
                    task = asyncio.create_task(self.executor.execute(call, on_event=queue.put_nowait))
                    task.add_done_callback(lambda done, q=queue: q.put_nowait(None))
                    try:
                        while True:
                            event = await queue.get()
                            if event is None:
                                break
                            yield event
                        result = await task
                    finally:
                        if not task.done():
                            task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task
                    history.append(result.to_message(call.id))
                yield AgentProgress(step=step, phase="completed")
                request = LLMRequest(messages=history, tools=request.tools, options=request.options)
        except asyncio.CancelledError:
            yield AgentCancelled()
            raise
        except LLMError:
            yield AgentFailed(reason="model_error")
            raise
        except Exception:
            yield AgentFailed(reason="execution_error")
            raise
