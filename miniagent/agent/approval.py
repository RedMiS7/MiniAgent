"""Harness-owned per-call approval policy, separate from tool implementations."""
import asyncio
from contextlib import suppress
from typing import Awaitable, Callable

from miniagent.models import ToolCall
from miniagent.tools import ToolExecutor

ApprovalCallback = Callable[[ToolCall, dict], Awaitable[bool]]


class ApprovalExecutor:
    def __init__(self, executor: ToolExecutor, required: frozenset[str], approve: ApprovalCallback):
        self.registry = executor.registry
        self._executor = executor
        self._required = required
        self._approve = approve

    async def _request(self, call, arguments):
        task = asyncio.ensure_future(self._approve(call, arguments))
        try:
            # Preserve cancellation even if the callback swallows its own cancellation.
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
            raise

    async def execute(self, call, *, on_event=None):
        if call.name not in self._required:
            return await self._executor.execute(call, on_event=on_event)
        return await self._executor.execute(call, on_event=on_event, approve=self._request)
