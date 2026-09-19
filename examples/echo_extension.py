"""A minimal extension example; echo does not invoke a shell."""
from miniagent.models import ToolDefinition
from miniagent.tools import ToolContent, ToolResult


class EchoTool:
    definition = ToolDefinition(
        name="echo", description="Return the supplied text unchanged.",
        parameters={"type": "object", "properties": {"text": {"type": "string"}},
                    "required": ["text"], "additionalProperties": False},
    )

    async def execute(self, arguments, context):
        return ToolResult(success=True, content=(ToolContent(type="text", value=arguments["text"]),))


def register(registry):
    registry.register(EchoTool())
