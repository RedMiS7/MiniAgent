import json
import unittest

from pydantic import TypeAdapter, ValidationError

from miniagent.agent import (
    AgentCancelled, AgentCompleted, AgentEvent, AgentFailed, AgentProgress, AgentStarted,
)
from miniagent.models import (
    ContinuationState, LLMEvent, LLMRequest, LLMResponse, Message, ResponseCompleted,
    TextDelta, ToolArgumentsDelta, ToolCall, ToolCallStarted,
)
from miniagent.tools import (
    ToolCancelled, ToolCompleted, ToolEvent, ToolFailed, ToolResult, ToolStarted,
)


class EventTests(unittest.TestCase):
    def test_tagged_events_round_trip(self):
        response = LLMResponse(message=Message(role="assistant", content="hello"), finish_reason="stop")
        groups = [
            (AgentEvent, [AgentStarted(), AgentProgress(), AgentCompleted(),
                          AgentFailed(reason="model_error"), AgentCancelled()]),
            (LLMEvent, [TextDelta(text="hello"), ToolCallStarted(index=0, id="c", name="echo"),
                        ToolArgumentsDelta(index=0, delta="{"), ResponseCompleted(response=response)]),
            (ToolEvent, [ToolStarted(call_id="c", tool_name="echo"),
                         ToolCompleted(call_id="c", tool_name="echo", elapsed_seconds=0.1),
                         ToolFailed(call_id="c", tool_name="echo", error_code="io_error"),
                         ToolCancelled(call_id="c", tool_name="echo")]),
        ]
        tags = set()
        for event_type, events in groups:
            adapter = TypeAdapter(event_type)
            for event in events:
                with self.subTest(event=event.type):
                    self.assertNotIn(event.type, tags)
                    tags.add(event.type)
                    restored = adapter.validate_json(event.model_dump_json())
                    self.assertIs(type(restored), type(event))
                    self.assertEqual(restored, event)
            with self.assertRaises(ValidationError):
                adapter.validate_python({"type": "unknown"})
        self.assertEqual(len(tags), 13)

    def test_invalid_agent_events(self):
        factories = [
            lambda: AgentStarted(reason="stop"),
            lambda: AgentProgress(reason="stop"),
            lambda: AgentProgress(step=0),
            lambda: AgentProgress(step=True),
            lambda: AgentProgress(phase="unknown"),
            lambda: AgentCompleted(reason="length"),
            lambda: AgentFailed(),
            lambda: AgentFailed(reason="stop"),
            lambda: AgentFailed(reason="cancelled"),
            lambda: AgentFailed(reason=" "),
            lambda: AgentCancelled(reason="stop"),
        ]
        for factory in factories:
            with self.subTest(factory=factory), self.assertRaises(ValidationError):
                factory()

    def test_invalid_llm_events(self):
        factories = [
            lambda: TextDelta(text=1),
            lambda: TextDelta(text="text", type="llm_response_completed"),
            lambda: ToolCallStarted(index=-1, id="c", name="echo"),
            lambda: ToolCallStarted(index=True, id="c", name="echo"),
            lambda: ToolCallStarted(index=0, id="", name="echo"),
            lambda: ToolCallStarted(index=0, id="c", name=1),
            lambda: ToolArgumentsDelta(index=-1, delta="{}"),
            lambda: ToolArgumentsDelta(index=0, delta={}),
            lambda: ResponseCompleted(response=Message(role="assistant", content="text")),
        ]
        for factory in factories:
            with self.subTest(factory=factory), self.assertRaises(ValidationError):
                factory()

    def test_invalid_tool_events(self):
        factories = [
            lambda: ToolStarted(call_id="", tool_name="echo"),
            lambda: ToolStarted(call_id="c", tool_name=" "),
            lambda: ToolStarted(call_id=1, tool_name="echo"),
            lambda: ToolStarted(call_id="c", tool_name="echo", elapsed_seconds=0),
            lambda: ToolCompleted(call_id="c", tool_name="echo", elapsed_seconds=-1),
            lambda: ToolCompleted(call_id="c", tool_name="echo", elapsed_seconds=float("nan")),
            lambda: ToolCompleted(call_id="c", tool_name="echo", elapsed_seconds=float("inf")),
            lambda: ToolCompleted(call_id="c", tool_name="echo", elapsed_seconds=True),
            lambda: ToolFailed(call_id="c", tool_name="echo", error_code=""),
            lambda: ToolCompleted(call_id="c", tool_name="echo", error_code="execution_error"),
            lambda: ToolCancelled(call_id="c", tool_name="echo", error_code="execution_error"),
        ]
        for factory in factories:
            with self.subTest(factory=factory), self.assertRaises(ValidationError):
                factory()

    def test_events_are_frozen_and_have_fixed_tags(self):
        event = TextDelta(text="hello")
        with self.assertRaises(ValidationError):
            event.text = "changed"
        with self.assertRaises(ValidationError):
            ToolStarted(call_id="c", tool_name="echo", type="tool_failed")

    def test_simulated_failure_round_trip(self):
        call = ToolCall(id="call-1", name="read_file", arguments='{"path":"missing"}')
        state = ContinuationState(provider="fake", model="fake-model", payload='{"state":1}')
        response = LLMResponse(message=Message(role="assistant", tool_calls=(call,),
                                               continuation=state), finish_reason="tool_calls")
        result = ToolResult(success=False, error_code="io_error", error_message="Read failed.")
        request = LLMRequest(messages=(Message(role="user", content="Read the file."),
                                       response.message, result.to_message(call.id)))
        final = LLMResponse(message=Message(role="assistant", content="The file could not be read."),
                            finish_reason="stop")
        events = [
            AgentStarted(),
            ToolCallStarted(index=0, id=call.id, name=call.name),
            ToolArgumentsDelta(index=0, delta=call.arguments),
            ResponseCompleted(response=response),
            ToolStarted(call_id=call.id, tool_name=call.name),
            ToolFailed(call_id=call.id, tool_name=call.name, elapsed_seconds=0.1, error_code="io_error"),
            AgentProgress(),
            TextDelta(text=final.text),
            ResponseCompleted(response=final),
            AgentCompleted(),
        ]
        self.assertEqual(events[1].id, events[5].call_id)
        self.assertEqual(events[1].index, events[2].index)
        self.assertIs(events[3].response, response)
        self.assertIs(request.messages[1].continuation, state)
        self.assertEqual(request.messages[2].tool_call_id, call.id)
        self.assertFalse(json.loads(request.messages[2].content)["success"])
        self.assertIsInstance(events[-1], AgentCompleted)


if __name__ == "__main__":
    unittest.main()
