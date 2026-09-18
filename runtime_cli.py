"""Single-run Runtime API smoke test: python runtime_cli.py --help."""
import argparse
import asyncio
import math
from pathlib import Path
import signal
import sys
from typing import Sequence

from agent_cli import Console
from miniagent.agent import AgentLoop, AgentRuntime
from miniagent.bootstrap import create_model, create_tools
from miniagent.config import ModelConfig
from miniagent.models import GenerationOptions, LLMError, Message
from miniagent.tools import ToolContext, ToolExecutor


async def run(config, prompt, workspace, options, *, system=None, allow_commands=False,
              max_steps=12, cancel_after=None):
    context = ToolContext(workspace, allow_commands=allow_commands)
    executor = ToolExecutor(create_tools(config), context)
    model = create_model(config)
    console = Console()
    runtime = AgentRuntime(AgentLoop(model, executor))
    event_loop = asyncio.get_running_loop()
    previous_sigint = signal.getsignal(signal.SIGINT)
    timer = None
    exit_code = 1

    def interrupt(signum, frame):
        event_loop.call_soon_threadsafe(runtime.cancel)

    try:
        signal.signal(signal.SIGINT, interrupt)
        messages = [
            Message(role="system", content=system or (
                "你是一个帮助用户完成具体任务的 Agent。按需使用工具检查事实，再给出简洁中文回答。"
                "文件路径必须相对于工作目录。遵守用户要求，不执行无关操作。"
            )),
            Message(role="user", content=prompt),
        ]
        console.status(f"[工作目录] {context.workspace}")
        console.status(f"[Runtime] 最多 {max_steps} 轮模型调用；Ctrl+C 请求取消并等待清理")
        console.status(f"[命令工具] {'已授权' if allow_commands else '未授权'}")
        async with runtime.stream(messages, options, max_steps=max_steps) as active:
            if cancel_after is not None:
                timer = event_loop.call_later(cancel_after, active.cancel)
                console.status(f"[取消测试] {cancel_after:g} 秒后请求取消")
            async for event in active.events():
                console.show(event)
        exit_code = 0 if runtime.result.status == "succeeded" else 1
    except asyncio.CancelledError:
        exit_code = 130
    except LLMError as exc:
        console.status(f"[模型错误] {exc.code}")
    except Exception:
        console.status("[执行错误] Runtime 执行失败")
    finally:
        if timer is not None:
            timer.cancel()
        try:
            result = runtime.result
            if result is not None:
                console.status(f"[Run结果] {result.status} · {result.reason}")
                if result.error_code is not None:
                    console.status(f"[错误码] {result.error_code}")
                if result.cleanup_error is not None:
                    console.status(f"[运行清理错误] {result.cleanup_error}")
            try:
                await model.aclose()
            except Exception:
                console.status("[模型资源] 关闭失败")
                if exit_code == 0:
                    exit_code = 1
            else:
                console.status("[模型资源] 已关闭")
        finally:
            signal.signal(signal.SIGINT, previous_sigint)
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通过真实模型 API 验证单次 Runtime，完整显示事件与结束结果。")
    parser.add_argument("--provider", required=True, choices=["openai", "deepseek", "bailian"])
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--system")
    parser.add_argument("--allow-commands", action="store_true", help="允许命令工具；默认禁止")
    parser.add_argument("--max-steps", type=int, default=12, help="模型调用轮数上限")
    parser.add_argument("--timeout", type=float, default=60, help="模型请求超时秒数")
    parser.add_argument("--max-retries", type=int, default=0, help="SDK 重试次数；测试入口默认 0")
    parser.add_argument("--cancel-after", type=float, help="指定秒数后请求取消，仍等待清理")
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--reasoning", choices=["none", "low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--temperature", type=float)
    args = parser.parse_args(argv)
    try:
        if not args.prompt.strip():
            raise ValueError("任务不能为空。")
        if args.max_steps < 1:
            raise ValueError("--max-steps must be positive.")
        if args.cancel_after is not None and (
            not math.isfinite(args.cancel_after) or args.cancel_after <= 0
        ):
            raise ValueError("--cancel-after must be a positive finite number.")
        config = ModelConfig.from_env(args.provider, args.model, args.timeout, args.max_retries)
        options = GenerationOptions(max_output_tokens=args.max_output_tokens,
                                    reasoning=args.reasoning, temperature=args.temperature)
        return asyncio.run(run(config, args.prompt, args.workspace, options, system=args.system,
                               allow_commands=args.allow_commands, max_steps=args.max_steps,
                               cancel_after=args.cancel_after))
    except (ValueError, OSError) as exc:
        print(f"配置或输入错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n[Runtime] 已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
