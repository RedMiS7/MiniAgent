import json
import unittest

from miniagent.agent import AgentEvent, RunEvent
from miniagent.models import (
    ContinuationState, LLMRequest, LLMResponse, Message, ResponseCompleted,
    TextDelta, ToolArgumentsDelta, ToolCall, ToolCallStarted,
)
from miniagent.tools import ToolEvent, ToolResult


class EventTests(unittest.TestCase):
    def test_invalid_agent_events(self):
        for args in [
            ("unknown",), ("completed",), ("failed",), ("cancelled",),
            ("started", "stop"), ("progress", "stop"),
            ("completed", "length"), ("failed", "stop"),
            ("cancelled", "stop"), ("failed", " "),
        ]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                AgentEvent(*args)

    def test_agent_terminal_states(self):
        for kind, reason in [("completed", "stop"), ("failed", "model_error"),
                             ("failed", "length"), ("cancelled", "cancelled")]:
            self.assertEqual(AgentEvent(kind, reason).reason, reason)

    def test_invalid_run_context(self):
        for args in [
            ("", AgentEvent("started")), (12, AgentEvent("started")),
            ("run", object()), ("run", TextDelta("text")),
            ("run", TextDelta("text"), " "),
            ("run", ToolEvent("started", "call", "echo")),
            ("run", AgentEvent("started"), "model"),
        ]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                RunEvent(*args)

    def test_invalid_llm_events(self):
        factories = [
            lambda: TextDelta(1), lambda: ToolCallStarted(-1, "c", "echo"),
            lambda: ToolCallStarted(True, "c", "echo"),
            lambda: ToolCallStarted(0, "", "echo"),
            lambda: ToolCallStarted(0, "c", 1),
            lambda: ToolArgumentsDelta(-1, "{}"),
            lambda: ToolArgumentsDelta(0, {}),
            lambda: ResponseCompleted(Message("assistant", "text")),
        ]
        for factory in factories:
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory()

    def test_invalid_tool_events(self):
        for args in [
            ("unknown", "c", "echo"), ("started", "", "echo"),
            ("started", "c", " "), ("started", 1, "echo"),
            ("completed", "c", "echo", -1),
            ("completed", "c", "echo", float("nan")),
            ("completed", "c", "echo", float("inf")),
            ("completed", "c", "echo", True),
            ("failed", "c", "echo", 0, ""),
            ("completed", "c", "echo", 0, "execution_error"),
        ]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                ToolEvent(*args)

    def test_simulated_failure_round_trip(self):
        call = ToolCall("call-1", "read_file", '{"path":"missing"}')
        state = ContinuationState("fake", "fake-model", '{"state":1}')
        response = LLMResponse(Message("assistant", tool_calls=(call,),
                                       continuation=state), "tool_calls")
        result = ToolResult(False, error_code="io_error", error_message="Read failed.")
        request = LLMRequest((Message("user", "Read the file."), response.message,
                              result.to_message(call.id)))
        final = LLMResponse(Message("assistant", "The file could not be read."), "stop")
        events = [
            RunEvent("run-1", AgentEvent("started")),
            RunEvent("run-1", ToolCallStarted(0, call.id, call.name), "model-1"),
            RunEvent("run-1", ToolArgumentsDelta(0, call.arguments), "model-1"),
            RunEvent("run-1", ResponseCompleted(response), "model-1"),
            RunEvent("run-1", ToolEvent("started", call.id, call.name), "model-1"),
            RunEvent("run-1", ToolEvent("failed", call.id, call.name, 0.1, "io_error"), "model-1"),
            RunEvent("run-1", AgentEvent("progress")),
            RunEvent("run-1", TextDelta(final.text), "model-2"),
            RunEvent("run-1", ResponseCompleted(final), "model-2"),
            RunEvent("run-1", AgentEvent("completed", "stop")),
        ]
        self.assertEqual({event.run_id for event in events}, {"run-1"})
        self.assertEqual(events[1].event.id, events[5].event.call_id)
        self.assertEqual(events[1].model_call_id, events[5].model_call_id)
        self.assertEqual(events[1].event.index, events[2].event.index)
        self.assertIs(events[3].event.response, response)
        self.assertIs(request.messages[1].continuation, state)
        self.assertEqual(request.messages[2].tool_call_id, call.id)
        self.assertFalse(json.loads(request.messages[2].content)["success"])
        self.assertEqual(events[-1].event.type, "completed")

    def test_context_distinguishes_runs_and_model_calls(self):
        payload = ToolArgumentsDelta(0, "{}")
        events = [RunEvent(run, payload, model) for run, model in
                  [("a", "m1"), ("a", "m2"), ("b", "m1")]]
        self.assertEqual(len({(e.run_id, e.model_call_id, e.event.index) for e in events}), 3)
        self.assertNotIn("secret", repr(RunEvent("run", TextDelta("secret"), "model")))


if __name__ == "__main__":
    unittest.main()
