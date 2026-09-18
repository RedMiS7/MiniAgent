import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import os
import signal
import tempfile
import unittest
from unittest.mock import patch

import runtime_cli
from miniagent.models import LLMError, LLMResponse, Message, ResponseCompleted, TextDelta, ToolCall


class FakeModel:
    def __init__(self, *items, on_request=None, close_error=False):
        self.items = items
        self.on_request = on_request
        self.close_error = close_error
        self.requests = []
        self.closed_streams = 0
        self.closed = 0

    async def stream(self, request):
        self.requests.append(request)
        try:
            if self.on_request is not None:
                self.on_request()
                await asyncio.Event().wait()
            item = self.items[len(self.requests) - 1]
            if isinstance(item, Exception):
                raise item
            yield TextDelta(text=item.text)
            yield ResponseCompleted(response=item)
        finally:
            self.closed_streams += 1

    async def aclose(self):
        self.closed += 1
        if self.close_error:
            raise RuntimeError("private-cleanup")


def answer(text="Hello", calls=()):
    return LLMResponse(message=Message(role="assistant", content=text, tool_calls=calls),
                       finish_reason="tool_calls" if calls else "stop")


class RuntimeCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.argv = ["--provider", "deepseek", "--model", "fake-model",
                     "--workspace", self.temp.name, "--prompt", "Test"]

    def invoke(self, model, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}), \
                patch.object(runtime_cli, "create_model", return_value=model) as factory, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = runtime_cli.main(self.argv + list(extra))
        return code, stdout.getvalue(), stderr.getvalue(), factory

    def test_success_shows_events_summary_and_closes_model(self):
        model = FakeModel(answer())
        code, stdout, stderr, factory = self.invoke(model)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.count("Hello"), 1)
        self.assertIn("[Agent] 开始任务", stderr)
        self.assertIn("[Agent] 任务完成", stderr)
        self.assertIn("[Run结果] succeeded · stop", stderr)
        self.assertIn("[模型资源] 已关闭", stderr)
        self.assertEqual(model.closed_streams, 1)
        self.assertEqual(model.closed, 1)
        self.assertEqual(factory.call_args.args[0].max_retries, 0)

    def test_tool_round_trip_reaches_runtime_success(self):
        call = ToolCall(id="c", name="list_files", arguments='{"path":"."}')
        model = FakeModel(answer("", (call,)), answer("Done"))
        code, stdout, stderr, _ = self.invoke(model)
        self.assertEqual(code, 0)
        self.assertIn("[工具成功] list_files", stderr)
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(model.requests[1].messages[-1].tool_call_id, "c")
        self.assertEqual(model.closed, 1)

    def test_step_limit_is_failure_without_tool_execution(self):
        call = ToolCall(id="c", name="list_files", arguments='{"path":"."}')
        model = FakeModel(answer("", (call,)))
        code, _, stderr, _ = self.invoke(model, "--max-steps", "1")
        self.assertEqual(code, 1)
        self.assertIn("[Run结果] failed · step_limit", stderr)
        self.assertNotIn("[工具开始]", stderr)
        self.assertEqual(model.closed, 1)

    def test_model_error_does_not_print_raw_diagnostics(self):
        model = FakeModel(LLMError("authentication", "private-response"))
        code, _, stderr, _ = self.invoke(model)
        self.assertEqual(code, 1)
        self.assertIn("[模型错误] authentication", stderr)
        self.assertIn("[Run结果] failed · model_error", stderr)
        self.assertNotIn("private-response", stderr)
        self.assertEqual(model.closed, 1)

    def test_framework_error_is_reported_without_raw_message(self):
        model = FakeModel(RuntimeError("private-input"))
        code, _, stderr, _ = self.invoke(model)
        self.assertEqual(code, 1)
        self.assertIn("[Run结果] failed · execution_error", stderr)
        self.assertNotIn("private-input", stderr)
        self.assertEqual(model.closed, 1)

    def test_timed_cancel_consumes_terminal_and_closes_model(self):
        model = FakeModel(on_request=lambda: None)
        code, _, stderr, _ = self.invoke(model, "--cancel-after", "0.01")
        self.assertEqual(code, 130)
        self.assertIn("[Agent] 已取消", stderr)
        self.assertIn("[Run结果] cancelled · cancelled", stderr)
        self.assertEqual(model.closed_streams, len(model.requests))
        self.assertEqual(model.closed, 1)

    def test_ctrl_c_requests_runtime_cancel_and_restores_handler(self):
        previous = signal.getsignal(signal.SIGINT)
        installed = []
        model = FakeModel(on_request=lambda: installed[0][1](signal.SIGINT, None))
        with patch.object(runtime_cli.signal, "signal",
                          side_effect=lambda signum, handler: installed.append((signum, handler))):
            code, _, stderr, _ = self.invoke(model)
        self.assertEqual(code, 130)
        self.assertIn("[Agent] 已取消", stderr)
        self.assertEqual(installed[-1], (signal.SIGINT, previous))
        self.assertEqual(model.closed_streams, 1)
        self.assertEqual(model.closed, 1)

    def test_cancel_timer_is_removed_after_fast_success(self):
        model = FakeModel(answer())
        code, _, stderr, _ = self.invoke(model, "--cancel-after", "60")
        self.assertEqual(code, 0)
        self.assertIn("[Run结果] succeeded · stop", stderr)
        self.assertEqual(model.closed, 1)

    def test_model_close_failure_returns_failure(self):
        model = FakeModel(answer(), close_error=True)
        code, _, stderr, _ = self.invoke(model)
        self.assertEqual(code, 1)
        self.assertIn("[模型资源] 关闭失败", stderr)
        self.assertNotIn("private-cleanup", stderr)

    def test_invalid_arguments_do_not_create_model(self):
        for extra in (["--prompt", " "], ["--max-steps", "0"], ["--cancel-after", "nan"],
                      ["--cancel-after", "0"], ["--cancel-after", "-1"], ["--timeout", "0"]):
            with self.subTest(extra=extra):
                code, _, _, factory = self.invoke(FakeModel(), *extra)
                self.assertEqual(code, 2)
                factory.assert_not_called()

    def test_missing_key_is_configuration_error(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(runtime_cli, "create_model") as factory, \
                redirect_stderr(io.StringIO()) as stderr:
            code = runtime_cli.main(self.argv)
        self.assertEqual(code, 2)
        self.assertIn("DEEPSEEK_API_KEY", stderr.getvalue())
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
