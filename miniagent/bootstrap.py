from openai import AsyncOpenAI

from .config import ModelConfig
from .models import LLM
from .models.deepseek_adapter import DeepSeekAdapter
from .models.openai_adapter import OpenAIAdapter


def create_model(config: ModelConfig) -> LLM:
    client = AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.timeout,
        max_retries=config.max_retries,
    )
    adapter = DeepSeekAdapter if config.provider == "deepseek" else OpenAIAdapter
    return adapter(client=client, model=config.model)
