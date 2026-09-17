import contextlib
import io
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from openai import AsyncOpenAI

import cli
from miniagent.bootstrap import create_model
from miniagent.config import ModelConfig
from miniagent.models import (
    ContinuationState, GenerationOptions, LLMError, LLMRequest, LLMResponse,
    Message, ResponseCompleted, TextDelta, ToolArgumentsDelta, ToolCallStarted,
)
from miniagent.models.bailian_adapter import BailianAdapter
from test_models import TOOL, chunk, completion, sse

MODELS = ("qwen3.7-plus", "glm-5")


class BailianTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, model, handler):
        client = AsyncOpenAI(
            api_key="fake-bailian", base_url="https://example.invalid/compatible-mode/v1",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        adapter = BailianAdapter(client, model)
        self.addAsyncCleanup(adapter.aclose)
        return adapter

    async def test_common_options_and_model_names(self):
        for model in MODELS:
            def handler(request):
                body = json.loads(request.content)
                self.assertEqual(request.url.path, "/compatible-mode/v1/chat/completions")
                self.assertEqual(body["model"], model)
                self.assertEqual(body["max_completion_tokens"], 256)
                self.assertNotIn("max_tokens", body)
                self.assertEqual(body["temperature"], 0.5)
                self.assertFalse(body["enable_thinking"])
                self.assertNotIn("thinking", body)
                self.assertEqual(body["tools"][0]["function"]["name"], "weather")
                return httpx.Response(200, json=completion())
            with self.subTest(model=model):
                result = await self.adapter(model, handler).generate(LLMRequest(
                    messages=[Message(role="user", content="hi")], tools=[TOOL], options=GenerationOptions(max_output_tokens=256, reasoning="none", temperature=0.5),
                ))
                self.assertEqual(result.text, "hello")

    async def test_glm_effort_mapping(self):
        for effort, wire in [("low", "high"), ("medium", "high"), ("high", "high"),
                            ("xhigh", "max"), ("max", "max")]:
            def handler(request):
                body = json.loads(request.content)
                self.assertEqual(body["reasoning_effort"], wire)
                self.assertTrue(body["enable_thinking"])
                return httpx.Response(200, json=completion())
            await self.adapter("glm-5", handler).generate(LLMRequest(
                messages=[Message(role="user", content="hi")], options=GenerationOptions(reasoning=effort),
            ))

    async def test_qwen_effort_and_unknown_model_rejected_before_http(self):
        def handler(request):
            self.fail("Unsupported option sent")
        for model, effort in [("qwen3.7-plus", "high"), ("glm-5.2", None)]:
            with self.assertRaises(LLMError) as caught:
                await self.adapter(model, handler).generate(LLMRequest(
                    messages=[Message(role="user", content="hi")], options=GenerationOptions(reasoning=effort),
                ))
            self.assertEqual(caught.exception.code, "unsupported_feature")

    async def test_tool_history_roundtrip(self):
        for model in MODELS:
            calls = []
            def handler(request):
                body = json.loads(request.content)
                calls.append(body)
                if len(calls) == 1:
                    self.assertNotIn("enable_thinking", body)
                    return httpx.Response(200, json=completion(
                        None, "tool_calls", reasoning_content="opaque-history",
                        tool_calls=[{"id": "call", "type": "function",
                                     "function": {"name": "weather", "arguments": "{}"}}],
                    ))
                self.assertEqual(body["messages"][1]["reasoning_content"], "opaque-history")
                self.assertEqual(body["messages"][2]["tool_call_id"], "call")
                if model == "qwen3.7-plus":
                    self.assertTrue(body["preserve_thinking"])
                    self.assertNotIn("clear_thinking", body)
                else:
                    self.assertFalse(body["clear_thinking"])
                    self.assertNotIn("preserve_thinking", body)
                return httpx.Response(200, json=completion("sunny"))
            with self.subTest(model=model):
                llm = self.adapter(model, handler)
                user = Message(role="user", content="weather?")
                first = await llm.generate(LLMRequest(messages=[user], tools=[TOOL]))
                self.assertEqual(first.message.continuation.provider, "bailian")
                self.assertNotIn("opaque-history", repr(first))
                second = await llm.generate(LLMRequest(messages=[
                    user, first.message, Message(role="tool", content="sunny", tool_call_id="call"),
                ], tools=[TOOL]))
                self.assertEqual(second.text, "sunny")

    async def test_foreign_model_continuation_rejected(self):
        for model in MODELS:
            def handler(request):
                self.fail("Foreign state sent")
            state = ContinuationState(provider="bailian", model="other-model", payload='""')
            with self.assertRaises(LLMError) as caught:
                await self.adapter(model, handler).generate(LLMRequest(messages=[
                    Message(role="assistant", content="hi", continuation=state), Message(role="user", content="hi"),
                ]))
            self.assertEqual(caught.exception.code, "invalid_request")

    async def test_stream_text_and_tools(self):
        for model in MODELS:
            events = [
                chunk({"content": "checking", "reasoning_content": "opaque"}),
                chunk({"tool_calls": [{
                    "index": 0, "id": "call", "type": "function",
                    "function": {"name": "weather", "arguments": "{"},
                }]}),
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]}),
                chunk({}, "tool_calls"),
                {"choices": [], "usage": completion()["usage"]},
            ]
            http_response = sse(events)
            llm = self.adapter(model, lambda r: http_response)
            result = [e async for e in llm.stream(LLMRequest(messages=[Message(role="user", content="hi")], tools=[TOOL]))]
            self.assertEqual(result[0], TextDelta(text="checking"))
            self.assertIsInstance(result[1], ToolCallStarted)
            self.assertEqual("".join(e.delta for e in result if isinstance(e, ToolArgumentsDelta)), "{}")
            self.assertIsInstance(result[-1], ResponseCompleted)
            self.assertEqual(result[-1].response.message.continuation.provider, "bailian")
            self.assertEqual(result[-1].response.usage.input_tokens, 3)
            self.assertTrue(http_response.is_closed)

    async def test_error_mapping_and_truncated_stream(self):
        for model in MODELS:
            llm = self.adapter(model, lambda r: httpx.Response(
                401, json={"error": {"message": "secret"}},
            ))
            with self.assertRaises(LLMError) as caught:
                await llm.generate(LLMRequest(messages=[Message(role="user", content="hi")]))
            self.assertEqual(caught.exception.code, "authentication")
            self.assertNotIn("secret", str(caught.exception))
            llm = self.adapter(model, lambda r: sse([chunk({"content": "partial"})]))
            with self.assertRaises(LLMError) as caught:
                _ = [e async for e in llm.stream(LLMRequest(messages=[Message(role="user", content="hi")]))]
            self.assertEqual(caught.exception.code, "invalid_response")


