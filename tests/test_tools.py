import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from miniagent.bootstrap import create_tools
from miniagent.models import ToolCall, ToolDefinition
from miniagent.tools import ToolContent, ToolContext, ToolExecutor, ToolRegistry, ToolResult


class ToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = []
        self.registry = create_tools()
        self.executor = ToolExecutor(self.registry, ToolContext(self.root), self.events.append)

    async def call(self, name, **args):
        return await self.executor.execute(ToolCall("c", name, json.dumps(args)))

    async def test_registered_tools(self):
        self.assertEqual({d.name for d in self.registry.definitions()}, {
            "list_files", "read_file", "write_file", "edit_file", "run_command", "image",
        })
        with self.assertRaises(ValueError):
            self.registry.register(self.registry.resolve("read_file"))

    async def test_validation_and_unknown_tool_do_not_execute(self):
        for name, args, code in [
            ("missing", "{}", "unknown_tool"),
            ("write_file", "{", "invalid_arguments"),
            ("write_file", '{"path":"file"}', "invalid_arguments"),
            ("read_file", '{"path":".","extra":1}', "invalid_arguments"),
            ("read_file", '{"path":".","start_line":true}', "invalid_arguments"),
        ]:
            result = await self.executor.execute(ToolCall("c", name, args))
            self.assertFalse(result.success)
            self.assertEqual(result.error_code, code)
        self.assertFalse((self.root / "file").exists())

    async def test_create_read_edit_and_diff(self):
        result = await self.call("write_file", path="sample.txt", content="hello\n")
        self.assertTrue(result.success)
        self.assertIn("+hello", result.content[0].value["diff"])
        result = await self.call("read_file", path="sample.txt")
        digest = result.content[0].value["sha256"]
        self.assertEqual(result.content[0].value["text"], "hello\n")
        result = await self.call("edit_file", path="sample.txt", old_text="hello",
                                 new_text="world", expected_sha256=digest)
        self.assertTrue(result.success)
        self.assertEqual((self.root / "sample.txt").read_text(), "world\n")
        self.assertEqual(self.events[-1].type, "completed")
        self.assertEqual(result.to_message("c").tool_call_id, "c")

    async def test_overwrite_and_stale_hash_rejected(self):
        (self.root / "sample").write_text("new")
        result = await self.call("write_file", path="sample", content="replace")
        self.assertEqual(result.error_code, "already_exists")
        result = await self.call("edit_file", path="sample", old_text="new",
                                 new_text="replace", expected_sha256="0" * 64)
        self.assertEqual(result.error_code, "conflict")
        self.assertEqual((self.root / "sample").read_text(), "new")

    async def test_ambiguous_edit_rejected(self):
        (self.root / "sample").write_bytes(b"x x")
        result = await self.call("edit_file", path="sample", old_text="x",
                                 new_text="y", expected_sha256=hashlib.sha256(b"x x").hexdigest())
        self.assertEqual(result.error_code, "conflict")

    async def test_path_escape_and_absolute_rejected(self):
        for path in ("../outside", str(self.root / "sample"), "file:stream"):
            result = await self.call("write_file", path=path, content="x")
            self.assertEqual(result.error_code, "access_denied")

    async def test_symlink_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            try:
                (self.root / "link").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Symlink privilege unavailable")
            result = await self.call("write_file", path="link/outside", content="x")
            self.assertEqual(result.error_code, "access_denied")

    async def test_size_limits(self):
        self.executor.context = ToolContext(self.root, max_file_bytes=8, max_output_chars=4)
        result = await self.call("write_file", path="large", content="012345678")
        self.assertEqual(result.error_code, "file_too_large")
        (self.root / "small").write_text("123456")
        result = await self.call("read_file", path="small")
        self.assertEqual(result.content[0].value["text"], "1234")
        self.assertTrue(result.content[0].value["truncated"])

    async def test_command_disabled(self):
        result = await self.call("run_command", argv=[sys.executable, "-c", "print(1)"])
        self.assertEqual(result.error_code, "access_denied")

    async def test_command_output_limits_and_exit(self):
        self.executor.context = ToolContext(self.root, allow_commands=True, max_output_chars=100)
        result = await self.call("run_command", argv=[
            sys.executable, "-c", "import sys; print('x'*10000); print('error',file=sys.stderr); sys.exit(2)",
        ])
        self.assertFalse(result.success)
        data = result.content[0].value
        self.assertEqual(data["exit_code"], 2)
        self.assertLessEqual(len(data["stdout"]), 100)
        self.assertTrue(data["truncated"])
        self.assertIn("error", data["stderr"])

    async def test_command_timeout(self):
        self.executor.context = ToolContext(self.root, allow_commands=True, command_timeout=0.2)
        result = await asyncio.wait_for(self.call(
            "run_command", argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        ), timeout=5)
        self.assertEqual(result.error_code, "timeout")

    async def test_command_cancel(self):
        self.executor.context = ToolContext(self.root, allow_commands=True)
        task = asyncio.create_task(self.call(
            "run_command", argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        ))
        await asyncio.sleep(0.2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        self.assertEqual(self.events[-1].type, "cancelled")

    async def test_extension_without_executor_change(self):
        class Echo:
            definition = ToolDefinition("echo", "Echo JSON.", {
                "type": "object", "properties": {"text": {"type": "string"}},
                "required": ["text"], "additionalProperties": False,
            })

            async def execute(self, args, context):
                return ToolResult(True, (ToolContent("text", args["text"]),))
        def register(registry):
            registry.register(Echo())
        register(self.registry)
        result = await self.call("echo", text="private")
        self.assertTrue(result.success)
        self.assertEqual(result.content[0].value, "private")
        self.assertNotIn("private", repr(self.events))
        self.assertNotIn("private", repr(result))

    async def test_executor_event_lifecycle(self):
        for outcome, terminal, code in [
            (ToolResult(True), "completed", None),
            (ToolResult(False, error_code="rejected", error_message="Rejected."), "failed", "rejected"),
            (RuntimeError("private detail"), "failed", "execution_error"),
            (asyncio.CancelledError(), "cancelled", None),
        ]:
            with self.subTest(terminal=terminal, code=code):
                class Example:
                    definition = ToolDefinition("example", "Test tool.", {"type": "object"})

                    async def execute(self, args, context):
                        if isinstance(outcome, BaseException):
                            raise outcome
                        return outcome

                registry = ToolRegistry()
                registry.register(Example())
                events = []
                executor = ToolExecutor(registry, ToolContext(self.root), events.append)
                call = ToolCall("example-call", "example", "{}")
                if terminal == "cancelled":
                    with self.assertRaises(asyncio.CancelledError):
                        await executor.execute(call)
                else:
                    result = await executor.execute(call)
                    self.assertEqual(result.success, terminal == "completed")
                self.assertEqual([event.type for event in events], ["started", terminal])
                self.assertTrue(all(event.call_id == call.id for event in events))
                self.assertTrue(all(event.tool_name == call.name for event in events))
                self.assertEqual(events[-1].error_code, code)
                self.assertGreaterEqual(events[-1].elapsed_seconds, 0)
                self.assertNotIn("private detail", repr(events))

    async def test_event_observer_failure_does_not_repeat_write(self):
        def broken(event):
            raise RuntimeError("display unavailable")
        self.executor.on_event = broken
        result = await self.call("write_file", path="one", content="once")
        self.assertTrue(result.success)
        self.assertEqual((self.root / "one").read_text(), "once")


if __name__ == "__main__":
    unittest.main()
