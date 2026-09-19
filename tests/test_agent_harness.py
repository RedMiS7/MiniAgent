import asyncio
import tempfile
import unittest
from unittest.mock import patch

from miniagent.agent import AgentHarness
from miniagent.bootstrap import create_harness
from miniagent.config import ModelConfig
from miniagent.models import GenerationOptions, LLMError, LLMResponse, Message, ResponseCompleted
from miniagent.tools import ToolContext, ToolExecutor, ToolRegistry


class FakeModel:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []
        self.closed = 0
        self.streams_closed = 0
        self.close_error = None

    async def stream(self, request):
        self.requests.append(request)
        try:
            item = next(self.responses)
            if isinstance(item, Exception):
                raise item
            yield ResponseCompleted(response=LLMResponse(
                message=Message(role="assistant", content=item), finish_reason="stop"))
        finally:
            self.streams_closed += 1

    async def aclose(self):
        self.closed += 1
        if self.close_error:
            raise self.close_error


class HarnessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.context = ToolContext(self.temp.name)
        self.executor = ToolExecutor(ToolRegistry(), self.context)
        self.messages = [Message(role="user", content="first")]

    async def consume(self, harness, messages=None, **kwargs):
        async with harness.run(messages or self.messages, **kwargs) as run:
            events = [event async for event in run.events()]
        return run, events

    async def test_independent_runs_forward_events_options_and_history(self):
        model = FakeModel("one", "two")
        harness = AgentHarness(model, self.executor)
        options = GenerationOptions(temperature=0.5)
        first, events = await self.consume(harness, options=options, max_steps=1)
        second, _ = await self.consume(harness, [Message(role="user", content="second")])
        self.assertIsNot(first, second)
        self.assertEqual(first.result.messages[-1].content, "one")
        self.assertEqual(second.result.messages[-1].content, "two")
        self.assertEqual([m.content for m in model.requests[1].messages], ["second"])
        self.assertEqual(model.requests[0].options, options)
        self.assertEqual(events[0].type, "agent_started")
        self.assertEqual(events[-1].type, "agent_completed")
        self.assertTrue(any(isinstance(e, ResponseCompleted) for e in events))
        self.assertEqual(model.closed, 0)

    async def test_reject_active_run_and_close_until_scope_exit(self):
        harness = AgentHarness(FakeModel("one", "two"), self.executor)
        async with harness.run(self.messages) as run:
            _ = [event async for event in run.events()]
            with self.assertRaisesRegex(RuntimeError, "active Run"):
                await self.consume(harness)
            with self.assertRaisesRegex(RuntimeError, "active Run"):
                await harness.aclose()
        next_run, _ = await self.consume(harness)
        self.assertEqual(next_run.result.status, "succeeded")

    async def test_failure_does_not_poison_next_run_or_retry(self):
        error = LLMError("bad", "test")
        model = FakeModel(error, "ok")
        harness = AgentHarness(model, self.executor)
        with self.assertRaises(LLMError) as caught:
            await self.consume(harness)
        self.assertIs(caught.exception, error)
        run, _ = await self.consume(harness)
        self.assertEqual(run.result.status, "succeeded")
        self.assertEqual(len(model.requests), 2)

    async def test_early_exit_cancels_and_allows_next_run(self):
        harness = AgentHarness(FakeModel("ok"), self.executor)
        async with harness.run(self.messages) as run:
            async for event in run.events():
                break
        self.assertEqual(run.result.status, "cancelled")
        next_run, _ = await self.consume(harness)
        self.assertEqual(next_run.result.status, "succeeded")

    async def test_cleanup_must_finish_before_reuse(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowModel(FakeModel):
            async def stream(self, request):
                try:
                    yield ResponseCompleted(response=LLMResponse(
                        message=Message(role="assistant", content="ok"), finish_reason="stop"))
                finally:
                    entered.set()
                    await release.wait()

        harness = AgentHarness(SlowModel(), self.executor)
        task = asyncio.create_task(self.consume(harness))
        await entered.wait()
        try:
            with self.assertRaisesRegex(RuntimeError, "active Run"):
                await self.consume(harness)
        finally:
            release.set()
            await task
        await self.consume(harness)

    async def test_borrowed_model_is_not_closed_and_closed_harness_rejects_runs(self):
        model = FakeModel("ok")
        async with AgentHarness(model, self.executor) as harness:
            await self.consume(harness)
        await harness.aclose()
        self.assertEqual(model.closed, 0)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await self.consume(harness)

    async def test_owned_model_closed_once(self):
        model = FakeModel("ok")
        async with AgentHarness(model, self.executor, owns_model=True) as harness:
            await self.consume(harness)
            self.assertEqual(model.closed, 0)
        await harness.aclose()
        self.assertEqual(model.closed, 1)

    async def test_model_close_failure_preserves_body_error(self):
        model = FakeModel()
        model.close_error = RuntimeError("close")
        original = ValueError("body")
        with self.assertRaises(ValueError) as caught:
            async with AgentHarness(model, self.executor, owns_model=True):
                raise original
        self.assertIs(caught.exception, original)
        self.assertIs(caught.exception.__cause__, model.close_error)

    async def test_invalid_run_can_be_followed_by_valid_run(self):
        harness = AgentHarness(FakeModel("ok"), self.executor)
        with self.assertRaises(ValueError):
            await self.consume(harness, max_steps=0)
        run, _ = await self.consume(harness)
        self.assertEqual(run.result.status, "succeeded")

    async def test_bootstrap_owns_created_model(self):
        config = ModelConfig(provider="deepseek", model="fake", api_key="test-only")
        model = FakeModel("ok")
        with patch("miniagent.bootstrap.create_model", return_value=model):
            async with create_harness(config, self.context) as harness:
                await self.consume(harness)
        self.assertEqual(model.closed, 1)
        self.assertTrue(model.requests[0].tools)
