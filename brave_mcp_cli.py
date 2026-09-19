"""Explicit MCP discovery/search smoke test, without a model or Harness policy."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

from miniagent.bootstrap import connect_brave_search
from miniagent.models import ToolCall
from miniagent.tools import ToolContext, ToolExecutor


async def run(query):
    async with connect_brave_search() as registry:
        if query is None:
            for definition in registry.definitions():
                print(definition.model_dump_json(indent=2))
            return 0
        executor = ToolExecutor(registry, ToolContext(Path.cwd()))
        result = await executor.execute(ToolCall(
            id="search-1", name="brave_web_search",
            arguments=json.dumps({"query": query, "count": 3}),
        ))
        print(result.model_dump_json(indent=2))
        return 0 if result.success else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Brave MCP 接入验证；默认只发现工具，不搜索。")
    parser.add_argument("--query", help="显式执行一次真实搜索；尚未接入 Harness 人工审批")
    args = parser.parse_args(argv)
    if args.query is not None and not args.query.strip():
        parser.error("query must not be blank")
    try:
        return asyncio.run(run(args.query))
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("MCP 连接或调用失败。请检查 BRAVE_API_KEY、Node.js 和网络配置。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
