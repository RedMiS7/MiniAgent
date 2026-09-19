"""Run a model with Brave MCP search and mandatory per-call terminal approval."""
import argparse
import asyncio
import json
from pathlib import Path
import signal
import sys

from agent_cli import Console
from miniagent.agent import AgentHarness
from miniagent.bootstrap import connect_brave_search, create_model
from miniagent.config import ModelConfig
from miniagent.tools import ToolContext, ToolExecutor
from miniagent.models import Message


async def read_decision():
    """Poll terminal input without leaving an uncancellable input thread behind."""
    if not sys.stdin.isatty():
        return False
    if sys.platform == "win32":
        import msvcrt
        while True:
            if msvcrt.kbhit():
                key = msvcrt.getwch()
                if key == "\x03":
                    raise asyncio.CancelledError()
                if key.lower() == "y":
                    return True
                if key.lower() == "n" or key in ("\r", "\n", "\x1b"):
                    return False
            await asyncio.sleep(0.05)
    else:
        import select
        while not select.select([sys.stdin], [], [], 0)[0]:
            await asyncio.sleep(0.05)
        return sys.stdin.readline().strip().lower() == "y"


async def approve_tool(call, arguments):
    print("\n[人工审批] " + json.dumps({"tool": call.name, "call_id": call.id,
                                      "arguments": arguments}, ensure_ascii=True), file=sys.stderr)
    print("批准这一次搜索？[y/N]（Ctrl+C 取消任务）", file=sys.stderr, flush=True)
    approved = await read_decision()
    print("[审批] 已批准" if approved else "[审批] 已拒绝", file=sys.stderr)
    return approved


async def run(config, prompt, *, max_steps=12):
    console = Console()
    # MCP scope owns the process; Harness owns the model. Run exits before both.
    async with connect_brave_search() as registry:
        executor = ToolExecutor(registry, ToolContext(Path.cwd()))
        async with AgentHarness(create_model(config), executor, owns_model=True,
                                approval_required=("brave_web_search",), approve=approve_tool,
                                on_event=lambda run, event: console.show(event)) as harness:
            active = None
            previous = signal.getsignal(signal.SIGINT)
            loop = asyncio.get_running_loop()

            def interrupt(signum, frame):
                if active is not None:
                    loop.call_soon_threadsafe(active.cancel)

            try:
                signal.signal(signal.SIGINT, interrupt)
                async with harness.run([Message(role="user", content=prompt)], max_steps=max_steps) as active:
                    async for _ in active.events():
                        pass
                return 0 if active.result.status == "succeeded" else 1
            except asyncio.CancelledError:
                return 130
            finally:
                signal.signal(signal.SIGINT, previous)
                if active is not None and active.result is not None:
                    console.status(f"[Run结果] {active.result.status} · {active.result.reason}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Harness + Brave 搜索；每次搜索必须人工批准。")
    parser.add_argument("--provider", required=True, choices=["openai", "deepseek", "bailian"])
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args(argv)
    if not args.prompt.strip() or args.max_steps < 1:
        parser.error("prompt must not be blank and max-steps must be positive")
    try:
        config = ModelConfig.from_env(args.provider, args.model, args.timeout, 0)
        return asyncio.run(run(config, args.prompt, max_steps=args.max_steps))
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("Harness 执行失败；请检查模型配置、BRAVE_API_KEY 及网络。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
