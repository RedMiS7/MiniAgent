import asyncio
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from miniagent.bootstrap import create_tools
from miniagent.config import ModelConfig
from miniagent.extensions.image import register
from miniagent.models import LLMError, ToolCall
from miniagent.models.image_adapter import ImageAdapter
from miniagent.models.image_backend import ImageCapabilities
from miniagent.tools import ToolContext, ToolExecutor, ToolRegistry
from test_models import completion, response

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZyQAAAAASUVORK5CYII="
)


class FakeImages:
    def __init__(self, inspect=True, generate=True):
        self.capabilities = ImageCapabilities(inspect, generate)
        self.calls = []

    async def inspect(self, data, mime_type, prompt):
        self.calls.append(("inspect", mime_type, prompt))
        return "a tiny image"

    async def generate(self, prompt, max_bytes):
        self.calls.append(("generate", prompt))
        return PNG


class ImageToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "input.png").write_bytes(PNG)
        self.current = FakeImages()
        self.other = FakeImages()
        self.registry = ToolRegistry()
        register(self.registry, {"current": self.current, "alternative": self.other})
        self.executor = ToolExecutor(self.registry, ToolContext(self.root))

    async def call(self, **arguments):
        return await self.executor.execute(ToolCall(id="call", name="image", arguments=json.dumps(arguments)))

    async def test_default_current_inspection(self):
        result = await self.call(action="inspect", path="input.png", prompt="describe")
        self.assertTrue(result.success)
        self.assertEqual(self.current.calls, [("inspect", "image/png", "describe")])
        self.assertFalse(self.other.calls)

    async def test_unsupported_does_not_fallback(self):
        self.current.capabilities = ImageCapabilities()
        result = await self.call(action="generate", output_path="new.png", prompt="draw")
        self.assertEqual(result.error_code, "unsupported_feature")
        self.assertEqual(result.content[0].value["available_models"], ["alternative"])
        self.assertFalse(self.current.calls)
        self.assertFalse(self.other.calls)
        self.assertFalse((self.root / "new.png").exists())

    async def test_explicit_selection_and_no_persistent_switch(self):
        result = await self.call(action="generate", output_path="new.png", prompt="draw", model="alternative")
        self.assertTrue(result.success)
        self.assertEqual((self.root / "new.png").read_bytes(), PNG)
        self.assertFalse(self.current.calls)
        await self.call(action="inspect", path="input.png", prompt="describe")
        self.assertEqual(len(self.current.calls), 1)

    async def test_unknown_model(self):
        result = await self.call(action="inspect", path="input.png", prompt="describe", model="arbitrary")
        self.assertEqual(result.error_code, "unsupported_feature")
        self.assertFalse(self.current.calls)

    async def test_no_configuration(self):
        self.executor.registry = create_tools()
        result = await self.call(action="inspect", path="input.png", prompt="describe")
        self.assertEqual(result.error_code, "unsupported_feature")

    async def test_output_existing_and_escape_before_api(self):
        for path, code in [("input.png", "already_exists"), ("../escape.png", "access_denied"),
                           ("new.jpg", "invalid_arguments"), ("absent/new.png", "invalid_arguments")]:
            result = await self.call(action="generate", output_path=path, prompt="draw")
            self.assertEqual(result.error_code, code)
        self.assertFalse(self.current.calls)

    async def test_input_invalid_and_oversize(self):
        (self.root / "bad.png").write_bytes(b"not an image")
        result = await self.call(action="inspect", path="bad.png", prompt="describe")
        self.assertEqual(result.error_code, "invalid_image")
        self.executor.context = ToolContext(self.root, max_image_bytes=2)
        result = await self.call(action="inspect", path="input.png", prompt="describe")
        self.assertEqual(result.error_code, "file_too_large")

    async def test_action_schema(self):
        for args in [
            {"action": "generate", "prompt": "draw"},
            {"action": "inspect", "prompt": "see", "output_path": "new.png"},
            {"action": "inspect", "prompt": "see", "path": "input.png", "extra": True},
        ]:
            result = await self.call(**args)
            self.assertEqual(result.error_code, "invalid_arguments")

    async def test_error_and_cancel(self):
        async def fail(*args):
            raise LLMError("permission", "Image model access denied.")
        self.current.generate = fail
        result = await self.call(action="generate", output_path="new.png", prompt="draw")
        self.assertEqual(result.error_code, "permission")
        self.assertEqual(result.content[0].value["available_models"], ["alternative"])
        self.assertFalse((self.root / "new.png").exists())
        async def cancel(*args):
            raise asyncio.CancelledError()
        self.current.generate = cancel
        with self.assertRaises(asyncio.CancelledError):
            await self.call(action="generate", output_path="new.png", prompt="draw")

    async def test_generation_does_not_overwrite_racing_file(self):
        async def race(*args):
            (self.root / "new.png").write_bytes(b"keep")
            return PNG
        self.current.generate = race
        result = await self.call(action="generate", output_path="new.png", prompt="draw")
        self.assertEqual(result.error_code, "already_exists")
        self.assertEqual((self.root / "new.png").read_bytes(), b"keep")


class ImageAdapterTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, provider, model, handler):
        adapter = ImageAdapter(ModelConfig(provider, model, "fake"))
        client = AsyncOpenAI(
            api_key="fake", base_url="https://example.invalid/v1", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(client.close)
        adapter._client = lambda generating=False: client
        return adapter

    async def test_vision_request_for_current_models(self):
        for provider, model in [("openai", "gpt-6-astra"),
                                ("deepseek", "deepseek-flash"), ("bailian", "qwen3.7-plus")]:
            def handler(request):
                data = json.loads(request.content)
                self.assertEqual(data["model"], model)
                content = (data["input"] if provider == "openai" else data["messages"])[0]["content"]
                self.assertIn("base64,", json.dumps(content[1]))
                self.assertIn(base64.b64encode(PNG).decode(), json.dumps(content[1]))
                return httpx.Response(200, json=response() if provider == "openai" else completion())
            self.assertEqual(await self.adapter(provider, model, handler).inspect(PNG, "image/png", "see"), "hello")

    async def test_openai_generation(self):
        for model in ("gpt-6-astra", "gpt-image-1.5"):
            def handler(request):
                data = json.loads(request.content)
                self.assertEqual(data["model"], model)
                encoded = base64.b64encode(PNG).decode()
                if model == "gpt-6-astra":
                    self.assertEqual(data["tool_choice"], {"type": "image_generation"})
                    return httpx.Response(200, json=response(output=[{
                        "id": "ig_1", "type": "image_generation_call", "status": "completed", "result": encoded,
                    }]))
                self.assertEqual(request.url.path, "/v1/images/generations")
                return httpx.Response(200, json={"created": 0, "data": [{"b64_json": encoded}]})
            data = await self.adapter("openai", model, handler).generate("draw", 10000)
            self.assertEqual(data, PNG)

    async def test_bailian_native_generation_and_download(self):
        calls = []
        def handler(request):
            calls.append(request)
            if request.method == "POST":
                body = json.loads(request.content)
                self.assertEqual(body["model"], "qwen-image-plus")
                self.assertEqual(body["input"]["messages"][0]["content"], [{"text": "draw"}])
                return httpx.Response(200, json={"output": {"choices": [{"message": {"content": [
                    {"image": "https://images.oss-cn-beijing.aliyuncs.com/test.png"},
                ]}}]}})
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(200, content=PNG)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = ImageAdapter(ModelConfig("bailian", "qwen-image-plus", "fake"))
        with patch("miniagent.models.image_adapter.httpx.AsyncClient", return_value=client):
            self.assertEqual(await adapter.generate("draw", 10000), PNG)
        self.assertEqual(len(calls), 2)
        self.assertTrue(client.is_closed)

    async def test_bailian_untrusted_url_is_not_downloaded(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"output": {"choices": [{"message": {"content": [
                {"image": "http://127.0.0.1/private"},
            ]}}]}})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = ImageAdapter(ModelConfig("bailian", "qwen-image-plus", "fake"))
        with patch("miniagent.models.image_adapter.httpx.AsyncClient", return_value=client):
            with self.assertRaises(LLMError):
                await adapter.generate("draw", 10000)
        self.assertEqual(len(calls), 1)

    async def test_capabilities_and_factory(self):
        config = ModelConfig("bailian", "glm-5", "fake")
        self.assertEqual(ImageAdapter(config).capabilities, ImageCapabilities())
        registry = create_tools(config, {"drawing": ModelConfig("bailian", "qwen-image-plus", "fake")})
        tool = registry.resolve("image")
        self.assertEqual(tool.models["current"].config, config)
        self.assertTrue(tool.models["drawing"].capabilities.generate)
        with self.assertRaises(ValueError):
            create_tools(config, {"current": config})

    async def test_http_failure_is_sanitized(self):
        adapter = self.adapter("openai", "gpt-6-astra", lambda r: httpx.Response(
            401, json={"error": {"message": "secret"}},
        ))
        with self.assertRaises(LLMError) as caught:
            await adapter.inspect(PNG, "image/png", "see")
        self.assertEqual(caught.exception.code, "authentication")
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
