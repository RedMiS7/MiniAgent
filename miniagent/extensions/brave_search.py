"""Expose only Brave web search from a caller-owned MCP session."""
from mcp import ClientSession
from mcp.types import TextContent

from miniagent.models import ToolDefinition
from miniagent.tools import ToolContent, ToolError, ToolResult

TOOL_NAME = "brave_web_search"


class BraveSearchTool:
    def __init__(self, session: ClientSession, definition: ToolDefinition):
        self.definition = definition
        self._session = session

    async def execute(self, arguments, context):
        result = await self._session.call_tool(TOOL_NAME, arguments=arguments)
        if any(not isinstance(item, TextContent) for item in result.content):
            raise ToolError("unsupported_content", "Brave returned unsupported non-text content.")
        content = [ToolContent(type="text", value=item.text) for item in result.content]
        if result.structuredContent is not None:
            content.append(ToolContent(type="json", value=result.structuredContent))
        return ToolResult(
            success=not result.isError, content=tuple(content),
            error_code="mcp_tool_error" if result.isError else None,
            error_message="Brave search failed." if result.isError else None,
        )


async def register(registry, session: ClientSession):
    """Discover the remote schema; do not import unrelated server tools."""
    cursor = None
    seen = set()
    while True:
        page = await session.list_tools(cursor=cursor)
        for tool in page.tools:
            if tool.name == TOOL_NAME:
                registry.register(BraveSearchTool(session, ToolDefinition(
                    name=tool.name, description=tool.description or "Search the web with Brave.",
                    parameters=tool.inputSchema,
                )))
                return
        cursor = page.nextCursor
        if cursor is None:
            raise ValueError("Brave MCP server does not expose brave_web_search.")
        if cursor in seen:
            raise ValueError("Brave MCP server repeated a tools cursor.")
        seen.add(cursor)
