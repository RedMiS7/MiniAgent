from .adapter_utils import state_payload
from .chat_adapter import ChatCompletionsAdapter
from .errors import LLMError
from .types import LLMRequest


class DeepSeekAdapter(ChatCompletionsAdapter):
    """DeepSeek-specific request and thinking-history rules."""
    _provider = "deepseek"

    def _request(self, request: LLMRequest) -> dict:
        options = request.options
        thinking = options.reasoning != "none"
        # 直接报错？
        if options.temperature is not None and thinking:
            raise LLMError("unsupported_feature", "Temperature requires reasoning='none' for DeepSeek.")
        messages = []
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
                raise LLMError("unsupported_feature", "DeepSeek cannot replay a refusal message.")
            if message.continuation:
                state = state_payload(message.continuation, "deepseek", self._model)
                if not isinstance(state, str):
                    raise LLMError("invalid_request", "Invalid DeepSeek continuation.")
                item["reasoning_content"] = state
            elif request.tools and thinking and message.role == "assistant":
                raise LLMError("invalid_request", "Thinking tool history requires its original continuation.")
            messages.append(item)
        params = {"model": self._model, "messages": messages}
        if request.tools:
            params["tools"] = [{
                "type": "function", "function": {
                    "name": t.name, "description": t.description, "parameters": t.parameters,
                },
            } for t in request.tools]
        if options.max_output_tokens is not None:
            params["max_tokens"] = options.max_output_tokens
        if options.reasoning is not None:
            params["extra_body"] = {"thinking": {"type": "enabled" if thinking else "disabled"}}
            if thinking:
                params["reasoning_effort"] = {
                    "medium": "high", "xhigh": "high",
                }.get(options.reasoning, options.reasoning)
        if options.temperature is not None:
            params["temperature"] = options.temperature
            # 返回 deepseek需要的json结构
        return params
