import asyncio
import tempfile
import unittest

from miniagent.agent import AgentHarness
from miniagent.extensions import files
from miniagent.models import LLMError, LLMResponse, Message, ResponseCompleted, TextDelta, ToolCall
from miniagent.tools import ToolContext, ToolExecutor, ToolRegistry


class Model:
    def __init__(self, *items):
        self.items = iter(items)
        self.requests = []
        self.closed_streams = 0

    async def stream(self, request):
        self.requests.append(request)
        try:
            item = next(self.items)
            if isinstance(item, Exception):
                raise item
            message = Message(role="assistant", tool_calls=(item,)) if isinstance(item, ToolCall) else Message(role="assistant", content=item)
            yield TextDelta(text=message.content)
            yield ResponseCompleted(response=LLMResponse(
                message=message, finish_reason="tool_calls" if message.tool_calls else "stop"))
        finally:
            self.closed_streams += 1


class HarnessEventTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = ToolRegistry()
        files.register(self.registry)
        self.executor = ToolExecutor(self.registry, ToolContext(self.temp.name))
        self.messages = [Message(role="user", content="Task")]

    async def consume(self, harness):
        async with harness.run(self.messages) as run:
            events = [event async for event in run.events()]
        return run, events

    async def test_callback_matches_full_stream_and_separates_runs(self):
        seen = []
        harness = AgentHarness(Model("first", "second"), self.executor,
                               on_event=lambda run, event: seen.append((run, event)))
        first, events1 = await self.consume(harness)
        second, events2 = await self.consume(harness)
        self.assertIsNot(first, second)
        self.assertEqual([e for r, e in seen if r is first], events1)
        self.assertEqual([e for r, e in seen if r is second], events2)
        self.assertEqual(sum(e.type == "agent_completed" for _, e in seen), 2)
        self.assertIsNot(seen[0][1], events1[0])

    async def test_callback_failure_does_not_replay_write(self):
        call = ToolCall(id="w", name="write_file", arguments='{"path":"out.txt","content":"one"}')
        model = Model(call, "Done")
        seen = []
        def broken(run, event):
            seen.append(event.type)
            raise ValueError("display failed")
        run, events = await self.consume(AgentHarness(model, self.executor, on_event=broken))
        self.assertEqual(run.result.status, "succeeded")
        self.assertEqual((self.executor.context.workspace / "out.txt").read_text(), "one")
        self.assertEqual(seen, [e.type for e in events])
        self.assertEqual(seen.count("tool_completed"), 1)
        self.assertEqual(len(model.requests), 2)

    async def test_terminal_callback_observes_result_after_cleanup(self):
        model = Model("Done")
        observed = []
        def observe(run, event):
            if event.type == "agent_completed":
                observed.append((run.result.status, model.closed_streams))
        await self.consume(AgentHarness(model, self.executor, on_event=observe))
        self.assertEqual(observed, [("succeeded", 1)])

    async def test_early_exit_notifies_cancel_terminal_once(self):
        model = Model("unused")
        seen = []
        harness = AgentHarness(model, self.executor, on_event=lambda run, event: seen.append(event.type))
        async with harness.run(self.messages) as run:
            async for event in run.events():
                break
        self.assertEqual(run.result.status, "cancelled")
        self.assertEqual(seen, ["agent_started", "agent_cancelled"])
        self.assertEqual(model.requests, [])

    async def test_no_consumer_still_notifies_final_cancellation(self):
        seen = []
        harness = AgentHarness(Model(), self.executor, on_event=lambda run, event: seen.append(event.type))
        async with harness.run(self.messages):
            pass
        self.assertEqual(seen, ["agent_cancelled"])

    async def test_model_error_preserved_despite_callback_errors(self):
        error = LLMError("test", "private")
        seen = []
        def broken(run, event):
            seen.append(event.type)
            raise RuntimeError("display")
        harness = AgentHarness(Model(error), self.executor, on_event=broken)
        with self.assertRaises(LLMError) as caught:
            await self.consume(harness)
        self.assertIs(caught.exception, error)
        self.assertEqual(seen.count("agent_failed"), 1)

    async def test_callback_cancelled_error_is_not_run_cancellation(self):
        def broken(run, event):
            raise asyncio.CancelledError()
        run, _ = await self.consume(AgentHarness(Model("Done"), self.executor, on_event=broken))
        self.assertEqual(run.result.status, "succeeded")

    async def test_callback_can_explicitly_cancel_run(self):
        seen = []
        def observe(run, event):
            seen.append(event.type)
            if event.type == "agent_started":
                run.cancel()
        harness = AgentHarness(Model("unused"), self.executor, on_event=observe)
        with self.assertRaises(asyncio.CancelledError):
            await self.consume(harness)
        self.assertEqual(seen.count("agent_cancelled"), 1)

    async def test_nested_payload_copy_prevents_mutating_model_history(self):
        def mutate(run, event):
            if event.type == "llm_response_completed":
                event.response.message.__dict__["content"] = "corrupt"
            if event.type == "agent_completed":
                event.messages[-1].__dict__["content"] = "corrupt"
        run, events = await self.consume(AgentHarness(Model("Original"), self.executor, on_event=mutate))
        self.assertEqual(run.result.messages[-1].content, "Original")
        self.assertEqual(next(e for e in events if e.type == "llm_response_completed").response.text, "Original")

    async def test_async_callback_is_rejected(self):
        async def observe(run, event):
            pass
        with self.assertRaises(TypeError):
            AgentHarness(Model(), self.executor, on_event=observe)
