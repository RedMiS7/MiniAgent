import math
import os
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class ModelConfig:
    provider: Literal["openai", "deepseek", "bailian"]
    model: str
    api_key: str = field(repr=False)
    timeout: float = 60.0
    max_retries: int = 2

    def __post_init__(self) -> None:
        if self.provider not in ("openai", "deepseek", "bailian"):
            raise ValueError("Provider must be openai, deepseek or bailian.")
        if not self.model.strip():
            raise ValueError("Model name is required.")
        if not self.api_key.strip():
            raise ValueError("API key is required.")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Timeout must be a positive finite number.")
        if self.max_retries < 0:
            raise ValueError("Max retries must be nonnegative.")

    @property
    def base_url(self) -> str:
        if self.provider == "deepseek":
            return "https://api.deepseek.com"
        if self.provider == "bailian":
            return "https://dashscope.aliyuncs.com/compatible-mode/v1"
        return "https://api.openai.com/v1"

    @classmethod
    def from_env(
        cls, provider: Literal["openai", "deepseek", "bailian"], model: str,
        timeout: float = 60.0, max_retries: int = 2,
    ) -> "ModelConfig":
        if provider not in ("openai", "deepseek", "bailian"):
            raise ValueError("Provider must be openai, deepseek or bailian.")
        key_name = {
            "openai": "OPENAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
            "bailian": "DASHSCOPE_API_KEY",
        }[provider]
        api_key = os.environ.get(key_name, "")
        if not api_key.strip():
            raise ValueError(f"Set the {key_name} environment variable.")
        return cls(provider, model, api_key, timeout, max_retries)
