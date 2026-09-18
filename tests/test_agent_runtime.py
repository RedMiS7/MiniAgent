import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import miniagent.agent as agent
from miniagent.agent import AgentCompleted, AgentFailed, AgentLoop, AgentRuntime, AgentStarted
from miniagent.models import (
    ContinuationState, GenerationOptions, LLMError, LLMResponse, Message,
    ResponseCompleted, TextDelta, ToolCall, ToolDefinition,
)
from miniagent.tools import ToolContext, ToolExecutor, ToolRegistry, ToolResult


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
        self.assertIsNone(runtime.result)
        options = GenerationOptions(max_output_tokens=100)
        result = await runtime.run(self.messages, options)
        self.assertEqual(runtime.result.status, "succeeded")
        self.assertIs(runtime.result, result)
        self.assertEqual(result.messages[:-1], tuple(self.messages))
        self.assertIs(result.messages[-1].continuation, continuation)
        self.assertEqual(model.requests[0].options, options)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(model.closed_streams, 1)
        self.assertFalse(model.closed)
        with self.assertRaises(AttributeError):
            runtime.result = None

    async def test_non_exception_failures_return_results(self):
        for reason in ("length", "refusal", "content_filter"):
            with self.subTest(reason=reason):
                runtime = self.runtime(FakeModel(response(reason=reason)))
                result = await runtime.run(self.messages)
                self.assertEqual(runtime.result.status, "failed")
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
        self.assertEqual(runtime.result.status, "failed")
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
            self.assertIsNone(runtime.result)
            with self.assertRaises(RuntimeError):
                await runtime.run(self.messages)
        finally:
            release.set()
            await task
        self.assertEqual(runtime.result.status, "succeeded")
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
            self.assertIsNone(runtime.result)
        finally:
            release.set()
            await task
        self.assertEqual(runtime.result.status, "succeeded")

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
        self.assertEqual(runtime.result.status, "failed")
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
            self.assertIsNone(runtime.result)
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(runtime.result.status, "cancelled")
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
        self.assertEqual(first.result.status, "failed")
        self.assertEqual(second.result.status, "succeeded")
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
                self.assertEqual(runtime.result.status, "failed")
                self.assertTrue(loop.closed)

    async def test_invalid_limit_is_recorded_without_model_call(self):
        model = FakeModel(response())
        runtime = self.runtime(model)
        with self.assertRaises(ValueError):
            await runtime.run(self.messages, max_steps=0)
        self.assertEqual(runtime.result.status, "failed")
        self.assertEqual(runtime.result.reason, "execution_error")
        self.assertEqual(model.requests, [])



    async def test_stream_preserves_order_and_publishes_one_terminal(self):
        model = FakeModel(response())
        runtime = self.runtime(model)
        async with runtime.stream(self.messages) as run:
            self.assertIs(run, runtime)
            events = [event async for event in run.events()]
            self.assertEqual(run.result.status, "succeeded")
        self.assertEqual([event.type for event in events], [
            "agent_started", "agent_progress", "llm_response_completed", "agent_completed",
        ])
        saved = runtime.result
        runtime.cancel()
        runtime.cancel()
        self.assertIs(runtime.result, saved)
        self.assertEqual(model.closed_streams, 1)
        self.assertFalse(model.closed)

    async def test_cancel_before_start_never_calls_model(self):
        model = FakeModel(response())
        runtime = self.runtime(model)
        runtime.cancel()
        saved = runtime.result
        runtime.cancel()
        self.assertEqual(runtime.result.status, "cancelled")
        with self.assertRaises(RuntimeError):
            await runtime.run(self.messages)
        self.assertIs(runtime.result, saved)
        self.assertEqual(model.requests, [])

    async def test_scope_without_consumption_cancels_without_calls(self):
        model = FakeModel(response())
        runtime = self.runtime(model)
        async with runtime.stream(self.messages):
            pass
        self.assertEqual(runtime.result.status, "cancelled")
        self.assertEqual(model.requests, [])

    async def test_break_at_call_boundaries_starts_no_next_call(self):
        call = ToolCall(id="c", name="unregistered", arguments="{}")
        for boundary in ("agent_started", "model", "llm_response_completed", "tools"):
            with self.subTest(boundary=boundary):
                observed = []
                self.executor.on_event = observed.append
                model = FakeModel(response(calls=(call,)))
                runtime = self.runtime(model)
                async with runtime.stream(self.messages) as run:
                    async for event in run.events():
                        if event.type == boundary or getattr(event, "phase", None) == boundary:
                            break
                self.assertEqual(runtime.result.status, "cancelled")
                self.assertEqual(observed, [])
                self.assertEqual(len(model.requests), 0 if boundary in ("agent_started", "model") else 1)
                self.assertEqual(model.closed_streams, len(model.requests))

    async def test_explicit_cancel_between_events_has_one_cancelled_terminal(self):
        model = FakeModel(response())
        runtime = self.runtime(model)
        events = []
        with self.assertRaises(asyncio.CancelledError):
            async with runtime.stream(self.messages) as run:
                async for event in run.events():
                    events.append(event)
                    if event.type == "agent_started":
                        run.cancel()
                        run.cancel()
        self.assertEqual(runtime.result.status, "cancelled")
        self.assertEqual([event.type for event in events], ["agent_started", "agent_cancelled"])
        self.assertEqual(model.requests, [])

    async def test_model_cancel_and_repeated_caller_cancel_do_not_interrupt_cleanup(self):
        entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

        class BlockingModel(FakeModel):
            async def stream(self, request):
                self.requests.append(request)
                try:
                    entered.set()
                    await asyncio.Event().wait()
                    yield ResponseCompleted(response=response())
                finally:
                    cleaning.set()
                    await release.wait()
                    cleaned.set()
                    self.closed_streams += 1

        model = BlockingModel()
        runtime = self.runtime(model)
        task = asyncio.create_task(runtime.run(self.messages))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            runtime.cancel()
            await asyncio.wait_for(cleaning.wait(), 2)
            runtime.cancel()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertFalse(cleaned.is_set())
            self.assertIsNone(runtime.result)
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(cleaned.is_set())
        self.assertEqual(runtime.result.status, "cancelled")
        self.assertEqual(model.closed_streams, 1)
        self.assertEqual(len(model.requests), 1)

    async def test_exit_during_tool_cancels_and_waits_without_starting_second_tool(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        executions = []

        class BlockingTool:
            definition = ToolDefinition(name="wait", description="Wait", parameters={"type": "object"})

            async def execute(self, arguments, context):
                executions.append("wait")
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
                return ToolResult(success=True)

        self.executor.registry.register(BlockingTool())
        calls = tuple(ToolCall(id=str(i), name="wait", arguments="{}") for i in range(2))
        model = FakeModel(response(calls=calls), response())
        runtime = self.runtime(model)
        async with runtime.stream(self.messages) as run:
            async for event in run.events():
                if event.type == "tool_started":
                    self.assertTrue(entered.is_set())
                    break
        self.assertTrue(cleaned.is_set())
        self.assertEqual(executions, ["wait"])
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(runtime.result.status, "cancelled")

    async def test_explicit_cancel_during_tool_does_not_start_next_call(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        executions = []

        class BlockingTool:
            definition = ToolDefinition(name="wait", description="Wait", parameters={"type": "object"})

            async def execute(self, arguments, context):
                executions.append("wait")
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
                return ToolResult(success=True)

        self.executor.registry.register(BlockingTool())
        calls = tuple(ToolCall(id=str(i), name="wait", arguments="{}") for i in range(2))
        model = FakeModel(response(calls=calls), response())
        runtime = self.runtime(model)
        task = asyncio.create_task(runtime.run(self.messages))
        await asyncio.wait_for(entered.wait(), 2)
        runtime.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cleaned.is_set())
        self.assertEqual(executions, ["wait"])
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(runtime.result.status, "cancelled")

    async def test_break_on_completion_preserves_success(self):
        runtime = self.runtime(FakeModel(response()))
        async with runtime.stream(self.messages) as run:
            async for event in run.events():
                if event.type == "agent_completed":
                    self.assertEqual(run.result.status, "succeeded")
                    break
        self.assertEqual(runtime.result.status, "succeeded")

    async def test_scope_body_error_cleans_run_and_preserves_body_exception(self):
        model = FakeModel(response())
        runtime = self.runtime(model)
        error = ValueError("Display failed")
        with self.assertRaises(ValueError) as caught:
            async with runtime.stream(self.messages) as run:
                async for event in run.events():
                    if event.type == "llm_response_completed":
                        raise error
        self.assertIs(caught.exception, error)
        self.assertEqual(runtime.result.status, "cancelled")
        self.assertEqual(model.closed_streams, 1)

    async def test_events_require_scope_and_single_consumer(self):
        runtime = self.runtime(FakeModel(response()))
        with self.assertRaises(RuntimeError):
            await anext(runtime.events())
        async with runtime.stream(self.messages) as run:
            events = run.events()
            await anext(events)
            with self.assertRaises(RuntimeError):
                await anext(run.events())
        with self.assertRaises(RuntimeError):
            await anext(events)
        await events.aclose()

    async def test_stream_failure_emits_failed_before_propagating_original(self):
        error = LLMError("connection", "private-diagnostic")
        runtime = self.runtime(FakeModel(error))
        events = []
        with self.assertRaises(LLMError) as caught:
            async with runtime.stream(self.messages) as run:
                async for event in run.events():
                    events.append(event)
        self.assertIs(caught.exception, error)
        self.assertEqual([event.type for event in events].count("agent_failed"), 1)
        self.assertEqual(events[-1].reason, "model_error")
        self.assertEqual(runtime.result.status, "failed")

    async def test_break_on_failed_event_does_not_hide_exception(self):
        error = LLMError("connection", "Failed")
        runtime = self.runtime(FakeModel(error))
        with self.assertRaises(LLMError) as caught:
            async with runtime.stream(self.messages) as run:
                async for event in run.events():
                    if event.type == "agent_failed":
                        break
        self.assertIs(caught.exception, error)
        self.assertEqual(runtime.result.status, "failed")


    async def test_cancel_during_terminal_cleanup_waits_until_cleanup_finishes(self):
        cleaning, release, cleaned = (asyncio.Event() for _ in range(3))

        class FinishingLoop:
            async def stream(self, messages, options, *, max_steps):
                try:
                    yield AgentCompleted(messages=(*messages, Message(role="assistant", content="Done")))
                finally:
                    cleaning.set()
                    await release.wait()
                    cleaned.set()

        runtime = AgentRuntime(FinishingLoop())
        task = asyncio.create_task(runtime.run(self.messages))
        try:
            await asyncio.wait_for(cleaning.wait(), 2)
            runtime.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertIsNone(runtime.result)
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(cleaned.is_set())
        self.assertEqual(runtime.result.status, "cancelled")

    async def test_model_suppressing_cancel_cannot_start_tools(self):
        entered = asyncio.Event()
        observed = []
        self.executor.on_event = observed.append
        call = ToolCall(id="c", name="unregistered", arguments="{}")

        class SuppressingModel(FakeModel):
            async def stream(self, request):
                self.requests.append(request)
                try:
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        pass
                    yield ResponseCompleted(response=response(calls=(call,)))
                finally:
                    self.closed_streams += 1

        model = SuppressingModel()
        runtime = self.runtime(model)
        task = asyncio.create_task(runtime.run(self.messages))
        await asyncio.wait_for(entered.wait(), 2)
        runtime.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(observed, [])
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(model.closed_streams, 1)
        self.assertEqual(runtime.result.status, "cancelled")

    async def test_invalid_messages_still_save_failure(self):
        runtime = self.runtime(FakeModel(response()))
        with self.assertRaises(TypeError):
            await runtime.run(None)
        self.assertEqual(runtime.result.status, "failed")
        self.assertEqual(runtime.result.reason, "execution_error")


    async def test_text_delta_is_delivered_before_model_completion(self):
        class StreamingModel(FakeModel):
            async def stream(self, request):
                self.requests.append(request)
                try:
                    yield TextDelta(text="partial")
                    await asyncio.Event().wait()
                finally:
                    self.closed_streams += 1

        model = StreamingModel()
        runtime = self.runtime(model)
        async with runtime.stream(self.messages) as run:
            async for event in run.events():
                if event.type == "llm_text_delta":
                    self.assertEqual(event.text, "partial")
                    self.assertIsNone(run.result)
                    break
        self.assertEqual(runtime.result.status, "cancelled")
        self.assertEqual(model.closed_streams, 1)

    async def test_exit_after_tool_completion_preserves_side_effect_without_next_call(self):
        executions = []

        class CountingTool:
            definition = ToolDefinition(name="count", description="Count", parameters={"type": "object"})

            async def execute(self, arguments, context):
                executions.append("count")
                return ToolResult(success=True)

        self.executor.registry.register(CountingTool())
        calls = tuple(ToolCall(id=str(i), name="count", arguments="{}") for i in range(2))
        model = FakeModel(response(calls=calls), response())
        runtime = self.runtime(model)
        async with runtime.stream(self.messages) as run:
            async for event in run.events():
                if event.type == "tool_completed":
                    break
        self.assertEqual(executions, ["count"])
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(runtime.result.status, "cancelled")

    def test_no_parallel_state_api(self):
        runtime = self.runtime(FakeModel(response()))
        self.assertNotIn("RunState", agent.__all__)
        self.assertFalse(hasattr(agent, "RunState"))
        self.assertFalse(hasattr(runtime, "state"))

    async def test_complete_agent_events_report_run_outcome(self):
        call = ToolCall(id="c", name="unregistered", arguments="{}")
        error = LLMError("connection", "Failed")
        cases = [
            ((response(),), {}, False, "agent_completed", "stop", None),
            ((response(reason="length"),), {}, False, "agent_failed", "length", None),
            ((response(calls=(call,)),), {"max_steps": 1}, False, "agent_failed", "step_limit", None),
            ((response(calls=(call,)), response()), {}, False, "agent_completed", "stop", None),
            ((error,), {}, False, "agent_failed", "model_error", error),
            ((response(),), {}, True, "agent_cancelled", "cancelled", None),
        ]
        for responses, options, cancel, terminal_type, reason, expected_error in cases:
            with self.subTest(terminal_type=terminal_type, reason=reason, responses=responses):
                runtime = self.runtime(FakeModel(*responses))
                events = []
                caught = None
                try:
                    async with runtime.stream(self.messages, **options) as run:
                        async for event in run.events():
                            if event.type.startswith("agent_"):
                                events.append(event)
                            if cancel and event.type == "agent_started":
                                run.cancel()
                except (LLMError, asyncio.CancelledError) as exc:
                    caught = exc
                self.assertEqual(events[0].type, "agent_started")
                self.assertEqual(events[-1].type, terminal_type)
                self.assertEqual(events[-1].reason, reason)
                terminals = [event for event in events if event.type in (
                    "agent_completed", "agent_failed", "agent_cancelled",
                )]
                self.assertEqual(len(terminals), 1)
                if cancel:
                    self.assertIsInstance(caught, asyncio.CancelledError)
                else:
                    self.assertIs(caught, expected_error)



    async def test_execution_error_survives_stream_close_error(self):
        for primary in (LLMError("connection", "private-primary"), RuntimeError("private-primary")):
            with self.subTest(primary=type(primary).__name__):
                cleanup = RuntimeError("private-cleanup")

                class BrokenStream:
                    def __aiter__(self):
                        return self

                    async def __anext__(self):
                        raise primary

                    async def aclose(self):
                        raise cleanup

                class BrokenLoop:
                    def stream(self, messages, options, *, max_steps):
                        return BrokenStream()

                runtime = AgentRuntime(BrokenLoop())
                events = []
                with self.assertRaises(type(primary)) as caught:
                    async with runtime.stream(self.messages) as run:
                        async for event in run.events():
                            events.append(event)
                self.assertIs(caught.exception, primary)
                self.assertIs(caught.exception.__cause__, cleanup)
                self.assertEqual(runtime.result.reason,
                                 "model_error" if isinstance(primary, LLMError) else "execution_error")
                self.assertIsNotNone(runtime.result.cleanup_error)
                self.assertNotIn("private", runtime.result.model_dump_json())
                self.assertEqual([e.type for e in events], ["agent_failed"])

    async def test_non_exception_failure_survives_stream_close_error(self):
        cleanup = RuntimeError("private-cleanup")

        class BrokenStream:
            def __init__(self):
                self.sent = False

            async def __anext__(self):
                if self.sent:
                    raise StopAsyncIteration
                self.sent = True
                return AgentFailed(reason="step_limit")

            async def aclose(self):
                raise cleanup

        class BrokenLoop:
            def stream(self, messages, options, *, max_steps):
                return BrokenStream()

        runtime = AgentRuntime(BrokenLoop())
        with self.assertRaises(RuntimeError) as caught:
            await runtime.run(self.messages)
        self.assertIs(caught.exception, cleanup)
        self.assertEqual(runtime.result.reason, "step_limit")
        self.assertIsNotNone(runtime.result.cleanup_error)
        self.assertEqual(runtime.result.messages, ())

    async def test_success_candidate_with_close_error_is_failed(self):
        cleanup = RuntimeError("private-cleanup")

        class BrokenStream:
            def __init__(self):
                self.sent = False

            async def __anext__(self):
                if self.sent:
                    raise StopAsyncIteration
                self.sent = True
                return AgentCompleted(messages=(Message(role="assistant", content="Done"),))

            async def aclose(self):
                raise cleanup

        class BrokenLoop:
            def stream(self, messages, options, *, max_steps):
                return BrokenStream()

        runtime = AgentRuntime(BrokenLoop())
        events = []
        with self.assertRaises(RuntimeError) as caught:
            async with runtime.stream(self.messages) as run:
                async for event in run.events():
                    events.append(event)
        self.assertIs(caught.exception, cleanup)
        self.assertEqual(runtime.result.status, "failed")
        self.assertIsNotNone(runtime.result.cleanup_error)
        self.assertEqual(runtime.result.messages, ())
        self.assertEqual([e.type for e in events], ["agent_failed"])

    async def test_active_cancellation_survives_stream_close_error(self):
        entered = asyncio.Event()
        cleanup = RuntimeError("private-cleanup")

        class BrokenStream:
            async def __anext__(self):
                entered.set()
                await asyncio.Event().wait()

            async def aclose(self):
                raise cleanup

        class BrokenLoop:
            def stream(self, messages, options, *, max_steps):
                return BrokenStream()

        runtime = AgentRuntime(BrokenLoop())
        propagated = []

        async def consume():
            try:
                await runtime.run(self.messages)
            except asyncio.CancelledError as exc:
                propagated.append(exc)
                raise

        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), 2)
        runtime.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIs(propagated[0].__cause__, cleanup)
        self.assertEqual(runtime.result.status, "cancelled")
        self.assertIsNotNone(runtime.result.cleanup_error)

    async def test_scope_body_error_survives_close_error(self):
        primary = ValueError("private-display")
        cleanup = RuntimeError("private-cleanup")

        class BrokenStream:
            async def __anext__(self):
                return AgentStarted()

            async def aclose(self):
                raise cleanup

        class BrokenLoop:
            def stream(self, messages, options, *, max_steps):
                return BrokenStream()

        runtime = AgentRuntime(BrokenLoop())
        with self.assertRaises(ValueError) as caught:
            async with runtime.stream(self.messages) as run:
                async for event in run.events():
                    raise primary
        self.assertIs(caught.exception, primary)
        self.assertIs(caught.exception.__cause__, cleanup)
        self.assertEqual(runtime.result.status, "cancelled")
        self.assertIsNotNone(runtime.result.cleanup_error)

    async def test_early_exit_reports_cleanup_error_instead_of_silently_cancelling(self):
        cleanup = RuntimeError("private-cleanup")

        class BrokenStream:
            async def __anext__(self):
                return AgentStarted()

            async def aclose(self):
                raise cleanup

        class BrokenLoop:
            def stream(self, messages, options, *, max_steps):
                return BrokenStream()

        runtime = AgentRuntime(BrokenLoop())
        with self.assertRaises(asyncio.CancelledError) as caught:
            async with runtime.stream(self.messages) as run:
                async for event in run.events():
                    break
        self.assertIs(caught.exception.__cause__, cleanup)
        self.assertEqual(runtime.result.status, "cancelled")
        self.assertIsNotNone(runtime.result.cleanup_error)

if __name__ == "__main__":
    unittest.main()
