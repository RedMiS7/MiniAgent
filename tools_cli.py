"""Exercise registered tools; image actions may call model APIs."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

from miniagent.bootstrap import create_tools
from miniagent.config import ModelConfig
from miniagent.models import ToolCall
from miniagent.tools import ToolContext, ToolExecutor, ToolStarted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--tool")
    parser.add_argument("--provider", choices=["openai", "deepseek", "bailian"])
    parser.add_argument("--model", help="Current model used by the image tool")
    parser.add_argument("--image-model", action="append", default=[],
                        metavar="PROVIDER:MODEL", help="Explicitly available alternative; repeatable")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--arguments", default="{}", help="JSON object")
    inputs.add_argument("--arguments-file", type=Path, help="UTF-8 JSON file")
    parser.add_argument("--allow-commands", action="store_true")
    args = parser.parse_args()
    if bool(args.provider) != bool(args.model):
        parser.error("--provider and --model must be used together.")
    try:
        current = ModelConfig.from_env(args.provider, args.model) if args.model else None
        alternatives = {}
        for spec in args.image_model:
            provider, name = spec.split(":", 1)
            if spec in alternatives:
                raise ValueError("Duplicate image model.")
            alternatives[spec] = ModelConfig.from_env(provider, name)
        registry = create_tools(current, alternatives)
    except ValueError as exc:
        parser.error(str(exc))
    if args.list:
        print(json.dumps([d.model_dump() for d in registry.definitions()], ensure_ascii=False, indent=2))
        return 0
    if not args.tool:
        parser.error("Specify --tool or --list.")
    try:
        context = ToolContext(args.workspace, allow_commands=args.allow_commands)
    except (ValueError, OSError):
        parser.error("Workspace must be an existing directory.")
    def show(event):
        elapsed = "" if isinstance(event, ToolStarted) else f" ({event.elapsed_seconds:.3f}s)"
        print(f"[{event.type}] {event.tool_name}{elapsed}", file=sys.stderr)
    executor = ToolExecutor(registry, context, on_event=show)
    arguments = args.arguments
    if args.arguments_file:
        try:
            arguments = args.arguments_file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            parser.error("Cannot read arguments file.")
    try:
        result = asyncio.run(executor.execute(ToolCall(id="cli-1", name=args.tool, arguments=arguments)))
    except KeyboardInterrupt:
        return 130
    print(result.to_message("cli-1").content)
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
