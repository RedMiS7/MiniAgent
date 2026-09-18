import unittest

from pydantic import ValidationError

from miniagent.agent import AgentCompleted, RunResult
from miniagent.models import ContinuationState, Message, ToolCall
from miniagent.tools import ToolResult


class RunTypesTests(unittest.TestCase):
    def test_result_rejects_nonterminal_statuses(self):
        for status in ("pending", "running", "unknown"):
            with self.subTest(status=status), self.assertRaises(ValidationError):
                RunResult(status=status, reason="stop")

    def test_success_preserves_tool_failure_history_and_continuation(self):
        state = ContinuationState(provider="fake", model="fake", payload='{"state":1}')
        call = ToolCall(id="c", name="read_file", arguments='{"path":"missing"}')
        messages = (
            Message(role="user", content="Read a file."),
            Message(role="assistant", tool_calls=(call,), continuation=state),
            ToolResult(success=False, error_code="io_error",
                       error_message="Read failed.").to_message(call.id),
            Message(role="assistant", content="The file could not be read.", continuation=state),
        )
        completed = AgentCompleted(messages=messages)
        result = RunResult(status="succeeded", reason=completed.reason, messages=completed.messages)
        self.assertEqual(result.messages, messages)
        self.assertIs(result.messages[-1].continuation, state)
        self.assertEqual(result.messages[2].tool_call_id, call.id)
        self.assertEqual(RunResult.model_validate_json(result.model_dump_json()), result)

    def test_plain_text_success_normalizes_history(self):
        messages = [Message(role="user", content="Hi"), Message(role="assistant", content="Hello")]
        result = RunResult(status="succeeded", reason="stop", messages=messages)
        messages.clear()
        self.assertIsInstance(result.messages, tuple)
        self.assertEqual(len(result.messages), 2)

    def test_non_exception_failure_and_cancellation(self):
        for reason in ("step_limit", "length", "refusal", "content_filter"):
            result = RunResult(status="failed", reason=reason)
            self.assertEqual(result.messages, ())
            self.assertIsNone(result.error_code)
            self.assertEqual(RunResult.model_validate_json(result.model_dump_json()), result)
        result = RunResult(status="cancelled", reason="cancelled")
        self.assertEqual(RunResult.model_validate_json(result.model_dump_json()), result)

    def test_error_details_are_preserved(self):
        for reason, code in (("model_error", "connection"), ("execution_error", "execution_error")):
            result = RunResult(status="failed", reason=reason,
                               error_code=code, error_message="Operation failed.")
            self.assertEqual(result.error_code, code)
            self.assertEqual(result.error_message, "Operation failed.")
            self.assertEqual(RunResult.model_validate_json(result.model_dump_json()), result)

    def test_contradictory_results_are_rejected(self):
        answer = (Message(role="assistant", content="Done"),)
        cases = [
            dict(status="succeeded", reason="length", messages=answer),
            dict(status="succeeded", reason="stop", messages=answer,
                 error_code="api", error_message="Failed"),
            dict(status="cancelled", reason="stop"),
            dict(status="cancelled", reason="cancelled", error_code="api", error_message="Failed"),
            dict(status="failed", reason="stop"),
            dict(status="failed", reason="cancelled"),
            dict(status="failed", reason="tool_calls"),
            dict(status="failed", reason="step_limit", messages=answer),
            dict(status="cancelled", reason="cancelled", messages=answer),
            dict(status="failed", reason="model_error", error_code="api"),
            dict(status="failed", reason="model_error", error_message="Failed"),
            dict(status="failed", reason="model_error", error_code="", error_message="Failed"),
            dict(status="failed", reason="model_error", error_code="api", error_message=" "),
            dict(status="failed", reason=" "),
            dict(status="failed", reason=123),
            dict(status="failed", reason="step_limit", extra_field=True),
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValidationError):
                RunResult(**case)

    def test_success_rejects_incomplete_or_invalid_history(self):
        call = ToolCall(id="c", name="echo", arguments="{}")
        pending = Message(role="assistant", tool_calls=(call,))
        tool = Message(role="tool", content="ok", tool_call_id="c")
        answer = Message(role="assistant", content="Done")
        for messages in ((), (Message(role="user"),), (pending,), (pending, answer),
                         (tool, answer), (pending, tool), (pending, tool, tool, answer),
                         (pending, tool, pending, tool, answer)):
            with self.subTest(messages=messages), self.assertRaises(ValidationError):
                RunResult(status="succeeded", reason="stop", messages=messages)

    def test_results_are_frozen_and_sensitive_fields_hidden_from_repr(self):
        result = RunResult(status="succeeded", reason="stop",
                           messages=(Message(role="assistant", content="private-history"),))
        with self.assertRaises(ValidationError):
            result.status = "failed"
        self.assertNotIn("private-history", repr(result))
        failure = RunResult(status="failed", reason="model_error",
                            error_code="api", error_message="private-diagnostic")
        self.assertNotIn("private-diagnostic", repr(failure))


if __name__ == "__main__":
    unittest.main()
