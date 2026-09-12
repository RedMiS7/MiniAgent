import asyncio
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
    Message, ResponseCompleted, TextDelta, TokenUsage, ToolArgumentsDelta,
    ToolCall, ToolCallStarted, ToolDefinition,
)
from miniagent.models.deepseek_adapter import DeepSeekAdapter
from miniagent.models.openai_adapter import OpenAIAdapter

TOOL = ToolDefinition("weather", "Weather lookup", {"type": "object", "properties": {}})


def completion(content="hello", finish="stop", **message_extra):
    return {
        "id": "test", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{
            "index": 0, "finish_reason": finish,
            "message": {"role": "assistant", "content": content, **message_extra},
        }],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def response(text="hello", output=None, status="completed", reason=None):
    return {
        "id": "resp_test", "object": "response", "created_at": 0, "model": "test",
        "status": status, "error": None,
        "incomplete_details": {"reason": reason} if reason else None,
        "output": output if output is not None else [{
            "type": "message", "id": "msg_test", "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }],
        "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5,
                  "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens_details": {"reasoning_tokens": 0}},
    }


def chunk(delta, finish=None, usage=None):
    return {
        "id": "test", "object": "chat.completion.chunk", "created": 0, "model": "test",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        "usage": usage,
    }


def sse(events):
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content="".join("data: " + json.dumps(e) + "\n\n" for e in events)
                          + "data: [DONE]\n\n")


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, provider, handler):
        client = AsyncOpenAI(
            api_key="fake-test-key", base_url="https://model.invalid/v1",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        cls = OpenAIAdapter if provider == "openai" else DeepSeekAdapter
        model = cls(client, "gpt-6-astra" if provider == "openai" else "deepseek-flash")
        self.addAsyncCleanup(model.aclose)
        return model

    async def test_same_request_has_provider_specific_wire_format(self):
        request = LLMRequest([Message("user", "hello")], [TOOL],
                             GenerationOptions(max_output_tokens=200, reasoning="high"))
        for provider in ("openai", "deepseek"):
            def handler(http_request):
                body = json.loads(http_request.content)
                self.assertEqual(http_request.headers["authorization"], "Bearer fake-test-key")
                if provider == "openai":
                    self.assertEqual(http_request.url.path, "/v1/responses")
                    self.assertEqual(body["input"], [{"role": "user", "content": "hello"}])
                    self.assertEqual(body["reasoning"], {"effort": "high"})
                    self.assertEqual(body["max_output_tokens"], 200)
                    self.assertEqual(body["tools"][0]["name"], "weather")
                    self.assertFalse(body["store"])
                    self.assertIn("reasoning.encrypted_content", body["include"])
                    return httpx.Response(200, json=response())
                self.assertEqual(http_request.url.path, "/v1/chat/completions")
                self.assertEqual(body["messages"], [{"role": "user", "content": "hello"}])
                self.assertEqual(body["thinking"], {"type": "enabled"})
                self.assertEqual(body["reasoning_effort"], "high")
                self.assertEqual(body["max_tokens"], 200)
                self.assertEqual(body["tools"][0]["function"]["name"], "weather")
                return httpx.Response(200, json=completion())
            with self.subTest(provider=provider):
                result = await self.adapter(provider, handler).generate(request)
                self.assertEqual(result, LLMResponse(Message("assistant", "hello"), "stop", TokenUsage(3, 2)))

    async def test_reasoning_mapping_and_disable(self):
        for effort, actual in [("medium", "high"), ("xhigh", "high"), ("none", None)]:
            def handler(request):
                body = json.loads(request.content)
                self.assertEqual(body.get("reasoning_effort"), actual)
                self.assertEqual(body["thinking"]["type"], "disabled" if actual is None else "enabled")
                return httpx.Response(200, json=completion())
            await self.adapter("deepseek", handler).generate(LLMRequest(
                [Message("user", "hi")], options=GenerationOptions(reasoning=effort),
            ))

    async def test_unsupported_options_do_not_send_http(self):
        def handler(request):
            self.fail("Unsupported parameters reached the service")
        for provider, options in [
            ("openai", GenerationOptions(reasoning="none")),
            ("openai", GenerationOptions(temperature=0.5)),
            ("deepseek", GenerationOptions(temperature=0.5)),
        ]:
            with self.subTest(provider=provider, options=options):
                with self.assertRaises(LLMError) as caught:
                    await self.adapter(provider, handler).generate(LLMRequest([Message("user", "hi")], options=options))
                self.assertEqual(caught.exception.code, "unsupported_feature")

    async def test_tool_round_trip_preserves_continuation(self):
        for provider in ("openai", "deepseek"):
            received = []
            reasoning_item = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}
            function_item = {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                             "name": "weather", "arguments": "{}", "status": "completed"}
            def handler(request):
                body = json.loads(request.content)
                received.append(body)
                if len(received) == 1:
                    if provider == "openai":
                        return httpx.Response(200, json=response(output=[reasoning_item, function_item]))
                    return httpx.Response(200, json=completion(
                        None, "tool_calls", reasoning_content="private-thinking",
                        tool_calls=[{"id": "call_1", "type": "function",
                                     "function": {"name": "weather", "arguments": "{}"}}],
                    ))
                if provider == "openai":
                    self.assertEqual(body["input"][1], reasoning_item)
                    self.assertEqual(body["input"][-1], {
                        "type": "function_call_output", "call_id": "call_1", "output": "sunny",
                    })
                    return httpx.Response(200, json=response("sunny"))
                self.assertEqual(body["messages"][1]["reasoning_content"], "private-thinking")
                self.assertEqual(body["messages"][-1], {
                    "role": "tool", "tool_call_id": "call_1", "content": "sunny",
                })
                return httpx.Response(200, json=completion("sunny"))
            with self.subTest(provider=provider):
                model = self.adapter(provider, handler)
                initial = Message("user", "weather?")
                first = await model.generate(LLMRequest([initial], [TOOL]))
                self.assertEqual(first.finish_reason, "tool_calls")
                self.assertEqual(first.message.tool_calls, (ToolCall("call_1", "weather", "{}"),))
                self.assertNotIn("private-thinking", repr(first))
                result = await model.generate(LLMRequest([
                    initial, first.message, Message("tool", "sunny", tool_call_id="call_1"),
                ], [TOOL]))
                self.assertEqual(result.text, "sunny")

    async def test_foreign_continuation_rejected(self):
        def handler(request):
            self.fail("Foreign continuation sent")
        message = Message("assistant", "hi", continuation=ContinuationState("other", "test", "{}"))
        for provider in ("openai", "deepseek"):
            with self.assertRaises(LLMError) as caught:
                await self.adapter(provider, handler).generate(LLMRequest([message, Message("user", "hi")]))
            self.assertEqual(caught.exception.code, "invalid_request")

    async def test_finish_reasons_are_results(self):
        for provider, body, reason in [
            ("openai", response("partial", status="incomplete", reason="max_output_tokens"), "length"),
            ("deepseek", completion("partial", "length"), "length"),
            ("deepseek", completion("", "content_filter"), "content_filter"),
            ("deepseek", completion("", refusal="No"), "refusal"),
            ("openai", response(output=[{
                "type": "message", "id": "m", "role": "assistant", "status": "completed",
                "content": [{"type": "refusal", "refusal": "No"}],
            }]), "refusal"),
        ]:
            with self.subTest(provider=provider, reason=reason):
                model = self.adapter(provider, lambda r: httpx.Response(200, json=body))
                self.assertEqual((await model.generate(LLMRequest([Message("user", "hi")]))).finish_reason, reason)

    async def test_bad_response_and_missing_usage(self):
        for provider in ("openai", "deepseek"):
            with self.subTest(provider=provider):
                for body in ({}, response("") if provider == "openai" else completion(None)):
                    model = self.adapter(provider, lambda r: httpx.Response(200, json=body))
                    with self.assertRaises(LLMError) as caught:
                        await model.generate(LLMRequest([Message("user", "hi")]))
                    self.assertEqual(caught.exception.code, "invalid_response")
                body = response() if provider == "openai" else completion()
                body.pop("usage")
                model = self.adapter(provider, lambda r: httpx.Response(200, json=body))
                self.assertIsNone((await model.generate(LLMRequest([Message("user", "hi")]))).usage)

    async def test_http_errors_are_sanitized_and_not_retried(self):
        for provider in ("openai", "deepseek"):
            for status, code in [(400, "invalid_request"), (401, "authentication"),
                                 (403, "permission"), (429, "rate_limit"), (500, "server")]:
                calls = []
                def handler(request):
                    calls.append(request)
                    return httpx.Response(status, json={"error": {"message": "secret-input"}})
                with self.subTest(provider=provider, status=status):
                    with self.assertRaises(LLMError) as caught:
                        await self.adapter(provider, handler).generate(LLMRequest([Message("user", "hi")]))
                    self.assertEqual(caught.exception.code, code)
                    self.assertEqual(caught.exception.retryable, status in (429, 500))
                    self.assertNotIn("secret-input", str(caught.exception))
                    self.assertEqual(len(calls), 1)

    async def test_transport_errors(self):
        for provider in ("openai", "deepseek"):
            for error_type, code in [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "connection")]:
                def handler(request):
                    raise error_type("secret-input", request=request)
                with self.subTest(provider=provider, code=code):
                    with self.assertRaises(LLMError) as caught:
                        await self.adapter(provider, handler).generate(LLMRequest([Message("user", "hi")]))
                    self.assertEqual(caught.exception.code, code)

    async def test_text_stream_final_response_and_usage(self):
        for provider in ("openai", "deepseek"):
            if provider == "openai":
                events = [
                    {"type": "response.output_text.delta", "delta": "hello"},
                    {"type": "response.completed", "response": response()},
                ]
            else:
                events = [
                    chunk({"content": "hel", "reasoning_content": "hidden"}),
                    chunk({"content": "lo"}, "stop"),
                    {"choices": [], "usage": completion()["usage"]},
                ]
            with self.subTest(provider=provider):
                http_response = sse(events)
                model = self.adapter(provider, lambda r: http_response)
                result = [e async for e in model.stream(LLMRequest([Message("user", "hi")]))]
                self.assertEqual("".join(e.text for e in result if isinstance(e, TextDelta)), "hello")
                self.assertIsInstance(result[-1], ResponseCompleted)
                self.assertEqual(result[-1].response.usage, TokenUsage(3, 2))
                self.assertTrue(http_response.is_closed)

    async def test_interleaved_tool_streams(self):
        for provider in ("openai", "deepseek"):
            if provider == "openai":
                items = [{"type": "function_call", "id": f"fc_{i}", "call_id": f"call_{i}",
                          "name": "weather", "arguments": "{}", "status": "completed"} for i in range(2)]
                events = [
                    {"type": "response.output_item.added", "output_index": i, "item": {**items[i], "arguments": ""}}
                    for i in range(2)
                ]
                events += [{"type": "response.function_call_arguments.delta", "output_index": i, "delta": part}
                           for part in ("{", "}") for i in (1, 0)]
                events.append({"type": "response.completed", "response": response(output=items)})
            else:
                events = [chunk({"tool_calls": [{
                    "index": i, "id": f"call_{i}", "type": "function",
                    "function": {"name": "weather", "arguments": ""},
                } for i in range(2)]})]
                events += [chunk({"tool_calls": [{"index": i, "function": {"arguments": part}}]})
                           for part in ("{", "}") for i in (1, 0)]
                events.append(chunk({}, "tool_calls"))
            with self.subTest(provider=provider):
                model = self.adapter(provider, lambda r: sse(events))
                results = [e async for e in model.stream(LLMRequest([Message("user", "hi")], [TOOL]))]
                self.assertEqual(len([e for e in results if isinstance(e, ToolCallStarted)]), 2)
                for i in range(2):
                    self.assertEqual("".join(e.delta for e in results
                                            if isinstance(e, ToolArgumentsDelta) and e.index == i), "{}")
                self.assertEqual(len(results[-1].response.message.tool_calls), 2)

    async def test_stream_truncation_is_error_and_closes(self):
        for provider, events in [
            ("openai", [{"type": "response.output_text.delta", "delta": "partial"}]),
            ("deepseek", [chunk({"content": "partial"})]),
        ]:
            http_response = sse(events)
            model = self.adapter(provider, lambda r: http_response)
            with self.assertRaises(LLMError) as caught:
                _ = [e async for e in model.stream(LLMRequest([Message("user", "hi")]))]
            self.assertEqual(caught.exception.code, "invalid_response")
            self.assertTrue(http_response.is_closed)

    async def test_early_stream_close(self):
        for provider, events in [
            ("openai", [{"type": "response.output_text.delta", "delta": "hi"}]),
            ("deepseek", [chunk({"content": "hi"})]),
        ]:
            http_response = sse(events)
            model = self.adapter(provider, lambda r: http_response)
            stream = model.stream(LLMRequest([Message("user", "hi")]))
            await anext(stream)
            await stream.aclose()
            self.assertTrue(http_response.is_closed)
            await model.aclose()
            self.assertTrue(model._client.is_closed())



    async def test_stream_transport_failure_after_text(self):
        for provider in ("openai", "deepseek"):
            event = ({"type": "response.output_text.delta", "delta": "partial"}
                     if provider == "openai" else chunk({"content": "partial"}))

            class BrokenStream(httpx.AsyncByteStream):
                closed = False

                async def __aiter__(self):
                    yield ("data: " + json.dumps(event) + "\n\n").encode()
                    raise httpx.ReadError("secret-stream-data")

                async def aclose(self):
                    self.closed = True

            transport_stream = BrokenStream()
            model = self.adapter(provider, lambda r: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=transport_stream,
            ))
            events = model.stream(LLMRequest([Message("user", "hi")]))
            self.assertIsInstance(await anext(events), TextDelta)
            with self.assertRaises(LLMError) as caught:
                await anext(events)
            self.assertEqual(caught.exception.code, "connection")
            self.assertNotIn("secret-stream-data", str(caught.exception))
            self.assertTrue(transport_stream.closed)

    async def test_stream_cancellation_closes_live_transport(self):
        for provider in ("openai", "deepseek"):
            event = ({"type": "response.output_text.delta", "delta": "partial"}
                     if provider == "openai" else chunk({"content": "partial"}))
            entered = asyncio.Event()

            class WaitingStream(httpx.AsyncByteStream):
                closed = False

                async def __aiter__(self):
                    yield ("data: " + json.dumps(event) + "\n\n").encode()
                    entered.set()
                    await asyncio.Event().wait()

                async def aclose(self):
                    self.closed = True

            transport_stream = WaitingStream()
            model = self.adapter(provider, lambda r: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=transport_stream,
            ))
            events = model.stream(LLMRequest([Message("user", "hi")]))
            await anext(events)
            task = asyncio.create_task(anext(events))
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(transport_stream.closed)

    async def test_stream_service_error_is_sanitized(self):
        for provider in ("openai", "deepseek"):
            data = ({"type": "error", "code": "server_error", "message": "secret", "param": None}
                    if provider == "openai" else {"error": {"message": "secret"}})
            model = self.adapter(provider, lambda r: sse([data]))
            with self.assertRaises(LLMError) as caught:
                _ = [e async for e in model.stream(LLMRequest([Message("user", "hi")]))]
            self.assertNotIn("secret", str(caught.exception))


