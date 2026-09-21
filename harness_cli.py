"""Run a model with Brave MCP search and mandatory per-call terminal approval."""
import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
import signal
import sys

from agent_cli import Console
from miniagent.agent import AgentHarness, RetryPolicy
from miniagent.bootstrap import connect_brave_search, create_model
from miniagent.config import ModelConfig
from miniagent.tools import ToolContext, ToolExecutor, ToolRegistry
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


@asynccontextmanager
async def tool_connection(offline, tool_failures=0):
    if offline:
        from examples.retry_faults import FaultyEchoTool
        registry = ToolRegistry()
        registry.register(FaultyEchoTool(tool_failures))
        yield registry
    else:
        async with connect_brave_search() as registry:
            yield registry


async def run(config, prompt, *, max_steps=12, retry_policy=None, fault=None, offline=False, tool_failures=0):
    console = Console()
    # MCP scope owns the process; Harness owns the model. Run exits before both.
    async with tool_connection(offline, tool_failures) as registry:
        executor = ToolExecutor(registry, ToolContext(Path.cwd()))
        if offline:
            from examples.retry_faults import OfflineModel
            model = OfflineModel()
            console.status("[离线演练] 不连接模型或 MCP；仅执行本地 echo")
        else:
            model = create_model(config)
        if fault is not None:
            from examples.retry_faults import FaultInjectingModel
            model = FaultInjectingModel(model, fault, console.status)
        async with AgentHarness(model, executor, owns_model=True,
                                approval_required=() if offline else ("brave_web_search",), approve=approve_tool,
                                retry_policy=retry_policy,
                                on_retry=lambda number, delay, code: console.status(
                                    f"[模型重试] 第 {number} 次重试，错误={code}，等待 {delay:.3f}s"),
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
            except Exception:
                console.status("[执行错误] Run 失败，详见结束原因与错误码")
                return 1
            finally:
                signal.signal(signal.SIGINT, previous)
                if active is not None and active.result is not None:
                    console.status(f"[Run结果] {active.result.status} · {active.result.reason}")
                    if active.result.error_code:
                        console.status(f"[错误码] {active.result.error_code}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Harness + Brave 搜索；每次搜索必须人工批准。")
    parser.add_argument("--provider", choices=["openai", "deepseek", "bailian"])
    parser.add_argument("--model")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--offline", action="store_true", help="无密钥/无网络的 echo 重试演练")
    parser.add_argument("--retries", type=int, default=2, help="每个模型步骤的额外重试次数，默认 2")
    parser.add_argument("--retry-base", type=float, default=1, help="退避初始上限秒数")
    parser.add_argument("--retry-cap", type=float, default=30, help="退避最大上限秒数")
    from examples.retry_faults import ERRORS, FaultPlan
    parser.add_argument("--fault-error", choices=ERRORS, help="显式注入模型错误，默认关闭")
    parser.add_argument("--fault-count", type=int, default=1, help="目标步骤连续注入次数")
    parser.add_argument("--fault-step", type=int, default=1, help="注入的模型步骤，离线第二步在 echo 完成后")
    parser.add_argument("--fault-after-partial", action="store_true", help="先发出模拟文本再报错，验证不重试")
    parser.add_argument("--fault-tool-count", type=int, default=0, help="离线 echo 在执行前失败的次数，默认 0")
    args = parser.parse_args(argv)
    if not args.prompt.strip() or args.max_steps < 1:
        parser.error("prompt must not be blank and max-steps must be positive")
    if args.fault_tool_count < 0 or (args.fault_tool_count and not args.offline):
        parser.error("--fault-tool-count must be nonnegative and requires --offline")
    if not args.offline and (not args.provider or not args.model):
        parser.error("--provider and --model are required unless --offline is used")
    if args.fault_error is None and (args.fault_count != 1 or args.fault_step != 1 or args.fault_after_partial):
        parser.error("fault settings require --fault-error")
    try:
        policy = RetryPolicy(args.retries, args.retry_base, args.retry_cap)
        fault = FaultPlan(args.fault_error, args.fault_count, args.fault_step, args.fault_after_partial) if args.fault_error else None
    except ValueError as exc:
        parser.error(str(exc))
    try:
        config = None if args.offline else ModelConfig.from_env(args.provider, args.model, args.timeout, 0)
        return asyncio.run(run(config, args.prompt, max_steps=args.max_steps,
                               retry_policy=policy, fault=fault, offline=args.offline, tool_failures=args.fault_tool_count))
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("Harness 执行失败；请检查模型配置、BRAVE_API_KEY 及网络。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
