from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from miniagent.models import Message, ToolDefinition


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ToolContext:
    workspace: Path
    allow_commands: bool = False
    max_file_bytes: int = 1_048_576
    max_output_chars: int = 16_384
    command_timeout: float = 30.0
    max_image_bytes: int = 20 * 1024 * 1024

    def __post_init__(self):
        root = Path(self.workspace).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Workspace must be a directory.")
        if self.max_file_bytes <= 0 or self.max_output_chars <= 0 or self.max_image_bytes <= 0:
            raise ValueError("Size limits must be positive.")
        import math
        if not math.isfinite(self.command_timeout) or self.command_timeout <= 0:
            raise ValueError("Timeout must be positive and finite.")
        object.__setattr__(self, "workspace", root)

    def path(self, name: str) -> Path:
        candidate = Path(name)
        if candidate.is_absolute() or candidate.drive or ":" in name:
            raise ToolError("access_denied", "Use a workspace-relative path.")
        resolved = (self.workspace / candidate).resolve()
        if not resolved.is_relative_to(self.workspace):
            raise ToolError("access_denied", "Path leaves the workspace.")
        return resolved


@dataclass(frozen=True)
class ToolContent:
    type: Literal["text", "json"]
    value: Any = field(repr=False)


@dataclass(frozen=True)
class ToolResult:
    success: bool
    content: tuple[ToolContent, ...] = ()
    error_code: str | None = None
    error_message: str | None = None

    def to_message(self, call_id: str) -> Message:
        import json
        payload = {
            "success": self.success,
            "content": [{"type": item.type, "value": item.value} for item in self.content],
        }
        if not self.success:
            payload["error"] = {"code": self.error_code, "message": self.error_message}
        return Message("tool", json.dumps(payload, ensure_ascii=False), tool_call_id=call_id)


class Tool(Protocol):
    definition: ToolDefinition

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        ...