class ProtocolTests(unittest.TestCase):
    def test_invalid_requests(self):
        factories = [
            lambda: LLMRequest([]),
            lambda: GenerationOptions(max_output_tokens=-1),
            lambda: GenerationOptions(reasoning="unsupported"),
            lambda: GenerationOptions(temperature=float("nan")),
            lambda: Message("user", tool_calls=(ToolCall("c", "f", "{}"),)),
            lambda: Message("tool", "result"),
            lambda: LLMRequest([Message("tool", "result", tool_call_id="c")]),
            lambda: LLMRequest([Message("assistant", tool_calls=(ToolCall("c", "f", "{}"),))]),
        ]
        for factory in factories:
            with self.assertRaises(LLMError):
                factory()

    def test_protocol_has_no_sdk_dependency(self):
        from pathlib import Path
        import ast
        for name in ("base.py", "types.py", "events.py", "errors.py", "__init__.py"):
            tree = ast.parse((Path("miniagent/models") / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(any(n.name.startswith(("openai", "httpx")) for n in node.names))
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or "").startswith(("openai", "httpx")))


class ConfigTests(unittest.TestCase):
    def test_provider_initialization(self):
        for provider, url, cls in [
            ("openai", "https://api.openai.com/v1", OpenAIAdapter),
            ("deepseek", "https://api.deepseek.com", DeepSeekAdapter),
        ]:
            config = ModelConfig(provider, "my-model", "fake-key", 12.0, 0)
            with patch("miniagent.bootstrap.AsyncOpenAI") as client:
                model = create_model(config)
                client.assert_called_once_with(
                    api_key="fake-key", base_url=url, timeout=12.0, max_retries=0,
                )
                self.assertIsInstance(model, cls)
            self.assertNotIn("fake-key", repr(config))

    def test_environment_keys_are_isolated(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake-openai"}, clear=True):
            self.assertEqual(ModelConfig.from_env("openai", "test").api_key, "fake-openai")
            with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
                ModelConfig.from_env("deepseek", "test")

    def test_invalid_config(self):
        for override in [
            {"provider": "other"}, {"model": " "}, {"api_key": " "},
            {"timeout": 0}, {"timeout": float("nan")}, {"timeout": float("inf")},
            {"max_retries": -1},
        ]:
            values = dict(provider="openai", model="test", api_key="fake")
            values.update(override)
            with self.subTest(override=override), self.assertRaises(ValueError):
                ModelConfig(**values)


class CliTests(unittest.TestCase):
    def test_success_uses_unified_request_for_both_providers(self):
        for provider in ("openai", "deepseek"):
            model = AsyncMock()
            model.generate.return_value = LLMResponse(Message("assistant", "hello"), "stop")
            with patch.dict("os.environ", {provider.upper() + "_API_KEY": "fake"}, clear=True), \
                 patch("cli.create_model", return_value=model), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                code = cli.main(["--provider", provider, "--model", "test", "--prompt", "hi",
                                 "--system", "Be brief.", "--reasoning", "high", "--max-output-tokens", "200"])
            self.assertEqual(code, 0)
            self.assertEqual(output.getvalue(), "hello\n")
            model.generate.assert_awaited_once_with(LLMRequest(
                [Message("system", "Be brief."), Message("user", "hi")],
                options=GenerationOptions(max_output_tokens=200, reasoning="high"),
            ))
            model.aclose.assert_awaited_once()

    def test_api_failure_exit_and_cleanup(self):
        model = AsyncMock()
        model.generate.side_effect = LLMError("timeout", "Model request timed out.")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}, clear=True), \
             patch("cli.create_model", return_value=model), \
             contextlib.redirect_stderr(io.StringIO()) as output:
            code = cli.main(["--provider", "openai", "--model", "test"])
        self.assertEqual(code, 1)
        self.assertIn("[timeout]", output.getvalue())
        model.aclose.assert_awaited_once()

    def test_missing_key_does_not_create_client(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch("cli.create_model") as factory, \
             contextlib.redirect_stderr(io.StringIO()) as output:
            code = cli.main(["--provider", "openai", "--model", "test"])
        self.assertEqual(code, 2)
        self.assertIn("OPENAI_API_KEY", output.getvalue())
        factory.assert_not_called()

    def test_stream_cli_does_not_duplicate_text(self):
        class FakeModel:
            closed = False

            async def stream(self, request):
                yield TextDelta("hello")
                yield ResponseCompleted(LLMResponse(Message("assistant", "hello"), "stop"))

            async def aclose(self):
                self.closed = True

        model = FakeModel()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}, clear=True), \
             patch("cli.create_model", return_value=model), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.main(["--provider", "openai", "--model", "test", "--stream"])
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), "hello\n")
        self.assertTrue(model.closed)


if __name__ == "__main__":
    unittest.main()
