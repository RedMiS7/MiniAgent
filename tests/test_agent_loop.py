import asyncio
from contextlib import aclosing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import TypeAdapter

import agent_cli
from miniagent.agent import AgentLoop, LoopEvent
from miniagent.bootstrap import create_tools
from miniagent.models import (
    ContinuationState, LLMError, LLMResponse, Message, ResponseCompleted,
    TextDelta, ToolCall, ToolDefinition,
)
from miniagent.tools import ToolContext, ToolExecutor, ToolResult


def response(text="总结完成", calls=(), reason=None, continuation=None):
    return LLMResponse(message=Message(role="assistant", content=text, tool_calls=calls,
                                       continuation=continuation),
                       finish_reason=reason or ("tool_calls" if calls else "stop"))


def call(id="c1", name="list_files", arguments='{"path":"."}'):
    return ToolCall(id=id, name=name, arguments=arguments)


class FakeModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.closed_streams = 0
        self.closed = False

    async def stream(self, request):
        self.requests.append(request)
        try:
            item = self.responses[len(self.requests) - 1]
            await asyncio.sleep(0)
            if isinstance(item, BaseException):
                raise item
            yield TextDelta(text=item.text)
            yield ResponseCompleted(response=item)
        finally:
            self.closed_streams += 1

    async def aclose(self):
        self.closed = True


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "README.md").write_text("MiniAgent example", encoding="utf-8")
        self.registry = create_tools()
        self.observed = []
        self.executor = ToolExecutor(self.registry, ToolContext(self.root), self.observed.append)
        self.messages = [Message(role="user", content="列目录，读取 README 并总结")]

    async def collect(self, model, **kwargs):
        return [e async for e in AgentLoop(model, self.executor).stream(self.messages, **kwargs)]

    async def test_two_tool_round_trips_preserve_history_and_state(self):
        state = ContinuationState(provider="fake", model="fake", payload='{"reasoning":"opaque"}')
        model = FakeModel(response("", (call(),), continuation=state),
                          response("", (call("c2", "read_file", '{"path":"README.md"}'),)),
                          response())
        events = await self.collect(model)
        self.assertEqual(events[-1].type, "agent_completed")
        self.assertEqual(len(model.requests), 3)
        self.assertEqual([len(r.messages) for r in model.requests], [1, 3, 5])
        self.assertIs(model.requests[2].messages[1].continuation, state)
        self.assertEqual(model.requests[1].messages[-1].tool_call_id, "c1")
        self.assertEqual(model.requests[2].messages[-1].tool_call_id, "c2")
        self.assertIn("MiniAgent example", model.requests[2].messages[-1].content)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(events[-1].messages[:-1], model.requests[-1].messages)
        self.assertEqual(events[-1].messages[-1].content, "总结完成")
        self.assertNotIn("MiniAgent example", repr(events[-1]))
        self.assertEqual([e.type for e in self.observed],
                         ["tool_started", "tool_completed"] * 2)
        adapter = TypeAdapter(LoopEvent)
        for event in events:
            self.assertEqual(adapter.validate_json(event.model_dump_json()), event)

    async def test_plain_text(self):
        model = FakeModel(response("你好"))
        events = await self.collect(model)
        self.assertEqual(events[-1].type, "agent_completed")
        self.assertEqual(model.closed_streams, 1)
        self.assertFalse(model.closed)  # Shared dependencies remain caller-owned.
        self.assertFalse(self.observed)

    async def test_failed_tool_returns_to_model(self):
        model = FakeModel(response("", (call(name="read_file", arguments='{"path":"missing"}'),)),
                          response("文件不存在"))
        events = await self.collect(model)
        self.assertIn("tool_failed", [e.type for e in events])
        self.assertFalse(json.loads(model.requests[1].messages[-1].content)["success"])
        self.assertEqual(events[-1].type, "agent_completed")

    async def test_multiple_calls_and_invalid_arguments(self):
        model = FakeModel(response("", (call(), call("c2", "read_file", "{"))), response())
        await self.collect(model)
        results = model.requests[1].messages[-2:]
        self.assertEqual([m.tool_call_id for m in results], ["c1", "c2"])
        self.assertFalse(json.loads(results[1].content)["success"])

    async def test_non_success_reasons_never_execute_tools(self):
        for reason in ("length", "refusal", "content_filter"):
            with self.subTest(reason=reason):
                model = FakeModel(response("", (call(),), reason=reason))
                events = await self.collect(model)
                self.assertEqual(events[-1].reason, reason)
                self.assertNotIn("agent_completed", [e.type for e in events])
        self.assertFalse(self.observed)

    async def test_duplicate_ids_rejected_before_execution(self):
        model = FakeModel(response("", (call(), call())))
        events = []
        with self.assertRaises(LLMError):
            async for event in AgentLoop(model, self.executor).stream(self.messages):
                events.append(event)
        self.assertEqual(events[-1].type, "agent_failed")
        self.assertFalse(self.observed)

    async def test_reused_id_across_rounds_is_rejected(self):
        model = FakeModel(response("", (call(),)), response("", (call(),)))
        with self.assertRaises(LLMError):
            await self.collect(model)
        self.assertEqual(len(self.observed), 2)

    async def test_observer_failure_does_not_break_stream_or_repeat_tool(self):
        def broken(event):
            raise RuntimeError("Display failed")
        self.executor.on_event = broken
        model = FakeModel(response("", (call(),)), response())
        events = await self.collect(model)
        self.assertEqual([e.type for e in events].count("tool_started"), 1)
        self.assertEqual([e.type for e in events].count("tool_completed"), 1)
        self.assertEqual(events[-1].type, "agent_completed")

    async def test_model_error_propagates(self):
        model = FakeModel(LLMError("connection", "Connection failed."))
        events = []
        with self.assertRaises(LLMError):
            async for event in AgentLoop(model, self.executor).stream(self.messages):
                events.append(event)
        self.assertEqual(events[-1].type, "agent_failed")
        self.assertEqual(model.closed_streams, 1)

    async def test_missing_final_response_does_not_execute(self):
        class Incomplete:
            async def stream(self, request):
                yield TextDelta(text="partial")
        with self.assertRaises(LLMError):
            await self.collect(Incomplete())
        self.assertFalse(self.observed)

    async def test_step_limit_prevents_unconsumable_tool_execution(self):
        model = FakeModel(response("", (call(),)))
        events = await self.collect(model, max_steps=1)
        self.assertEqual(events[-1].reason, "step_limit")
        self.assertFalse(self.observed)

    async def test_close_model_stream_before_response(self):
        model = FakeModel(response("", (call(),)))
        async with aclosing(AgentLoop(model, self.executor).stream(self.messages)) as stream:
            async for event in stream:
                if event.type == "llm_text_delta":
                    break
        self.assertEqual(model.closed_streams, 1)
        self.assertFalse(self.observed)

    async def test_close_and_cancel_running_tool(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                started, finished = asyncio.Event(), asyncio.Event()

                class BlockingTool:
                    definition = ToolDefinition(name="wait", description="wait",
                                                parameters={"type": "object"})

                    async def execute(self, arguments, context):
                        started.set()
                        try:
                            await asyncio.Event().wait()
                        finally:
                            finished.set()
                        return ToolResult(success=True)

                registry = create_tools()
                registry.register(BlockingTool())
                executor = ToolExecutor(registry, ToolContext(self.root))
                model = FakeModel(response("", (call(name="wait", arguments="{}"),)))
                stream = AgentLoop(model, executor).stream(self.messages)
                if not cancel:
                    async with aclosing(stream):
                        async for event in stream:
                            if event.type == "tool_started":
                                self.assertTrue(started.is_set())
                                self.assertFalse(finished.is_set())
                                break
                else:
                    events = []

                    async def consume():
                        async with aclosing(stream):
                            async for event in stream:
                                events.append(event)

                    task = asyncio.create_task(consume())
                    await asyncio.wait_for(started.wait(), 2)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertEqual(events[-1].type, "agent_cancelled")
                self.assertTrue(finished.is_set())
                self.assertEqual(len(model.requests), 1)

    async def test_concurrent_runs_keep_messages_and_callbacks_separate(self):
        other = self.root / "other"
        other.mkdir()
        (other / "README.md").write_text("Other user", encoding="utf-8")
        model_a = FakeModel(response("", (call(name="read_file", arguments='{"path":"README.md"}'),)),
                            response("A"))
        model_b = FakeModel(response("", (call(name="read_file", arguments='{"path":"README.md"}'),)),
                            response("B"))
        executor_b = ToolExecutor(self.registry, ToolContext(other))
        async def collect_b():
            return [e async for e in AgentLoop(model_b, executor_b).stream(
                [Message(role="user", content="B")])]
        a, b = await asyncio.gather(self.collect(model_a), collect_b())
        self.assertIn("MiniAgent example", model_a.requests[-1].messages[-1].content)
        self.assertNotIn("Other user", model_a.requests[-1].messages[-1].content)
        self.assertIn("Other user", model_b.requests[-1].messages[-1].content)
        self.assertEqual(a[-2].response.text, "A")
        self.assertEqual(b[-2].response.text, "B")
        self.assertEqual(len(self.observed), 2)


class AgentCliTests(unittest.TestCase):
    def test_interactive_cli_completes_task_and_shows_steps(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "README.md").write_text("CLI example", encoding="utf-8")
            model = FakeModel(response("", (call(),)),
                              response("", (call("c2", "read_file", '{"path":"README.md"}'),)),
                              response("这是示例项目。"))
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}), \
                 patch("builtins.input", side_effect=["列目录，读 README 并总结", "/exit"]) as prompt, \
                 patch("agent_cli.create_model", return_value=model), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                code = agent_cli.main(["--provider", "openai", "--model", "fake", "--workspace", root])
            self.assertEqual(code, 0)
            self.assertEqual(prompt.call_count, 2)
            self.assertEqual(stdout.getvalue().count("这是示例项目。"), 1)
            self.assertIn('[工具开始] list_files · "."', stderr.getvalue())
            self.assertIn('[工具成功] read_file · "README.md"', stderr.getvalue())
            self.assertNotIn(" · c1", stderr.getvalue())
            self.assertNotIn(" · c2", stderr.getvalue())
            for label in ("第 1 轮", "第 2 轮", "第 3 轮", "工具开始", "list_files",
                          "read_file", "工具成功", "任务完成"):
                self.assertIn(label, stderr.getvalue())
            self.assertTrue(model.closed)


    def test_multiturn_keeps_tool_history_and_continuation(self):
        with tempfile.TemporaryDirectory() as root:
            state = ContinuationState(provider="fake", model="fake", payload='{"state":1}')
            model = FakeModel(response("", (call(),), continuation=state),
                              response("目录已列出"), response("根据上轮目录继续回答"))
            with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}), \
                 patch("builtins.input", side_effect=["列目录", "继续解释", "/exit"]), \
                 patch("agent_cli.create_model", return_value=model) as factory, \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = agent_cli.main(["--provider", "openai", "--model", "fake", "--workspace", root])
            self.assertEqual(code, 0)
            factory.assert_called_once()
            history = model.requests[-1].messages
            self.assertEqual([m.role for m in history],
                             ["system", "user", "assistant", "tool", "assistant", "user"])
            self.assertEqual(history[-1].content, "继续解释")
            self.assertIs(history[2].continuation, state)
            self.assertEqual(history[3].tool_call_id, "c1")
            self.assertEqual(history[4].content, "目录已列出")
            self.assertTrue(model.closed)

    def test_interactive_empty_input_and_eof(self):
        with tempfile.TemporaryDirectory() as root:
            model = FakeModel(response("你好"))
            with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}), \
                 patch("builtins.input", side_effect=["  ", "你好", EOFError()]), \
                 patch("agent_cli.create_model", return_value=model), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = agent_cli.main(["--provider", "openai", "--model", "fake", "--workspace", root])
            self.assertEqual(code, 0)
            self.assertEqual(len(model.requests), 1)
            self.assertTrue(model.closed)

    def test_failed_turn_ends_session_without_reading_next_prompt(self):
        for item in (response("", (call(),)), LLMError("connection", "Failed.")):
            with self.subTest(item=item), tempfile.TemporaryDirectory() as root:
                model = FakeModel(item)
                stderr = io.StringIO()
                with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}), \
                     patch("builtins.input", side_effect=["任务", "不应读取"]) as prompt, \
                     patch("agent_cli.create_model", return_value=model), \
                     redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    code = agent_cli.main(["--provider", "openai", "--model", "fake",
                                           "--workspace", root, "--max-steps", "1"])
                self.assertEqual(code, 1)
                prompt.assert_called_once()
                self.assertIn("会话已结束", stderr.getvalue())
                self.assertTrue(model.closed)

    def test_prompt_option_remains_single_shot(self):
        with tempfile.TemporaryDirectory() as root:
            model = FakeModel(response("回答"))
            with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}), \
                 patch("builtins.input", side_effect=AssertionError("No interactive input")), \
                 patch("agent_cli.create_model", return_value=model), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = agent_cli.main(["--provider", "openai", "--model", "fake",
                                       "--workspace", root, "--prompt", "任务"])
            self.assertEqual(code, 0)
            self.assertEqual(len(model.requests), 1)


    def test_text_conversation_and_separate_sessions(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("甲", "乙"):
                model = FakeModel(response("记住了"), response(name))
                with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}), \
                     patch("builtins.input", side_effect=[name, "我是谁？", "/exit"]), \
                     patch("agent_cli.create_model", return_value=model), \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    code = agent_cli.main(["--provider", "openai", "--model", "fake", "--workspace", root])
                self.assertEqual(code, 0)
                self.assertEqual([m.content for m in model.requests[-1].messages[1:]],
                                 [name, "记住了", "我是谁？"])
                self.assertEqual(len(model.requests[0].messages), 2)

    def test_interrupt_at_input_closes_model(self):
        with tempfile.TemporaryDirectory() as root:
            model = FakeModel()
            with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}), \
                 patch("builtins.input", side_effect=KeyboardInterrupt()), \
                 patch("agent_cli.create_model", return_value=model), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = agent_cli.main(["--provider", "openai", "--model", "fake", "--workspace", root])
            self.assertEqual(code, 130)
            self.assertTrue(model.closed)
            self.assertFalse(model.requests)

    def test_non_success_exit_and_model_cleanup(self):
        for item in (response(reason="length"), LLMError("connection", "Failed.")):
            with self.subTest(item=item), tempfile.TemporaryDirectory() as root:
                model = FakeModel(item)
                with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}), \
                     patch("agent_cli.create_model", return_value=model), \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    code = agent_cli.main(["--provider", "openai", "--model", "fake",
                                           "--workspace", root, "--prompt", "test"])
                self.assertEqual(code, 1)
                self.assertTrue(model.closed)


if __name__ == "__main__":
    unittest.main()
