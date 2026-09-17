import json
import unittest

from pydantic import BaseModel, ValidationError

from miniagent.models import (
    ContinuationState, GenerationOptions, LLMError, LLMRequest, LLMResponse,
    Message, TokenUsage, ToolCall, ToolDefinition,
)
from miniagent.models.adapter_utils import sdk_errors
from miniagent.tools import ToolContent, ToolResult


class CoreTypesTests(unittest.TestCase):
    def test_models_use_pydantic_and_keyword_construction(self):
        for cls in (ContinuationState, GenerationOptions, LLMRequest, LLMResponse,
                    Message, TokenUsage, ToolCall, ToolDefinition, ToolContent, ToolResult):
            self.assertTrue(issubclass(cls, BaseModel), cls.__name__)

    def test_invalid_core_values_are_rejected(self):
        factories = [
            lambda: ToolCall(id=123, name="echo", arguments="{}"),
            lambda: ToolCall(id=" ", name="echo", arguments="{}"),
            lambda: LLMResponse(message=Message(role="user"), finish_reason="stop"),
            lambda: LLMResponse(message=Message(role="assistant"), finish_reason="unknown"),
            lambda: LLMResponse(message=Message(role="assistant"), finish_reason="tool_calls"),
            lambda: Message(role="assistant", tool_calls=[object()]),
            lambda: Message(role="user", tool_call_id="c"),
            lambda: Message(role="assistant", continuation=object()),
            lambda: LLMRequest(messages=[Message(role="user")], options=object()),
            lambda: GenerationOptions(max_output_tokens=True),
            lambda: GenerationOptions(temperature=True),
            lambda: ToolDefinition(name="echo", description="Echo", parameters={"type": "array"}),
            lambda: ToolDefinition(name="echo", description="Echo", parameters={"type": "object", "x": object()}),
            lambda: ToolContent(type="json", value={1: "non-string key"}),
            lambda: LLMResponse(message=Message(role="assistant", tool_calls=(
                ToolCall(id="c", name="echo", arguments="{}"),)), finish_reason="stop"),
            lambda: TokenUsage(input_tokens=-1, output_tokens=0),
            lambda: TokenUsage(input_tokens="1", output_tokens=0),
            lambda: ToolContent(type="text", value=123),
            lambda: ToolContent(type="unknown", value="text"),
            lambda: ToolContent(type="json", value=object()),
            lambda: ToolContent(type="json", value={"x": float("nan")}),
            lambda: ToolResult(success="yes"),
            lambda: ToolResult(success=True, error_code="execution_error"),
            lambda: ToolResult(success=True, error_message="error"),
            lambda: ToolResult(success=True, content=[object()]),
            lambda: ContinuationState(provider="fake", model="fake", payload="invalid"),
            lambda: Message(role="user", extra_field="x"),
        ]
        for factory in factories:
            with self.subTest(factory=factory), self.assertRaises(ValidationError):
                factory()

    def test_sequence_normalization_and_continuation_round_trip(self):
        state = ContinuationState(provider="fake", model="model", payload='{"key":"value"}')
        call = ToolCall(id="c", name="echo", arguments="{")
        answer = Message(role="assistant", tool_calls=[call], continuation=state)
        result = ToolResult(success=False, error_code="invalid_arguments", error_message="Invalid.")
        request = LLMRequest(messages=[Message(role="user", content="hello"), answer, result.to_message("c")],
                             tools=[ToolDefinition(name="echo", description="Echo", parameters={"type": "object"})])
        self.assertIsInstance(request.messages, tuple)
        self.assertIsInstance(request.tools, tuple)
        self.assertIsInstance(answer.tool_calls, tuple)
        self.assertIs(request.messages[1].continuation, state)
        self.assertEqual(request.messages[1].tool_calls[0].arguments, "{")
        self.assertEqual(request.messages[-1].tool_call_id, "c")
        self.assertEqual(LLMRequest.model_validate_json(request.model_dump_json()), request)

    def test_tool_wire_format_is_preserved(self):
        success = ToolResult(success=True, content=[ToolContent(type="text", value="你好")])
        self.assertIsInstance(success.content, tuple)
        self.assertEqual(json.loads(success.to_message("c").content), {
            "success": True, "content": [{"type": "text", "value": "你好"}],
        })
        failure = ToolResult(success=False, error_code="io_error", error_message="Failed.")
        self.assertEqual(json.loads(failure.to_message("c").content), {
            "success": False, "content": [], "error": {"code": "io_error", "message": "Failed."},
        })

    def test_invalid_tool_history_still_rejected(self):
        call = ToolCall(id="c", name="echo", arguments="{}")
        assistant = Message(role="assistant", tool_calls=(call,))
        result = Message(role="tool", content="ok", tool_call_id="c")
        histories = [
            [assistant],
            [result],
            [assistant, Message(role="user", content="continue"), result],
            [assistant, result, result],
            [assistant, result, assistant, result],
            [Message(role="assistant", tool_calls=(call, call)), result],
        ]
        for history in histories:
            with self.subTest(history=history), self.assertRaises(ValidationError):
                LLMRequest(messages=history)
        tool = ToolDefinition(name="echo", description="Echo", parameters={"type": "object"})
        with self.assertRaises(ValidationError):
            LLMRequest(messages=[Message(role="user")], tools=[tool, tool])

    def test_nested_json_values_preserve_types(self):
        value = {"items": [None, True, 1, 1.5, "1", {"text": "你好"}]}
        content = ToolContent(type="json", value=value)
        restored = ToolContent.model_validate_json(content.model_dump_json())
        self.assertEqual(restored.value, value)
        self.assertIs(type(restored.value["items"][1]), bool)
        self.assertIs(type(restored.value["items"][2]), int)
        self.assertIs(type(restored.value["items"][4]), str)
        # Existing failed results without diagnostic fields remain valid.
        self.assertFalse(ToolResult(success=False).success)

    def test_frozen_fields_and_private_repr(self):
        message = Message(role="user", content="hello")
        with self.assertRaises(ValidationError):
            message.role = "assistant"
        state = ContinuationState(provider="fake", model="fake", payload='"private"')
        self.assertNotIn("private", repr(state))
        self.assertNotIn("private", repr(ToolResult(
            success=True, content=(ToolContent(type="text", value="private"),))))

    def test_sdk_boundary_sanitizes_validation_errors(self):
        with self.assertRaises(LLMError) as caught:
            with sdk_errors():
                LLMResponse(message=Message(role="user", content="private"), finish_reason="stop")
        self.assertEqual(caught.exception.code, "invalid_response")
        self.assertNotIn("private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
