import asyncio
import json
import time
from copy import deepcopy
from typing import Awaitable, Callable

from miniagent.models import ToolCall
from .base import ToolContext, ToolError, ToolResult
from .events import ToolCancelled, ToolCompleted, ToolEvent, ToolFailed, ToolStarted
from .registry import ToolRegistry


class ToolApprovalError(RuntimeError):
    """Approval infrastructure failed; the Run must stop without executing."""


class ToolExecutor:
    def __init__(
        self, registry: ToolRegistry, context: ToolContext,
        on_event: Callable[[ToolEvent], None] | None = None,
    ):
        self.registry = registry
        self.context = context
        self.on_event = on_event

    def _emit(self, event: ToolEvent, on_event=None):
        for callback in (self.on_event, on_event):
            if callback is None:
                continue
            try:
                callback(event)
            except Exception:
                # A display failure must not turn a completed write into a retry.
                pass

    async def execute(
        self, call: ToolCall, *, on_event: Callable[[ToolEvent], None] | None = None,
        approve: Callable[[ToolCall, dict], Awaitable[bool]] | None = None,
    ) -> ToolResult:
        start = time.monotonic()
        self._emit(ToolStarted(call_id=call.id, tool_name=call.name), on_event)
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
            if approve is not None:
                try:
                    approved = await approve(call.model_copy(deep=True), deepcopy(arguments))
                    if type(approved) is not bool:
                        raise TypeError("Approval must return a boolean.")
                except Exception as exc:
                    raise ToolApprovalError("Tool approval failed.") from exc
                if not approved:
                    raise ToolError("approval_denied", "The user denied this tool call.")
            result = await tool.execute(arguments, self.context)
        except asyncio.CancelledError:
            self._emit(ToolCancelled(
                call_id=call.id, tool_name=call.name, elapsed_seconds=time.monotonic() - start,
            ), on_event)
            raise
        except ToolApprovalError:
            self._emit(ToolFailed(
                call_id=call.id, tool_name=call.name, elapsed_seconds=time.monotonic() - start,
                error_code="approval_error",
            ), on_event)
            raise
        except ToolError as exc:
            result = ToolResult(success=False, error_code=exc.code, error_message=str(exc))
        except (OSError, UnicodeError):
            result = ToolResult(success=False, error_code="io_error", error_message="Tool I/O operation failed.")
        except Exception:
            result = ToolResult(success=False, error_code="execution_error", error_message="Tool execution failed.")
        if result.success:
            self._emit(ToolCompleted(
                call_id=call.id, tool_name=call.name, elapsed_seconds=time.monotonic() - start,
            ), on_event)
        else:
            self._emit(ToolFailed(
                call_id=call.id, tool_name=call.name, elapsed_seconds=time.monotonic() - start,
                error_code=result.error_code,
            ), on_event)
        return result

    @staticmethod
    def _reject_constant(value):
        raise ValueError("Non-JSON number")
