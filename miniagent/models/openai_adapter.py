import json
from typing import AsyncIterator

from openai import AsyncOpenAI

from .adapter_utils import require_text, sdk_errors, state_payload, tool_call_from, usage_from
from .errors import LLMError
from .events import LLMEvent, ResponseCompleted, TextDelta, ToolArgumentsDelta, ToolCallStarted
from .types import ContinuationState, LLMRequest, LLMResponse, Message


class OpenAIAdapter:
    """Responses API adapter; SDK objects never cross the protocol boundary."""

    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    def _request(self, request: LLMRequest) -> dict:
        options = request.options
        if self._model.startswith("gpt-6"):
            if options.reasoning == "none" or options.temperature is not None:
                raise LLMError("unsupported_feature", "This model cannot disable reasoning or set temperature.")
        inputs = []
        for message in request.messages:
            if message.continuation:
                items = state_payload(message.continuation, "openai", self._model)
                if not isinstance(items, list) or any(
                    not isinstance(item, dict) or item.get("type") != "reasoning" for item in items
                ):
                    raise LLMError("invalid_request", "Invalid OpenAI continuation.")
                inputs.extend(items)
            if message.role == "tool":
                inputs.append({
                    "type": "function_call_output", "call_id": message.tool_call_id,
                    "output": message.content,
                })
                continue
            if message.role == "assistant":
                content = []
                if message.content:
                    content.append({"type": "output_text", "text": message.content, "annotations": []})
                if message.refusal is not None:
                    content.append({"type": "refusal", "refusal": message.refusal})
                if content:
                    inputs.append({"type": "message", "role": "assistant", "content": content})
            else:
                inputs.append({"role": message.role, "content": message.content})
            for call in message.tool_calls:
                inputs.append({
                    "type": "function_call", "call_id": call.id,
                    "name": call.name, "arguments": call.arguments,
                })
        params = {
            "model": self._model, "input": inputs, "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if request.tools:
            params["tools"] = [{
                "type": "function", "name": t.name, "description": t.description,
                "parameters": t.parameters, "strict": False,
            } for t in request.tools]
        if options.max_output_tokens is not None:
            params["max_output_tokens"] = options.max_output_tokens
        if options.reasoning is not None:
            params["reasoning"] = {"effort": options.reasoning}
        if options.temperature is not None:
            params["temperature"] = options.temperature
        return params

    def _response(self, data: dict) -> LLMResponse:
        status = data["status"]
        if status == "failed":
            raise LLMError("api", "Model generation failed.")
        if status not in ("completed", "incomplete"):
            raise LLMError("invalid_response", "Invalid response status.")
        text, calls, reasoning, refusals = [], [], [], []
        for item in data["output"]:
            if item["type"] == "message":
                for part in item["content"]:
                    if part["type"] == "output_text":
                        text.append(require_text(part["text"]))
                    elif part["type"] == "refusal":
                        refusals.append(require_text(part["refusal"]))
                    else:
                        raise LLMError("unsupported_feature", "Unsupported model output.")
            elif item["type"] == "function_call":
                calls.append(tool_call_from(
                    require_text(item["call_id"]), require_text(item["name"]),
                    require_text(item["arguments"]),
                ))
            elif item["type"] == "reasoning":
                reasoning.append(item)
            else:
                raise LLMError("unsupported_feature", "Unsupported model output.")
        finish = "tool_calls" if calls else "stop"
        if refusals:
            finish = "refusal"
        if status == "incomplete":
            reason = (data.get("incomplete_details") or {}).get("reason")
            if reason not in ("max_output_tokens", "content_filter"):
                raise LLMError("invalid_response", "Unknown incomplete response reason.")
            finish = "length" if reason == "max_output_tokens" else "content_filter"
        if finish == "stop" and not "".join(text).strip():
            raise LLMError("invalid_response", "Model returned no answer.")
        continuation = None
        if reasoning:
            continuation = ContinuationState("openai", self._model, json.dumps(reasoning))
        return LLMResponse(
            Message(
                "assistant", "".join(text), tuple(calls), continuation=continuation,
                refusal="".join(refusals) if refusals else None,
            ),
            finish, usage_from(data.get("usage"), "input_tokens", "output_tokens"),
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        params = self._request(request)
        with sdk_errors():
            response = await self._client.responses.create(**params, stream=False)
            return self._response(response.model_dump(exclude_none=True))

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        params = self._request(request)
        with sdk_errors():
            stream = await self._client.responses.create(**params, stream=True)
            try:
                started = set()
                async for event in stream:
                    data = event.model_dump(exclude_none=True)
                    kind = data["type"]
                    if kind == "response.output_text.delta":
                        yield TextDelta(require_text(data["delta"]))
                    elif kind == "response.output_item.added" and data["item"]["type"] == "function_call":
                        item = data["item"]
                        index = data["output_index"]
                        if index in started:
                            raise LLMError("invalid_response", "Duplicate tool stream index.")
                        started.add(index)
                        yield ToolCallStarted(index, require_text(item["call_id"]), require_text(item["name"]))
                        if item.get("arguments"):
                            yield ToolArgumentsDelta(index, require_text(item["arguments"]))
                    elif kind == "response.function_call_arguments.delta":
                        index = data["output_index"]
                        if index not in started:
                            raise LLMError("invalid_response", "Tool delta arrived before tool start.")
                        yield ToolArgumentsDelta(index, require_text(data["delta"]))
                    elif kind in ("response.completed", "response.incomplete"):
                        result = self._response(data["response"])
                        yield ResponseCompleted(result)
                        return
                    elif kind in ("error", "response.failed"):
                        raise LLMError("api", "Model stream failed.")
                raise LLMError("invalid_response", "Model stream ended without a final response.")
            finally:
                await stream.close()

    async def aclose(self) -> None:
        await self._client.close()
