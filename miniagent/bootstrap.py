from contextlib import asynccontextmanager

from openai import AsyncOpenAI

from .config import ModelConfig
from .models import LLM
from .models.bailian_adapter import BailianAdapter
from .models.deepseek_adapter import DeepSeekAdapter
from .models.openai_adapter import OpenAIAdapter


def create_model(config: ModelConfig) -> LLM:
    client = AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.timeout,
        max_retries=config.max_retries,
    )
    adapter = {
        "openai": OpenAIAdapter, "deepseek": DeepSeekAdapter, "bailian": BailianAdapter,
    }[config.provider]
    return adapter(client=client, model=config.model)


def create_tools(model_config: ModelConfig | None = None, image_models: dict[str, ModelConfig] | None = None):
    """Explicitly load trusted built-in extensions."""
    from .extensions import files, shell, image
    from .models.image_adapter import ImageAdapter
    from .tools import ToolRegistry

    registry = ToolRegistry()
    for extension in (files, shell):
        extension.register(registry)
    configs = dict(image_models or {})
    if "current" in configs:
        raise ValueError("'current' is reserved for the current model.")
    backends = {name: ImageAdapter(config) for name, config in configs.items()}
    if model_config is not None:
        backends["current"] = ImageAdapter(model_config)
    image.register(registry, backends)
    return registry


def create_harness(config: ModelConfig, context, *, retry_policy=None, on_retry=None):
    """Create a Harness that owns its model, using the built-in extensions."""
    from .agent import AgentHarness
    from .tools import ToolExecutor

    executor = ToolExecutor(create_tools(config), context)
    if retry_policy is not None:
        from dataclasses import replace
        config = replace(config, max_retries=0)
    return AgentHarness(create_model(config), executor, owns_model=True,
                        retry_policy=retry_policy, on_retry=on_retry)


@asynccontextmanager
async def connect_brave_search():
    """Own a Brave STDIO server and session; consume tools inside this scope."""
    import os
    import shutil
    from datetime import timedelta
    from tempfile import TemporaryDirectory

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from .extensions import brave_search
    from .tools import ToolRegistry

    key = os.environ.get("BRAVE_API_KEY", "").strip()
    if not key:
        raise ValueError("Set BRAVE_API_KEY before connecting to Brave Search.")
    command = shutil.which("npx")
    if command is None:
        raise ValueError("Brave MCP requires Node.js and npx on PATH.")
    # Keep the server's dotenv loader away from the project's .env files.
    with TemporaryDirectory(prefix="miniagent-brave-") as cwd:
        parameters = StdioServerParameters(
            command=command,
            args=["-y", "@brave/brave-search-mcp-server@2.1.4", "--transport", "stdio"],
            env={"BRAVE_API_KEY": key}, cwd=cwd,
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session:
                await session.initialize()
                registry = ToolRegistry()
                await brave_search.register(registry, session)
                yield registry
