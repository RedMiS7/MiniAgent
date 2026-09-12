"""Single real-API request: python cli.py --help."""
import argparse
import asyncio
from contextlib import aclosing
import sys
from typing import Sequence

from miniagent.bootstrap import create_model
from miniagent.config import ModelConfig
from miniagent.models import (
    GenerationOptions, LLMError, LLMRequest, Message, ResponseCompleted, TextDelta,
)


async def run(
    config: ModelConfig,
    prompt: str,
    system: str | None,
    options: GenerationOptions | None = None,
    stream: bool = False,
) -> str:
    # 创建某个模型的adpter
    model = create_model(config)
    try:
        messages = []
        if system is not None:
            messages.append(Message(role="system", content=system))
        messages.append(Message(role="user", content=prompt))
        request = LLMRequest(messages, options=options or GenerationOptions())
        if stream:
            response = None
            async with aclosing(model.stream(request)) as events:
                async for event in events:
                    if isinstance(event, TextDelta):
                        print(event.text, end="", flush=True)
                    elif isinstance(event, ResponseCompleted):
                        response = event.response
            print()
            if response is None:
                raise LLMError("invalid_response", "Stream returned no final response.")
        else:
            # 调用模型返回回复
            response = await model.generate(request)
        if response.finish_reason != "stop":
            print(f"Generation ended: {response.finish_reason}", file=sys.stderr)
        return response.text
    finally:
        await model.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test one real model API call.")
    parser.add_argument("--provider", required=True, choices=["openai", "deepseek"])
    parser.add_argument("--model", required=True, help="Model ID available to your account")
    parser.add_argument("--prompt", default="你好")
    parser.add_argument("--system", help="Optional system message")
    parser.add_argument("--timeout", type=float, default=60.0, help="SDK request timeout in seconds")
    parser.add_argument("--max-retries", type=int, default=2, help="SDK retries; use 0 to disable")
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--reasoning", choices=["none", "low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.prompt.strip():
            raise ValueError("Prompt must not be blank.")
        config = ModelConfig.from_env(
            args.provider, args.model, args.timeout, args.max_retries,
        )
        options = GenerationOptions(args.max_output_tokens, args.reasoning, args.temperature)
        answer = asyncio.run(run(config, args.prompt, args.system, options, args.stream))
        if not args.stream:
            print(answer)
        return 0
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except LLMError as exc:
        print(f"Model error [{exc.code}]: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
