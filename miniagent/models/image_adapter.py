import base64
from urllib.parse import urlparse

import httpx
from openai import AsyncOpenAI

from miniagent.config import ModelConfig
from .adapter_utils import require_text, sdk_errors
from .errors import LLMError
from .image_backend import ImageCapabilities

# Capabilities verified for these exact provider/model combinations.
CAPABILITIES = {
    ("openai", "gpt-6-astra"): ImageCapabilities(True, True),
    ("openai", "gpt-image-1.5"): ImageCapabilities(False, True),
    ("bailian", "qwen3.7-plus"): ImageCapabilities(True, False),
    ("bailian", "glm-5"): ImageCapabilities(),
    ("bailian", "qwen-image-plus"): ImageCapabilities(False, True),
    ("deepseek", "deepseek-flash"): ImageCapabilities(True, False),
}


class ImageAdapter:
    """Image-only calls bound to one model; never falls back to another model."""
    def __init__(self, config: ModelConfig):
        self.config = config
        self.capabilities = CAPABILITIES.get((config.provider, config.model), ImageCapabilities())

    def _client(self, generating=False):
        return AsyncOpenAI(
            api_key=self.config.api_key, base_url=self.config.base_url,
            timeout=self.config.timeout,
            max_retries=0 if generating else self.config.max_retries,
        )

    async def inspect(self, data: bytes, mime_type: str, prompt: str) -> str:
        if not self.capabilities.inspect:
            raise LLMError("unsupported_feature", "Image recognition is not supported by this adapter/model.")
        url = f"data:{mime_type};base64," + base64.b64encode(data).decode("ascii")
        with sdk_errors():
            async with self._client() as client:
                if self.config.provider == "openai":
                    response = await client.responses.create(
                        model=self.config.model, store=False,
                        input=[{"role": "user", "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_image", "image_url": url},
                        ]}],
                    )
                    if response.status != "completed":
                        raise LLMError("invalid_response", "Image recognition did not complete.")
                    text = response.output_text
                else:
                    response = await client.chat.completions.create(
                        model=self.config.model, stream=False,
                        messages=[{"role": "user", "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": url}},
                        ]}],
                    )
                    if len(response.choices) != 1 or response.choices[0].finish_reason != "stop":
                        raise LLMError("invalid_response", "Image recognition did not complete.")
                    text = response.choices[0].message.content
                if not require_text(text).strip():
                    raise LLMError("invalid_response", "Image recognition returned no text.")
                return text

    async def generate(self, prompt: str, max_bytes: int) -> bytes:
        if not self.capabilities.generate:
            raise LLMError("unsupported_feature", "Image generation is not supported by this adapter/model.")
        with sdk_errors():
            if self.config.provider == "bailian":
                return await self._bailian_generate(prompt, max_bytes)
            async with self._client(generating=True) as client:
                if self.config.model == "gpt-6-astra":
                    response = await client.responses.create(
                        model=self.config.model, input=prompt, store=False,
                        tools=[{"type": "image_generation", "output_format": "png"}],
                        tool_choice={"type": "image_generation"},
                    )
                    if response.status != "completed":
                        raise LLMError("invalid_response", "Image generation did not complete.")
                    results = [item.result for item in response.output if item.type == "image_generation_call"]
                    if len(results) != 1:
                        raise LLMError("invalid_response", "Expected one generated image.")
                    encoded = results[0]
                else:
                    response = await client.images.generate(
                        model=self.config.model, prompt=prompt, n=1, output_format="png",
                    )
                    encoded = response.data[0].b64_json
                if not isinstance(encoded, str) or len(encoded) > ((max_bytes + 2) // 3) * 4:
                    raise LLMError("invalid_response", "Image data is missing or exceeds the size limit.")
                data = base64.b64decode(encoded, validate=True)
                if not data or len(data) > max_bytes:
                    raise LLMError("invalid_response", "Image exceeds the size limit.")
                return data

    @staticmethod
    def _http_status(response):
        if response.is_success:
            return
        code = {401: "authentication", 403: "permission", 429: "rate_limit"}.get(
            response.status_code, "server" if response.status_code >= 500 else "invalid_request",
        )
        raise LLMError(code, "Image service rejected the request.")

    async def _bailian_generate(self, prompt: str, max_bytes: int) -> bytes:
        # Native synchronous API; no duplicate task submission or automatic retries.
        async with httpx.AsyncClient(timeout=self.config.timeout, follow_redirects=False) as client:
            response = await client.post(
                "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                json={
                    "model": self.config.model,
                    "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
                    "parameters": {"n": 1},
                },
            )
            self._http_status(response)
            parts = response.json()["output"]["choices"][0]["message"]["content"]
            urls = [part["image"] for part in parts if "image" in part]
            if len(urls) != 1:
                raise LLMError("invalid_response", "Expected one generated image URL.")
            url = urlparse(urls[0])
            if (url.scheme != "https" or not (url.hostname or "").endswith(".aliyuncs.com")
                    or url.username or url.password or url.port not in (None, 443)):
                raise LLMError("invalid_response", "Untrusted generated image URL.")
            # No API credentials on the signed image download.
            async with client.stream("GET", urls[0]) as download:
                self._http_status(download)
                result = bytearray()
                async for part in download.aiter_bytes():
                    if len(result) + len(part) > max_bytes:
                        raise LLMError("invalid_response", "Generated image exceeds the size limit.")
                    result.extend(part)
            return bytes(result)
