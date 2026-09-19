import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from mcp.types import CallToolResult, ImageContent, ListToolsResult, TextContent, Tool

from miniagent.agent import AgentHarness
from miniagent.extensions import brave_search
from miniagent.models import LLMResponse, Message, ResponseCompleted, ToolCall
from miniagent.tools import ToolContext, ToolExecutor, ToolRegistry


def remote_tool(name="brave_web_search"):
    return Tool(name=name, description="Search", inputSchema={
        "type": "object", "properties": {"query": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["query"], "additionalProperties": False,
    })


class BraveSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.context = ToolContext(self.temp.name)
        self.registry = ToolRegistry()
        self.session = AsyncMock()
        self.session.list_tools.return_value = ListToolsResult(tools=[remote_tool(), remote_tool("other")])
        self.session.call_tool.return_value = CallToolResult(
            content=[TextContent(type="text", text="Found")], structuredContent={"url": "https://example.org"})
        self.call = ToolCall(id="c1", name="brave_web_search", arguments='{"query":"MCP"}')

    async def execute(self, call=None):
        await brave_search.register(self.registry, self.session)
        return await ToolExecutor(self.registry, self.context).execute(call or self.call)

    async def test_register_only_search_and_preserve_remote_schema(self):
        result = await self.execute()
        self.assertEqual([d.name for d in self.registry.definitions()], ["brave_web_search"])
        self.assertEqual(self.registry.definitions()[0].parameters, remote_tool().inputSchema)
        self.assertTrue(result.success)
        self.assertEqual(result.content[0].value, "Found")
        self.assertEqual(result.content[1].value, {"url": "https://example.org"})
        self.session.call_tool.assert_awaited_once_with("brave_web_search", arguments={"query": "MCP"})

    async def test_invalid_arguments_never_call_server(self):
        result = await self.execute(ToolCall(id="c", name="brave_web_search", arguments="{}"))
        self.assertEqual(result.error_code, "invalid_arguments")
        self.session.call_tool.assert_not_awaited()

    async def test_remote_error_is_not_success(self):
        self.session.call_tool.return_value = CallToolResult(
            isError=True, content=[TextContent(type="text", text="Search unavailable")])
        result = await self.execute()
        self.assertEqual(result.error_code, "mcp_tool_error")
        self.assertEqual(result.content[0].value, "Search unavailable")

    async def test_transport_failure_uses_existing_executor_error(self):
        self.session.call_tool.side_effect = RuntimeError("private diagnostic")
        result = await self.execute()
        self.assertEqual(result.error_code, "execution_error")
        self.assertNotIn("private", result.model_dump_json())

    async def test_unsupported_content_is_explicit(self):
        self.session.call_tool.return_value = CallToolResult(
            content=[ImageContent(type="image", data="AA==", mimeType="image/png")])
        result = await self.execute()
        self.assertEqual(result.error_code, "unsupported_content")

    async def test_cancel_propagates(self):
        self.session.call_tool.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.execute()

    async def test_discovery_pagination(self):
        self.session.list_tools.side_effect = [
            ListToolsResult(tools=[], nextCursor="next"), ListToolsResult(tools=[remote_tool()])]
        await brave_search.register(self.registry, self.session)
        self.assertEqual(self.session.list_tools.await_args_list[-1].kwargs, {"cursor": "next"})

    async def test_missing_tool_and_repeated_cursor_rejected(self):
        for page in (ListToolsResult(tools=[]), ListToolsResult(tools=[], nextCursor="loop")):
            self.session.list_tools.return_value = page
            with self.assertRaises(ValueError):
                await brave_search.register(self.registry, self.session)
            self.assertEqual(self.registry.definitions(), ())

    async def test_duplicate_registration_uses_existing_registry_guard(self):
        await brave_search.register(self.registry, self.session)
        with self.assertRaises(ValueError):
            await brave_search.register(self.registry, self.session)

    async def test_harness_model_tool_round_trip_without_core_changes(self):
        requests = []
        call = self.call

        class Model:
            async def stream(self, request):
                requests.append(request)
                message = Message(role="assistant", tool_calls=(call,)) if len(requests) == 1 else Message(role="assistant", content="Done")
                yield ResponseCompleted(response=LLMResponse(
                    message=message, finish_reason="tool_calls" if len(requests) == 1 else "stop"))

        await brave_search.register(self.registry, self.session)
        async with AgentHarness(Model(), ToolExecutor(self.registry, self.context)) as harness:
            async with harness.run([Message(role="user", content="Search MCP")]) as run:
                events = [event async for event in run.events()]
        self.assertEqual(run.result.status, "succeeded")
        self.assertEqual(requests[1].messages[-1].tool_call_id, "c1")
        self.assertIn("Found", requests[1].messages[-1].content)
        self.assertEqual(self.session.call_tool.await_count, 1)
        self.assertTrue(any(event.type == "tool_completed" for event in events))

    async def test_missing_key_rejected_before_process_start(self):
        from miniagent.bootstrap import connect_brave_search
        with patch.dict("os.environ", {"BRAVE_API_KEY": ""}), patch("mcp.client.stdio.stdio_client") as start:
            with self.assertRaises(ValueError):
                async with connect_brave_search():
                    pass
            start.assert_not_called()

    async def test_connection_closes_session_and_transport_on_body_failure(self):
        from contextlib import asynccontextmanager
        from miniagent.bootstrap import connect_brave_search
        closed = []

        @asynccontextmanager
        async def transport(parameters):
            self.assertEqual(parameters.env, {"BRAVE_API_KEY": "test-only"})
            self.assertIn("@brave/brave-search-mcp-server@2.1.4", parameters.args)
            try:
                yield ("read", "write")
            finally:
                closed.append("transport")

        @asynccontextmanager
        async def session_scope(*args, **kwargs):
            try:
                yield self.session
            finally:
                closed.append("session")

        with patch.dict("os.environ", {"BRAVE_API_KEY": "test-only"}), \
                patch("shutil.which", return_value="npx"), \
                patch("mcp.client.stdio.stdio_client", transport), \
                patch("mcp.ClientSession", session_scope):
            with self.assertRaisesRegex(RuntimeError, "body"):
                async with connect_brave_search() as registry:
                    self.assertEqual(len(registry.definitions()), 1)
                    raise RuntimeError("body")
        self.assertEqual(closed, ["session", "transport"])
        self.session.initialize.assert_awaited_once()

    async def test_cli_discovery_never_searches_and_explicit_query_does(self):
        from contextlib import asynccontextmanager
        import brave_mcp_cli
        await brave_search.register(self.registry, self.session)

        @asynccontextmanager
        async def connect():
            yield self.registry

        with patch.object(brave_mcp_cli, "connect_brave_search", connect), patch("builtins.print"):
            self.assertEqual(await brave_mcp_cli.run(None), 0)
            self.session.call_tool.assert_not_awaited()
            self.assertEqual(await brave_mcp_cli.run("MCP"), 0)
            self.session.call_tool.assert_awaited_once_with("brave_web_search", arguments={"query": "MCP", "count": 3})
