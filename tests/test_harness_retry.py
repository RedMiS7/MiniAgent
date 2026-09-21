import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import harness_cli
from examples.retry_faults import FaultInjectingModel, FaultPlan, OfflineModel
from miniagent.agent import AgentHarness, RetryPolicy
from miniagent.agent.retry import RetryingModel
from miniagent.bootstrap import create_harness
from miniagent.config import ModelConfig
from miniagent.models import LLMError, LLMRequest, LLMResponse, Message, ResponseCompleted, TextDelta
from miniagent.tools import ToolContext, ToolExecutor, ToolRegistry


class Model:
    def __init__(self, *items):
        self.items = iter(items)
        self.calls = []
        self.closed = 0
        self.streams_closed = 0

    async def stream(self, request):
        self.calls.append(request)
        try:
            item = next(self.items)
            if isinstance(item, BaseException):
                raise item
            if item == "partial":
                yield TextDelta(text="partial")
                raise LLMError("timeout", "timeout", True)
            yield ResponseCompleted(response=LLMResponse(message=Message(role="assistant", content="ok"), finish_reason="stop"))
        finally:
            self.streams_closed += 1

    async def aclose(self):
        self.closed += 1


class RetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.request = LLMRequest(messages=[Message(role="user", content="hi")])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.executor = ToolExecutor(ToolRegistry(), ToolContext(self.temp.name))

    async def test_exponential_full_jitter_capped_and_same_request(self):
        model = Model(*[LLMError("timeout", "test", True) for _ in range(4)], "ok")
        reports = []
        wrapper = RetryingModel(model, RetryPolicy(4, 1, 3), lambda *args: reports.append(args))
        with patch("miniagent.agent.retry.random.uniform", side_effect=lambda low, high: high / 2) as jitter, \
                patch("miniagent.agent.retry.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await wrapper.generate(self.request)
        self.assertEqual(result.text, "ok")
        self.assertEqual([c.args for c in jitter.call_args_list], [(0,1), (0,2), (0,3), (0,3)])
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [0.5, 1, 1.5, 1.5])
        self.assertEqual([r[0] for r in reports], [1,2,3,4])
        self.assertEqual(model.calls, [self.request] * 5)
        self.assertEqual(model.streams_closed, 5)
        self.assertEqual(model.closed, 0)

    async def test_exhaustion_preserves_last_failure(self):
        errors = [LLMError("server", "test", True) for _ in range(3)]
        model = Model(*errors)
        with patch("miniagent.agent.retry.asyncio.sleep", new_callable=AsyncMock), self.assertRaises(LLMError) as caught:
            await RetryingModel(model, RetryPolicy()).generate(self.request)
        self.assertIs(caught.exception, errors[-1])
        self.assertEqual(len(model.calls), 3)

    async def test_permanent_errors_and_unexpected_errors_not_retried(self):
        for error in (LLMError("authentication", "test"), RuntimeError("bug"), asyncio.CancelledError()):
            model = Model(error)
            with self.assertRaises(type(error)):
                await RetryingModel(model, RetryPolicy()).generate(self.request)
            self.assertEqual(len(model.calls), 1)

    async def test_partial_output_blocks_retry(self):
        model = Model("partial", "ok")
        seen = []
        with self.assertRaises(LLMError):
            async for event in RetryingModel(model, RetryPolicy()).stream(self.request):
                seen.append(event)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(model.calls), 1)

    async def test_cleanup_failure_prevents_retry_and_keeps_primary(self):
        primary = LLMError("timeout", "test", True)
        cleanup = RuntimeError("cleanup")
        class Stream:
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise primary
            async def aclose(self):
                raise cleanup
        class Broken:
            def stream(self, request):
                return Stream()
        with self.assertRaises(LLMError) as caught:
            await RetryingModel(Broken(), RetryPolicy()).generate(self.request)
        self.assertIs(caught.exception, primary)
        self.assertIs(caught.exception.__cause__, cleanup)

    async def test_cancel_during_backoff_ends_run_without_new_attempt(self):
        entered = asyncio.Event()
        async def wait(delay):
            entered.set()
            await asyncio.Event().wait()
        model = Model(LLMError("timeout", "test", True), "ok")
        harness = AgentHarness(model, self.executor, retry_policy=RetryPolicy())
        async with harness.run(self.request.messages) as run:
            async def consume():
                return [e async for e in run.events()]
            with patch("miniagent.agent.retry.asyncio.sleep", side_effect=wait):
                task = asyncio.create_task(consume())
                await entered.wait()
                run.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(run.result.status, "cancelled")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.streams_closed, 1)

    async def test_policy_default_disabled_and_owned_model_closed_once(self):
        model = Model(LLMError("timeout", "test", True))
        with self.assertRaises(LLMError):
            async with AgentHarness(model, self.executor, owns_model=True) as harness:
                async with harness.run(self.request.messages) as run:
                    _ = [e async for e in run.events()]
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.closed, 1)

    async def test_factory_disables_sdk_retry_without_mutating_config(self):
        config = ModelConfig(provider="deepseek", model="test", api_key="fake", max_retries=5)
        model = Model("ok")
        with patch("miniagent.bootstrap.create_model", return_value=model) as factory:
            async with create_harness(config, self.executor.context, retry_policy=RetryPolicy()):
                pass
        self.assertEqual(factory.call_args.args[0].max_retries, 0)
        self.assertEqual(config.max_retries, 5)
        self.assertEqual(model.closed, 1)

    async def test_model_retry_after_tool_does_not_replay_tool(self):
        from examples.echo_extension import register
        register(self.executor.registry)
        model = FaultInjectingModel(OfflineModel(), FaultPlan("timeout", 2, 2), lambda _: None)
        seen = []
        harness = AgentHarness(model, self.executor, retry_policy=RetryPolicy(), on_event=lambda r,e: seen.append(e))
        with patch("miniagent.agent.retry.asyncio.sleep", new_callable=AsyncMock):
            async with harness.run(self.request.messages, max_steps=2) as run:
                _ = [e async for e in run.events()]
        self.assertEqual(run.result.status, "succeeded")
        self.assertEqual(sum(e.type == "tool_completed" for e in seen), 1)

    async def test_retry_observer_failure_does_not_change_result(self):
        def broken(*args):
            raise ValueError("display")
        with patch("miniagent.agent.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await RetryingModel(Model(LLMError("server", "test", True), "ok"), RetryPolicy(), broken).generate(self.request)
        self.assertEqual(result.text, "ok")

    async def test_retry_counter_resets_for_each_request(self):
        model = Model(LLMError("server", "test", True), "ok", LLMError("server", "test", True), "ok")
        reports = []
        wrapper = RetryingModel(model, RetryPolicy(1), lambda n, d, c: reports.append(n))
        with patch("miniagent.agent.retry.asyncio.sleep", new_callable=AsyncMock):
            await wrapper.generate(self.request)
            await wrapper.generate(self.request)
        self.assertEqual(reports, [1, 1])

    async def test_real_adapters_retry_429_without_sdk_retry_layer(self):
        import json
        import httpx
        from openai import AsyncOpenAI
        from test_models import response, chunk, sse
        for provider in ("openai", "deepseek"):
            bodies = []
            def handler(request):
                bodies.append(json.loads(request.content))
                if len(bodies) == 1:
                    return httpx.Response(429, json={"error":{"message":"limited", "type":"rate_limit"}})
                return sse([{"type":"response.completed", "response":response("ok")}] if provider == "openai"
                           else [chunk({"content":"ok"}, "stop")])
            clients = []
            def client_factory(**kwargs):
                self.assertEqual(kwargs["max_retries"], 0)
                kwargs["base_url"] = "https://example.invalid/v1"
                client = AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
                clients.append(client)
                return client
            reports = []
            with self.subTest(provider=provider), patch("miniagent.bootstrap.AsyncOpenAI", side_effect=client_factory), \
                    patch("miniagent.agent.retry.asyncio.sleep", new_callable=AsyncMock):
                config = ModelConfig(provider=provider, model="test", api_key="test-only", max_retries=8)
                async with create_harness(config, self.executor.context, retry_policy=RetryPolicy(),
                                          on_retry=lambda *args: reports.append(args)) as harness:
                    async with harness.run(self.request.messages) as run:
                        _ = [e async for e in run.events()]
                self.assertEqual(run.result.status, "succeeded")
                self.assertEqual(len(bodies), 2)
                self.assertEqual(bodies[0], bodies[1])
                self.assertEqual(len(reports), 1)
                self.assertTrue(clients[0].is_closed())

    async def test_fault_injector_preserves_cleanup_error_chain(self):
        primary = LLMError("timeout", "test", True)
        cleanup = RuntimeError("cleanup")
        class Stream:
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise primary
            async def aclose(self):
                raise cleanup
        class Broken:
            def stream(self, request):
                return Stream()
        injector = FaultInjectingModel(Broken(), FaultPlan("timeout", step=2), lambda _: None)
        with self.assertRaises(LLMError) as caught:
            _ = [e async for e in injector.stream(self.request)]
        self.assertIs(caught.exception, primary)
        self.assertIs(caught.exception.__cause__, cleanup)

    async def test_chained_cleanup_failure_does_not_enter_retry(self):
        error = LLMError("timeout", "test", True)
        error.__cause__ = RuntimeError("cleanup")
        model = Model(error, "ok")
        with patch("miniagent.agent.retry.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(LLMError):
                await RetryingModel(model, RetryPolicy()).generate(self.request)
            sleep.assert_not_awaited()
        self.assertEqual(len(model.calls), 1)

    def test_invalid_policy(self):
        for kwargs in ({"max_retries":-1}, {"max_retries":True}, {"base_delay":0}, {"max_delay":float("nan")}, {"base_delay":2,"max_delay":1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RetryPolicy(**kwargs)


class RetryCliTests(unittest.TestCase):
    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = harness_cli.main(["--offline", "--prompt", "test", "--retry-base", "0.001", "--retry-cap", "0.01", *args])
        return code, out.getvalue() + err.getvalue()

    def test_success_after_tool_does_not_replay_tool(self):
        code, text = self.invoke("--fault-error", "timeout", "--fault-count", "2", "--fault-step", "2")
        self.assertEqual(code, 0)
        self.assertEqual(text.count("[工具成功] echo"), 1)
        self.assertEqual(text.count("[模型重试]"), 2)
        self.assertIn("succeeded", text)

    def test_exhaustion_and_zero_retry(self):
        for options in (("--fault-count", "3"), ("--retries", "0")):
            code, text = self.invoke("--fault-error", "rate_limit", *options)
            self.assertEqual(code, 1)
            self.assertIn("[错误码] rate_limit", text)
            self.assertNotIn("[工具成功]", text)

    def test_partial_and_permanent_error_never_retry(self):
        for options in (("--fault-error", "timeout", "--fault-after-partial"), ("--fault-error", "authentication")):
            code, text = self.invoke(*options)
            self.assertEqual(code, 1)
            self.assertNotIn("[模型重试]", text)

    def test_offline_mode_does_not_open_model_or_mcp(self):
        with patch.object(harness_cli, "create_model") as model, patch.object(harness_cli, "connect_brave_search") as mcp:
            self.assertEqual(self.invoke()[0], 0)
        model.assert_not_called()
        mcp.assert_not_called()

    def test_invalid_fault_options_fail_before_connection(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            harness_cli.main(["--offline", "--prompt", "test", "--fault-step", "2"])
        self.assertEqual(caught.exception.code, 2)

    def test_tool_failure_is_returned_to_model_not_automatically_retried(self):
        code, text = self.invoke("--fault-tool-count", "1")
        self.assertEqual(code, 0)
        self.assertEqual(text.count("[工具开始] echo"), 2)
        self.assertEqual(text.count("[工具成功] echo"), 1)
        self.assertNotIn("[模型重试]", text)
        self.assertIn("[第 3 轮]", text)

    def test_tool_failures_remain_bounded_by_steps(self):
        code, text = self.invoke("--fault-tool-count", "10", "--max-steps", "2")
        self.assertEqual(code, 1)
        self.assertIn("step_limit", text)
        self.assertEqual(text.count("[工具开始] echo"), 1)
