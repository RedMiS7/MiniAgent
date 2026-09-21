"""Bounded retry of one model request, before any model event is delivered."""
import asyncio
from dataclasses import dataclass
import math
import random
from typing import Callable

from miniagent.models import LLM, LLMError, ResponseCompleted

RetryCallback = Callable[[int, float, str], None]


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2
    base_delay: float = 1.0
    max_delay: float = 30.0

    def __post_init__(self):
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer.")
        for value in (self.base_delay, self.max_delay):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("Retry delays must be positive finite numbers.")
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay must be at least base_delay.")


class RetryingModel:
    """Borrow a model; Harness retains ownership of the original dependency."""
    def __init__(self, model: LLM, policy: RetryPolicy, on_retry: RetryCallback | None = None):
        self._model = model
        self._policy = policy
        self._on_retry = on_retry

    async def stream(self, request):
        retries = 0
        ceiling = self._policy.base_delay
        while True:
            emitted = False
            stream = None
            failure = None
            try:
                stream = self._model.stream(request.model_copy(deep=True))
                async for event in stream:
                    emitted = True
                    yield event
            except BaseException as exc:
                failure = exc
            finally:
                if stream is not None:
                    try:
                        await stream.aclose()
                    except BaseException as cleanup_error:
                        if failure is not None:
                            raise failure from cleanup_error
                        raise
            if failure is None:
                return
            if (not isinstance(failure, LLMError) or not failure.retryable or emitted
                    or failure.__cause__ is not None
                    or retries >= self._policy.max_retries):
                raise failure
            retries += 1
            delay = random.uniform(0.0, ceiling)
            if self._on_retry is not None:
                try:
                    self._on_retry(retries, delay, failure.code)
                except (Exception, asyncio.CancelledError):
                    pass
            await asyncio.sleep(delay)
            ceiling = min(self._policy.max_delay, ceiling * 2)

    async def generate(self, request):
        result = None
        async for event in self.stream(request):
            if isinstance(event, ResponseCompleted):
                result = event.response
        if result is None:
            raise LLMError("invalid_response", "Model stream returned no response.")
        return result

    async def aclose(self):
        await self._model.aclose()
