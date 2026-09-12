from typing import AsyncIterator, Protocol

from .events import LLMEvent
from .types import LLMRequest, LLMResponse


class LLM(Protocol):
    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Return a model result, or raise LLMError."""
        ...

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        """Yield deltas and one final response; raise LLMError on failure."""
        ...

    async def aclose(self) -> None:
        """Release resources. Close a stream explicitly when stopping early."""
        ...
