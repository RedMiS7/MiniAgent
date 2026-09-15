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
