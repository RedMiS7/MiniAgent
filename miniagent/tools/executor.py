import asyncio
import json
import time
from dataclasses import dataclass
from typing import Callable, Literal

from miniagent.models import ToolCall
from .base import ToolContext, ToolError, ToolResult
from .registry import ToolRegistry


@dataclass(frozen=True)
class ToolEvent:
    type: Literal["started", "completed", "failed", "cancelled"]
    call_id: str
    tool_name: str
    elapsed_seconds: float = 0.0
    error_code: str | None = None


class ToolExecutor:
    def __init__(
        self, registry: ToolRegistry, context: ToolContext,
        on_event: Callable[[ToolEvent], None] | None = None,
    ):
        self.registry = registry
        self.context = context
        self.on_event = on_event

    def _emit(self, event):
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                # A display failure must not turn a completed write into a retry.
                pass

    async def execute(self, call: ToolCall) -> ToolResult:
        start = time.monotonic()
        self._emit(ToolEvent("started", call.id, call.name))
        try:
            if len(call.arguments.encode("utf-8")) > self.context.max_file_bytes * 8 + 65536:
                raise ToolError("invalid_arguments", "Arguments exceed the size limit.")
            tool = self.registry.resolve(call.name)
            try:
                arguments = json.loads(call.arguments, parse_constant=self._reject_constant)
            except (ValueError, RecursionError):
                raise ToolError("invalid_arguments", "Arguments must be valid JSON.") from None
            if not isinstance(arguments, dict):
                raise ToolError("invalid_arguments", "Arguments must be an object.")
            self.registry.validate(call.name, arguments)
            result = await tool.execute(arguments, self.context)
        except asyncio.CancelledError:
            self._emit(ToolEvent("cancelled", call.id, call.name, time.monotonic() - start))
            raise
        except ToolError as exc:
            result = ToolResult(False, error_code=exc.code, error_message=str(exc))
        except (OSError, UnicodeError):
            result = ToolResult(False, error_code="io_error", error_message="Tool I/O operation failed.")
        except Exception:
            result = ToolResult(False, error_code="execution_error", error_message="Tool execution failed.")
        self._emit(ToolEvent(
            "completed" if result.success else "failed", call.id, call.name,
            time.monotonic() - start, result.error_code,
        ))
        return result

    @staticmethod
    def _reject_constant(value):
        raise ValueError("Non-JSON number")
