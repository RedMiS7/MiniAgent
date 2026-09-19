"""Offline usage example: python -m examples.harness_reuse (no API keys)."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from miniagent.agent import AgentHarness
from miniagent.extensions.files import ListFilesTool, ReadFileTool
from miniagent.models import LLMResponse, Message, ResponseCompleted, ToolCall
from miniagent.tools import ToolContext, ToolExecutor, ToolRegistry
from . import echo_extension

TASKS = ("列出目录，读取 note.txt 并总结。", "用 echo 返回 Hello Harness。")
CALLS = (
    ToolCall(id="list", name="list_files", arguments='{"path":"."}'),
    ToolCall(id="read", name="read_file", arguments='{"path":"note.txt"}'),
    ToolCall(id="echo", name="echo", arguments='{"text":"Hello Harness"}'),
)


def create_registry():
    registry = ToolRegistry()
    registry.register(ListFilesTool())
    registry.register(ReadFileTool())
    echo_extension.register(registry)
    return registry


async def run_tasks(model, workspace, *, on_event=None):
    """Borrow a model and run two independent tasks on one Harness instance."""
    executor = ToolExecutor(create_registry(), ToolContext(workspace))
    results = []
    async with AgentHarness(model, executor, on_event=on_event) as harness:
        for prompt in TASKS:
            async with harness.run([Message(role="user", content=prompt)]) as run:
                async for _ in run.events():
                    pass
            results.append(run.result)
    return results


class ScriptedModel:
    """Fixed responses illustrate orchestration, not model reasoning quality."""
    def __init__(self):
        self.responses = iter((CALLS[0], CALLS[1], "文档介绍可复用 Agent。", CALLS[2], "Hello Harness"))

    async def stream(self, request):
        item = next(self.responses)
        message = (Message(role="assistant", tool_calls=(item,)) if isinstance(item, ToolCall)
                   else Message(role="assistant", content=item))
        yield ResponseCompleted(response=LLMResponse(
            message=message, finish_reason="tool_calls" if message.tool_calls else "stop"))

    async def aclose(self):
        pass


async def main():
    with TemporaryDirectory(prefix="miniagent-demo-") as workspace:
        Path(workspace, "note.txt").write_text("本项目构建可复用 Agent。", encoding="utf-8")
        model = ScriptedModel()
        try:
            results = await run_tasks(model, workspace)
            for index, result in enumerate(results, 1):
                print(f"任务 {index}: {result.status} / {result.reason}")
                print(result.messages[-1].content)
        finally:
            await model.aclose()


if __name__ == "__main__":
    asyncio.run(main())
