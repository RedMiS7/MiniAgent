from .adapter_utils import state_payload
from .chat_adapter import ChatCompletionsAdapter
from .errors import LLMError
from .types import LLMRequest


class BailianAdapter(ChatCompletionsAdapter):
    """Beijing Model Studio: Alibaba-hosted qwen3.7-plus and glm-5."""
    _provider = "bailian"

    def _request(self, request: LLMRequest) -> dict:
        if self._model not in ("qwen3.7-plus", "glm-5"):
            raise LLMError("unsupported_feature", "Unsupported Bailian model.")
        options = request.options
        extra = {}
        if options.reasoning is not None:
            extra["enable_thinking"] = options.reasoning != "none"
            if options.reasoning != "none":
                if self._model == "qwen3.7-plus":
                    raise LLMError(
                        "unsupported_feature",
                        "Qwen3.7 Plus has no documented reasoning effort mapping; omit reasoning for its default or use 'none'.",
                    )
                extra["reasoning_effort"] = {
                    "low": "high", "medium": "high", "xhigh": "max",
                }.get(options.reasoning, options.reasoning)

        messages = []
        has_state = False
        for message in request.messages:
            item = {"role": message.role, "content": message.content}
            if message.tool_call_id:
                item["tool_call_id"] = message.tool_call_id
            if message.tool_calls:
                item["tool_calls"] = [{
                    "id": call.id, "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                } for call in message.tool_calls]
            if message.refusal is not None:
                raise LLMError("unsupported_feature", "Bailian cannot replay refusal messages.")
            if message.continuation:
                state = state_payload(message.continuation, self._provider, self._model)
                if not isinstance(state, str):
                    raise LLMError("invalid_request", "Invalid Bailian continuation.")
                item["reasoning_content"] = state
                has_state = True
            messages.append(item)
        if has_state:
            if self._model == "qwen3.7-plus":
                extra["preserve_thinking"] = True
            else:
                extra["clear_thinking"] = False

        params = {"model": self._model, "messages": messages}
        if request.tools:
            params["tools"] = [{
                "type": "function", "function": {
                    "name": tool.name, "description": tool.description,
                    "parameters": tool.parameters,
                },
            } for tool in request.tools]
        if options.max_output_tokens is not None:
            params["max_completion_tokens"] = options.max_output_tokens
        if options.temperature is not None:
            params["temperature"] = options.temperature
        if extra:
            params["extra_body"] = extra
        return params
