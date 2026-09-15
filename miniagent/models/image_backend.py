"""Internal image protocol; implementations keep provider SDKs private."""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ImageCapabilities:
    inspect: bool = False
    generate: bool = False


class ImageBackend(Protocol):
    capabilities: ImageCapabilities

    async def inspect(self, data: bytes, mime_type: str, prompt: str) -> str:
        ...

    async def generate(self, prompt: str, max_bytes: int) -> bytes:
        ...
