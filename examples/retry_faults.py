"""Explicit CLI fault injection; never installed automatically in production."""
from dataclasses import dataclass
import json

from examples.echo_extension import EchoTool
from miniagent.tools import ToolResult
from miniagent.models import LLMError, LLMResponse, Message, ResponseCompleted, TextDelta, ToolCall

ERRORS = ("timeout", "connection", "rate_limit", "server", "authentication", "permission", "invalid_request")


@dataclass(frozen=True)
class FaultPlan:
    error: str
    count: int = 1
    step: int = 1
    after_partial: bool = False

    def __post_init__(self):
        if self.error not in ERRORS:
            raise ValueError("Unsupported injected error.")
        if type(self.count) is not int or self.count < 1 or type(self.step) is not int or self.step < 1:
            raise ValueError("Fault count and step must be positive integers.")


class FaultInjectingModel:
    def __init__(self, model, plan, report):
        self._model = model
        self._plan = plan
        self._report = report
        self._remaining = plan.count
        self._completed = 0

    async def stream(self, request):
        if self._remaining and self._completed + 1 == self._plan.step:
            self._remaining -= 1
            self._report(f"[故障注入] step={self._plan.step} error={self._plan.error} remaining={self._remaining}；本次未调用 API")
            if self._plan.after_partial:
                yield TextDelta(text="[模拟的部分输出]")
            raise LLMError(self._plan.error, "Injected model failure.",
                           self._plan.error in ("timeout", "connection", "rate_limit", "server"))
        events = self._model.stream(request)
        primary = None
        try:
            async for event in events:
                yield event
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                await events.aclose()
            except BaseException as cleanup:
                if primary is not None:
                    raise primary from cleanup
                raise
        self._completed += 1

    async def aclose(self):
        await self._model.aclose()


class OfflineModel:
    """Echo once, then consume its result. No network or secrets."""
    async def stream(self, request):
        if request.messages[-1].role == "tool" and json.loads(request.messages[-1].content)["success"]:
            message = Message(role="assistant", content="离线任务完成，已收到 echo 结果。")
            finish = "stop"
        else:
            attempt = sum(m.role == "tool" for m in request.messages)
            message = Message(role="assistant", tool_calls=(ToolCall(
                id=f"offline-echo-{attempt}", name="echo", arguments='{"text":"retry demo"}'),))
            finish = "tool_calls"
        yield ResponseCompleted(response=LLMResponse(message=message, finish_reason=finish))

    async def aclose(self):
        pass


class FaultyEchoTool(EchoTool):
    """Fail before execution; a new call requires a fresh model decision."""
    def __init__(self, failures):
        self._remaining = failures

    async def execute(self, arguments, context):
        if self._remaining:
            self._remaining -= 1
            return ToolResult(success=False, error_code="injected_tool_error",
                              error_message="Simulated tool failure before execution.")
        return await super().execute(arguments, context)
