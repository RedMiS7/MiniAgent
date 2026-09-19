import asyncio
from contextlib import asynccontextmanager
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
from miniagent.agent import AgentHarness
from miniagent.extensions import brave_search, files
from miniagent.models import LLMResponse, Message, ResponseCompleted, ToolCall
from miniagent.tools import ToolContext, ToolExecutor, ToolRegistry
from miniagent.tools.executor import ToolApprovalError


class Model:
    def __init__(self, calls):
        self.calls = calls
        self.requests = []
        self.closed = 0

    async def stream(self, request):
        self.requests.append(request)
        i = len(self.requests) - 1
        message = Message(role="assistant", tool_calls=(self.calls[i],)) if i < len(self.calls) else Message(role="assistant", content="Done")
        yield ResponseCompleted(response=LLMResponse(
            message=message, finish_reason="tool_calls" if message.tool_calls else "stop"))

    async def aclose(self):
        self.closed += 1


class ApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = ToolRegistry()
        self.session = AsyncMock()
        self.session.list_tools.return_value = ListToolsResult(tools=[Tool(
            name="brave_web_search", inputSchema={"type":"object", "properties":{"query":{"type":"string"}}, "required":["query"]})])
        self.session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text="Found")])
        await brave_search.register(self.registry, self.session)
        self.executor = ToolExecutor(self.registry, ToolContext(self.temp.name))
        self.call = ToolCall(id="c1", name="brave_web_search", arguments='{"query":"MCP"}')
        self.model = Model([self.call])

    def harness(self, approve):
        return AgentHarness(self.model, self.executor, approval_required=("brave_web_search",), approve=approve)

    async def consume(self, harness):
        async with harness.run([Message(role="user", content="Search")]) as run:
            self.active = run
            self.events = []
            async for event in run.events():
                self.events.append(event)
        return run.result

    async def test_allow_calls_remote_once(self):
        approve = AsyncMock(return_value=True)
        result = await self.consume(self.harness(approve))
        self.assertEqual(result.status, "succeeded")
        approve.assert_awaited_once_with(self.call, {"query":"MCP"})
        self.session.call_tool.assert_awaited_once()

    async def test_denial_returns_result_and_next_call_requires_new_approval(self):
        self.model.calls.append(ToolCall(id="c2", name="brave_web_search", arguments=self.call.arguments))
        approve = AsyncMock(side_effect=[False, True])
        result = await self.consume(self.harness(approve))
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(approve.await_count, 2)
        self.assertEqual(self.session.call_tool.await_count, 1)
        denied = json.loads(self.model.requests[1].messages[-1].content)
        self.assertEqual(denied["error"]["code"], "approval_denied")

    async def test_approval_error_and_invalid_decision_stop_run(self):
        for callback in (AsyncMock(side_effect=ValueError("private")), AsyncMock(return_value="yes")):
            self.model = Model([self.call])
            with self.assertRaises(ToolApprovalError):
                await self.consume(self.harness(callback))
            self.assertEqual(self.active.result.reason, "execution_error")
            self.session.call_tool.assert_not_awaited()
            self.assertEqual(len(self.model.requests), 1)
            self.assertTrue(any(e.type == "tool_failed" and e.error_code == "approval_error" for e in self.events))

    async def test_cancel_waiting_approval_blocks_even_late_approval(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def approve(call, arguments):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                return True
        task = asyncio.create_task(self.consume(self.harness(approve)))
        await entered.wait()
        self.session.call_tool.assert_not_awaited()
        self.active.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.active.result.status, "cancelled")
        self.session.call_tool.assert_not_awaited()

    async def test_invalid_arguments_skip_approval(self):
        self.model.calls = [ToolCall(id="bad", name="brave_web_search", arguments="{}")]
        approve = AsyncMock(return_value=True)
        await self.consume(self.harness(approve))
        approve.assert_not_awaited()
        self.session.call_tool.assert_not_awaited()

    async def test_callback_cannot_modify_executed_arguments(self):
        async def approve(call, arguments):
            arguments["query"] = "changed"
            return True
        await self.consume(self.harness(approve))
        self.session.call_tool.assert_awaited_once_with("brave_web_search", arguments={"query":"MCP"})

    async def test_missing_callback_and_unknown_policy_fail_closed(self):
        with self.assertRaises(ValueError):
            self.harness(None)
        with self.assertRaises(ValueError):
            AgentHarness(self.model, self.executor, approval_required=("typo",), approve=AsyncMock())

    async def test_unprotected_tools_do_not_prompt(self):
        files.register(self.registry)
        self.model.calls = [ToolCall(id="f", name="list_files", arguments='{"path":"."}')]
        approve = AsyncMock(return_value=True)
        await self.consume(self.harness(approve))
        approve.assert_not_awaited()
        self.assertTrue(json.loads(self.model.requests[1].messages[-1].content)["success"])

    async def test_approval_does_not_bypass_existing_file_limits(self):
        files.register(self.registry)
        self.model.calls = [ToolCall(id="f", name="list_files", arguments='{"path":"../"}')]
        harness = AgentHarness(self.model, self.executor, approval_required=("list_files",), approve=AsyncMock(return_value=True))
        await self.consume(harness)
        self.assertEqual(json.loads(self.model.requests[1].messages[-1].content)["error"]["code"], "access_denied")

    async def test_cli_uses_harness_approval_and_closes_both_resources(self):
        import harness_cli
        closed = []
        @asynccontextmanager
        async def connect():
            try:
                yield self.registry
            finally:
                closed.append(True)
        with patch.object(harness_cli, "connect_brave_search", connect), \
                patch.object(harness_cli, "create_model", return_value=self.model), \
                patch.object(harness_cli, "read_decision", AsyncMock(return_value=False)), \
                patch("builtins.print"):
            self.assertEqual(await harness_cli.run(None, "Search"), 0)
        self.session.call_tool.assert_not_awaited()
        self.assertEqual(self.model.closed, 1)
        self.assertEqual(closed, [True])

    async def test_noninteractive_terminal_denies(self):
        import harness_cli
        with patch.object(harness_cli.sys.stdin, "isatty", return_value=False):
            self.assertFalse(await harness_cli.read_decision())

    async def test_early_scope_exit_cleans_approval_and_releases_harness(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def approve(call, arguments):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        harness = self.harness(approve)
        async with harness.run([Message(role="user", content="Search")]) as run:
            async for event in run.events():
                if event.type == "tool_started":
                    await entered.wait()
                    break
        self.assertTrue(cleaned.is_set())
        self.assertEqual(run.result.status, "cancelled")
        self.session.call_tool.assert_not_awaited()
        # Fake model's next response is text; reuse must be possible after cleanup.
        result = await self.consume(harness)
        self.assertEqual(result.status, "succeeded")

    async def test_harness_policy_does_not_mutate_shared_executor(self):
        await self.consume(self.harness(AsyncMock(return_value=False)))
        result = await self.executor.execute(self.call)
        self.assertTrue(result.success)
        self.session.call_tool.assert_awaited_once()

    async def test_windows_terminal_wait_is_cancellable(self):
        import harness_cli
        import types
        keyboard = types.SimpleNamespace(kbhit=lambda: False)
        with patch.object(harness_cli.sys.stdin, "isatty", return_value=True), \
                patch.object(harness_cli.sys, "platform", "win32"), \
                patch.dict("sys.modules", {"msvcrt": keyboard}):
            task = asyncio.create_task(harness_cli.read_decision())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