class BailianEntryTests(unittest.TestCase):
    def test_config_factory_and_key_isolation(self):
        with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "fake"}, clear=True):
            config = ModelConfig.from_env("bailian", "glm-5")
            self.assertEqual(config.base_url, "https://dashscope.aliyuncs.com/compatible-mode/v1")
            with patch("miniagent.bootstrap.AsyncOpenAI") as client:
                self.assertIsInstance(create_model(config), BailianAdapter)
                self.assertEqual(client.call_args.kwargs["base_url"], config.base_url)
            with self.assertRaises(ValueError):
                ModelConfig.from_env("deepseek", "deepseek-flash")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}, clear=True):
            with self.assertRaisesRegex(ValueError, "DASHSCOPE_API_KEY"):
                ModelConfig.from_env("bailian", "glm-5")

    def test_cli_uses_unchanged_unified_request(self):
        for name in MODELS:
            llm = AsyncMock()
            llm.generate.return_value = LLMResponse(message=Message(role="assistant", content="hello"), finish_reason="stop")
            with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "fake"}, clear=True), \
                 patch("cli.create_model", return_value=llm), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(["--provider", "bailian", "--model", name]), 0)
            llm.generate.assert_awaited_once_with(LLMRequest(messages=[Message(role="user", content="你好")]))
            llm.aclose.assert_awaited_once()
            self.assertEqual(output.getvalue(), "hello\n")
