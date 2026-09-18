import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from miniagent.agent import AgentCompleted, AgentLoop, AgentRuntime, AgentStarted
from miniagent.models import (
    ContinuationState, GenerationOptions, LLMError, LLMResponse, Message,
    ResponseCompleted, ToolCall,
)
from miniagent.tools import ToolContext, ToolExecutor, ToolRegistry


def response(text="Done", *, calls=(), reason=None, continuation=None):
    return LLMResponse(
        message=Message(role="assistant", content=text, tool_calls=calls, continuation=continuation),
        finish_reason=reason or ("tool_calls" if calls else "stop"),
    )


class FakeModel:
    def __init__(self, *responses):
        self.responses = responses
        self.requests = []
        self.closed_streams = 0
        self.closed = False

    async def stream(self, request):
        self.requests.append(request)
        try:
            item = self.responses[len(self.requests) - 1]
            if isinstance(item, BaseException):
                raise item
            yield ResponseCompleted(response=item)
        finally:
            self.closed_streams += 1

    async def aclose(self):
        self.closed = True


class AgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.executor = ToolExecutor(ToolRegistry(), ToolContext(Path(self.temp.name)))
        self.messages = [Message(role="user", content="Complete the task.")]

    def runtime(self, model):
        return AgentRuntime(AgentLoop(model, self.executor))

    async def test_success_retains_history_options_and_borrowed_model(self):
        continuation = ContinuationState(provider="fake", model="fake", payload='{"state":1}')
        model = FakeModel(response(continuation=continuation))
        runtime = self.runtime(model)
        self.assertEqual(runtime.state, "pending")
        self.assertIsNone(runtime.result)
        options = GenerationOptions(max_output_tokens=100)
        result = await runtime.run(self.messages, options)
        self.assertEqual(runtime.state, "succeeded")
        self.assertIs(runtime.result, result)
        self.assertEqual(result.messages[:-1], tuple(self.messages))
        self.assertIs(result.messages[-1].continuation, continuation)
        self.assertEqual(model.requests[0].options, options)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(model.closed_streams, 1)
        self.assertFalse(model.closed)
        with self.assertRaises(AttributeError):
            runtime.state = "pending"
        with self.assertRaises(AttributeError):
            runtime.result = None

    async def test_non_exception_failures_return_results(self):
        for reason in ("length", "refusal", "content_filter"):
            with self.subTest(reason=reason):
                runtime = self.runtime(FakeModel(response(reason=reason)))
                result = await runtime.run(self.messages)
                self.assertEqual(runtime.state, "failed")
                self.assertEqual(result.reason, reason)
                self.assertEqual(result.messages, ())
                self.assertIsNone(result.error_code)

    async def test_step_limit_does_not_start_tools(self):
        observed = []
        self.executor.on_event = observed.append
        call = ToolCall(id="c", name="unregistered", arguments="{}")
        model = FakeModel(response(calls=(call,)))
        runtime = self.runtime(model)
        result = await runtime.run(self.messages, max_steps=1)
        self.assertEqual(result.reason, "step_limit")
        self.assertEqual(result.status, "failed")
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(observed, [])

    async def test_tool_failure_can_end_in_success(self):
        call = ToolCall(id="c", name="unregistered", arguments="{}")
        model = FakeModel(response(calls=(call,)), response("Tool unavailable."))
        result = await self.runtime(model).run(self.messages)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.messages[2].tool_call_id, call.id)
        self.assertFalse(json.loads(result.messages[2].content)["success"])
        self.assertEqual(model.requests[1].messages, result.messages[:-1])

    async def test_model_error_is_saved_and_original_exception_propagates(self):
        error = LLMError("connection", "private-diagnostic")
        model = FakeModel(error)
        runtime = self.runtime(model)
        with self.assertRaises(LLMError) as caught:
            await runtime.run(self.messages)
        self.assertIs(caught.exception, error)
        self.assertEqual(runtime.state, "failed")
        self.assertEqual(runtime.result.reason, "model_error")
        self.assertEqual(runtime.result.error_code, "connection")
        self.assertNotIn("private-diagnostic", runtime.result.model_dump_json())
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(model.closed_streams, 1)

    async def test_framework_error_is_saved_without_raw_message(self):
        error = RuntimeError("private-tool-input")
        runtime = self.runtime(FakeModel(error))
        with self.assertRaises(RuntimeError) as caught:
            await runtime.run(self.messages)
        self.assertIs(caught.exception, error)
        self.assertEqual(runtime.result.reason, "execution_error")
        self.assertEqual(runtime.result.error_code, "execution_error")
        self.assertNotIn("private-tool-input", runtime.result.model_dump_json())

    async def test_completed_run_cannot_restart_or_replace_result(self):
        for item in (response(), response(reason="length"), LLMError("api", "Failed")):
            with self.subTest(item=item):
                model = FakeModel(item)
                runtime = self.runtime(model)
                try:
                    await runtime.run(self.messages)
                except LLMError:
                    pass
                original = runtime.result
                with self.assertRaises(RuntimeError):
                    await runtime.run(self.messages)
                self.assertIs(runtime.result, original)
                self.assertEqual(len(model.requests), 1)

    async def test_running_instance_rejects_second_start(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class BlockingModel(FakeModel):
            async def stream(self, request):
                entered.set()
                await release.wait()
                async for event in super().stream(request):
                    yield event

        model = BlockingModel(response())
        runtime = self.runtime(model)
        task = asyncio.create_task(runtime.run(self.messages))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual(runtime.state, "running")
            self.assertIsNone(runtime.result)
            with self.assertRaises(RuntimeError):
                await runtime.run(self.messages)
        finally:
            release.set()
            await task
        self.assertEqual(runtime.state, "succeeded")
        self.assertEqual(len(model.requests), 1)

    async def test_terminal_result_waits_for_loop_cleanup(self):
        cleaning, release = asyncio.Event(), asyncio.Event()

        class FinishingLoop:
            async def stream(self, messages, options, *, max_steps):
                try:
                    yield AgentCompleted(messages=(*messages, Message(role="assistant", content="Done")))
                finally:
                    cleaning.set()
                    await release.wait()

        runtime = AgentRuntime(FinishingLoop())
        task = asyncio.create_task(runtime.run(self.messages))
        try:
            await asyncio.wait_for(cleaning.wait(), 2)
            self.assertEqual(runtime.state, "running")
            self.assertIsNone(runtime.result)
        finally:
            release.set()
            await task
        self.assertEqual(runtime.state, "succeeded")

    async def test_cleanup_failure_does_not_publish_success(self):
        error = RuntimeError("private-cleanup-error")

        class BrokenCleanupLoop:
            async def stream(self, messages, options, *, max_steps):
                try:
                    yield AgentCompleted(messages=(*messages, Message(role="assistant", content="Done")))
                finally:
                    raise error

        runtime = AgentRuntime(BrokenCleanupLoop())
        with self.assertRaises(RuntimeError) as caught:
            await runtime.run(self.messages)
        self.assertIs(caught.exception, error)
        self.assertEqual(runtime.state, "failed")
        self.assertEqual(runtime.result.reason, "execution_error")
        self.assertEqual(runtime.result.messages, ())

    async def test_caller_cancellation_waits_for_cleanup_and_remains_queryable(self):
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class WaitingModel(FakeModel):
            async def stream(self, request):
                self.requests.append(request)
                try:
                    entered.set()
                    await asyncio.Event().wait()
                    yield ResponseCompleted(response=response())
                finally:
                    cleaning.set()
                    await release.wait()
                    self.closed_streams += 1

        model = WaitingModel()
        runtime = self.runtime(model)
        task = asyncio.create_task(runtime.run(self.messages))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.wait_for(cleaning.wait(), 2)
            self.assertEqual(runtime.state, "running")
            self.assertIsNone(runtime.result)
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(runtime.state, "cancelled")
        self.assertEqual(runtime.result.reason, "cancelled")
        self.assertEqual(model.closed_streams, 1)
        self.assertFalse(model.closed)
        original = runtime.result
        with self.assertRaises(RuntimeError):
            await runtime.run(self.messages)
        self.assertIs(runtime.result, original)

    async def test_separate_runtimes_do_not_share_history_or_failure(self):
        model = FakeModel(LLMError("api", "Failed"), response("Second answer"))
        loop = AgentLoop(model, self.executor)
        first, second = AgentRuntime(loop), AgentRuntime(loop)
        with self.assertRaises(LLMError):
            await first.run(self.messages)
        second_messages = [Message(role="user", content="Independent task")]
        result = await second.run(second_messages)
        self.assertEqual(first.state, "failed")
        self.assertEqual(second.state, "succeeded")
        self.assertEqual(model.requests[-1].messages, tuple(second_messages))
        self.assertEqual(result.messages[0], second_messages[0])
        self.assertFalse(model.closed)

    async def test_missing_or_repeated_terminal_events_fail(self):
        completed = AgentCompleted(messages=(Message(role="assistant", content="Done"),))

        class InvalidLoop:
            def __init__(self, events):
                self.events = events
                self.closed = False

            async def stream(self, messages, options, *, max_steps):
                try:
                    for event in self.events:
                        yield event
                finally:
                    self.closed = True

        for events in ([], [AgentStarted()], [completed, completed], [completed, AgentStarted()]):
            with self.subTest(events=events):
                loop = InvalidLoop(events)
                runtime = AgentRuntime(loop)
                with self.assertRaises(RuntimeError):
                    await runtime.run(self.messages)
                self.assertEqual(runtime.state, "failed")
                self.assertTrue(loop.closed)

    async def test_invalid_limit_is_recorded_without_model_call(self):
        model = FakeModel(response())
        runtime = self.runtime(model)
        with self.assertRaises(ValueError):
            await runtime.run(self.messages, max_steps=0)
        self.assertEqual(runtime.state, "failed")
        self.assertEqual(runtime.result.reason, "execution_error")
        self.assertEqual(model.requests, [])


if __name__ == "__main__":
    unittest.main()
