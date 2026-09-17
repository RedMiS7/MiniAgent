"""Interactive multi-turn Agent conversation: python agent_cli.py --help."""
import argparse
import asyncio
import json
from contextlib import aclosing
from pathlib import Path
import sys
from typing import Sequence

from miniagent.agent import AgentLoop
from miniagent.bootstrap import create_model, create_tools
from miniagent.config import ModelConfig
from miniagent.models import GenerationOptions, LLMError, Message
from miniagent.tools import ToolContext, ToolExecutor


class Console:
    def __init__(self):
        self.text_open = False
        self.round_text = False
        self.tool_labels = {}

    def status(self, text):
        if self.text_open:
            print(flush=True)
            self.text_open = False
        print(text, file=sys.stderr, flush=True)

    def show(self, event):
        if event.type == "agent_started":
            self.tool_labels.clear()
            self.status("[Agent] 开始任务")
        elif event.type == "agent_progress":
            labels = {"model": "正在调用模型", "tools": "正在执行工具", "completed": "工具结果已回传"}
            self.status(f"[第 {event.step} 轮] {labels[event.phase]}")
            if event.phase == "model":
                self.round_text = False
        elif event.type == "llm_text_delta":
            print(event.text, end="", flush=True)
            self.text_open = True
            self.round_text = True
        elif event.type == "llm_response_completed":
            self.tool_labels.clear()
            for call in event.response.message.tool_calls:
                if call.name not in ("list_files", "read_file", "write_file", "edit_file"):
                    continue
                try:
                    arguments = json.loads(call.arguments)
                except (ValueError, RecursionError):
                    continue
                path = arguments.get("path") if isinstance(arguments, dict) else None
                if isinstance(path, str) and path:
                    self.tool_labels[call.id] = f"{call.name} · {json.dumps(path, ensure_ascii=False)}"
            if not self.round_text and event.response.text:
                print(event.response.text, end="", flush=True)
                self.text_open = True
            self.status(f"[模型响应结束] {event.response.finish_reason}")
        elif event.type == "tool_started":
            self.status(f"[工具开始] {self.tool_labels.get(event.call_id, event.tool_name)}")
        elif event.type in ("tool_completed", "tool_failed", "tool_cancelled"):
            labels = {"tool_completed": "工具成功", "tool_failed": "工具失败", "tool_cancelled": "工具取消"}
            code = getattr(event, "error_code", None)
            detail = f" · {code}" if code else ""
            target = self.tool_labels.pop(event.call_id, event.tool_name)
            self.status(f"[{labels[event.type]}] {target} · {event.elapsed_seconds:.3f}s{detail}")
        elif event.type == "agent_completed":
            self.status("[Agent] 任务完成")
        elif event.type == "agent_failed":
            self.status(f"[Agent] 未完成：{event.reason}")
        elif event.type == "agent_cancelled":
            self.status("[Agent] 已取消")


async def run(config, prompt, workspace, options, *, system=None, allow_commands=False, max_steps=12):
    context = ToolContext(workspace, allow_commands=allow_commands)
    executor = ToolExecutor(create_tools(config), context)
    model = create_model(config)
    console = Console()
    interactive = prompt is None
    try:
        messages = [Message(role="system", content=system or (
            "你是一个帮助用户完成具体任务的 Agent。按需使用工具检查事实，再给出简洁中文回答。"
            "文件路径必须相对于工作目录。遵守用户要求，不执行无关操作。"
        ))]
        loop = AgentLoop(model, executor)
        console.status(f"[工作目录] {context.workspace}")
        console.status(f"[命令工具] {'已授权' if allow_commands else '未授权'} · 每次任务最多 {max_steps} 轮模型调用")
        if interactive:
            console.status("[会话] 可连续输入，历史在本次进程内保留；输入 /exit 退出。")
        while True:
            if interactive:
                try:
                    prompt = input("你> ")
                except EOFError:
                    console.status("[会话] 已退出")
                    return 0
                if prompt.strip() == "/exit":
                    console.status("[会话] 已退出")
                    return 0
                if not prompt.strip():
                    continue
            messages.append(Message(role="user", content=prompt))
            completed = False
            async with aclosing(loop.stream(messages, options, max_steps=max_steps)) as events:
                async for event in events:
                    console.show(event)
                    if event.type == "agent_completed":
                        messages = list(event.messages)
                        completed = True
            if not completed:
                console.status("[会话] 本轮未完成，会话已结束；请重新启动，不能直接续接未完成的历史。")
                return 1
            if not interactive:
                return 0
    except LLMError:
        console.status("[会话] 模型调用失败，会话已结束；请重新启动。")
        raise
    finally:
        await model.aclose()

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多轮 Agent 对话：保留历史并显示每一步模型和工具执行过程。")
    parser.add_argument("--provider", required=True, choices=["openai", "deepseek", "bailian"])
    parser.add_argument("--model", required=True)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--prompt", help="单次执行指定任务；省略时进入多轮对话")
    parser.add_argument("--system")
    parser.add_argument("--allow-commands", action="store_true", help="允许命令工具；默认禁止")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--reasoning", choices=["none", "low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--temperature", type=float)
    args = parser.parse_args(argv)
    try:
        if args.max_steps < 1:
            raise ValueError("--max-steps must be positive.")
        config = ModelConfig.from_env(args.provider, args.model, args.timeout, args.max_retries)
        options = GenerationOptions(max_output_tokens=args.max_output_tokens,
                                    reasoning=args.reasoning, temperature=args.temperature)
        prompt = args.prompt
        if prompt is not None and not prompt.strip():
            raise ValueError("任务不能为空。")
        return asyncio.run(run(config, prompt, args.workspace, options, system=args.system,
                               allow_commands=args.allow_commands, max_steps=args.max_steps))
    except (ValueError, OSError) as exc:
        print(f"配置或输入错误：{exc}", file=sys.stderr)
        return 2
    except LLMError as exc:
        print(f"模型错误 [{exc.code}]：{exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n[Agent] 已取消，会话已结束；请重新启动。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
