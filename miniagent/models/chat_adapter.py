import json
from abc import ABC, abstractmethod
from typing import AsyncIterator

from openai import AsyncOpenAI

from .adapter_utils import require_text, sdk_errors, tool_call_from, usage_from
from .errors import LLMError
from .events import LLMEvent, ResponseCompleted, TextDelta, ToolArgumentsDelta, ToolCallStarted
from .types import ContinuationState, LLMRequest, LLMResponse, Message


class ChatCompletionsAdapter(ABC):
    """Shared Chat Completions transport, response and SSE conversion."""
    _provider: str

    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    @abstractmethod
    def _request(self, request: LLMRequest) -> dict:
        ...

    def _response(self, data: dict) -> LLMResponse:
        choices = data["choices"]
        if len(choices) != 1:
            raise LLMError("invalid_response", "Expected one model choice.")
        choice = choices[0]
        message = choice["message"]
        finish = choice["finish_reason"]
        if finish not in ("stop", "tool_calls", "length", "content_filter"):
            raise LLMError("invalid_response", "Unknown finish reason.")
        calls = []
        for call in message.get("tool_calls") or []:
            if call["type"] != "function":
                raise LLMError("unsupported_feature", "Only function tools are supported.")
            calls.append(tool_call_from(
                require_text(call["id"]), require_text(call["function"]["name"]),
                require_text(call["function"]["arguments"]),
            ))
        text = message.get("content")
        text = "" if text is None else require_text(text)
        refusal = message.get("refusal")
        if refusal is not None:
            refusal = require_text(refusal)
            finish = "refusal"
        if finish == "stop" and calls:
            finish = "tool_calls"
        if finish == "tool_calls" and not calls:
            raise LLMError("invalid_response", "Tool finish without a tool call.")
        if finish == "stop" and not text.strip():
            raise LLMError("invalid_response", "Model returned no answer.")
        state = message.get("reasoning_content")
        continuation = None
        if state is not None:
            continuation = ContinuationState(
                self._provider, self._model, json.dumps(require_text(state)),
            )
        return LLMResponse(
            Message("assistant", text, tuple(calls), continuation=continuation, refusal=refusal),
            finish, usage_from(data.get("usage"), "prompt_tokens", "completion_tokens"),
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        params = self._request(request)
        with sdk_errors():
            # 真正调用LLM的函数
            response = await self._client.chat.completions.create(**params, stream=False)
            return self._response(response.model_dump())

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        params = self._request(request)
        with sdk_errors():
            stream = await self._client.chat.completions.create(
                **params, stream=True, stream_options={"include_usage": True},
            )
            try:
                text, reasoning, refusal = [], [], []
                has_reasoning = False
                calls = {}
                started = set()
                finish = None
                usage = None
                async for chunk in stream:
                    data = chunk.model_dump()
                    if data.get("usage") is not None:
                        usage = data["usage"]
                    for choice in data["choices"]:
                        if choice["index"] != 0 or finish is not None:
                            raise LLMError("invalid_response", "Unexpected stream choice.")
                        delta = choice["delta"]
                        if delta.get("content") is not None:
                            part = require_text(delta["content"])
                            text.append(part)
                            yield TextDelta(part)
                        if delta.get("reasoning_content") is not None:
                            has_reasoning = True
                            reasoning.append(require_text(delta["reasoning_content"]))
                        if delta.get("refusal") is not None:
                            refusal.append(require_text(delta["refusal"]))
                        for update in delta.get("tool_calls") or []:
                            index = update["index"]
                            if type(index) is not int or index < 0:
                                raise LLMError("invalid_response", "Invalid tool stream index.")
                            call = calls.setdefault(index, {
                                "id": "", "type": "function", "function": {"name": "", "arguments": ""},
                            })
                            if update.get("type") not in (None, "function"):
                                raise LLMError("unsupported_feature", "Only function tools are supported.")
                            if update.get("id"):
                                if call["id"] and call["id"] != update["id"]:
                                    raise LLMError("invalid_response", "Tool ID changed during stream.")
                                call["id"] = require_text(update["id"])
                            function = update.get("function") or {}
                            if function.get("name"):
                                if index in started:
                                    raise LLMError("invalid_response", "Tool name changed during stream.")
                                call["function"]["name"] += require_text(function["name"])
                            arguments = require_text(function.get("arguments") or "")
                            call["function"]["arguments"] += arguments
                            if index not in started and call["id"] and call["function"]["name"]:
                                started.add(index)
                                yield ToolCallStarted(index, call["id"], call["function"]["name"])
                                if call["function"]["arguments"]:
                                    yield ToolArgumentsDelta(index, call["function"]["arguments"])
                            elif index in started and arguments:
                                yield ToolArgumentsDelta(index, arguments)
                        if choice.get("finish_reason") is not None:
                            finish = choice["finish_reason"]
                if finish is None or set(calls) != started:
                    raise LLMError("invalid_response", "Model stream ended before completion.")
                message = {
                    "content": "".join(text), "tool_calls": [calls[i] for i in sorted(calls)],
                    "reasoning_content": "".join(reasoning) if has_reasoning else None,
                    "refusal": "".join(refusal) if refusal else None,
                }
                result = self._response({
                    "choices": [{"message": message, "finish_reason": finish}], "usage": usage,
                })
                yield ResponseCompleted(result)
            finally:
                await stream.close()

    async def aclose(self) -> None:
        await self._client.close()
