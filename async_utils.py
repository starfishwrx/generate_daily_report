from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Awaitable, Iterable
from typing import TypeVar

import httpx

from autodatareport.events import current_metrics


T = TypeVar("T")
_GLOBAL_SEMAPHORE: ContextVar[asyncio.Semaphore | None] = ContextVar(
    "autodatareport_global_semaphore",
    default=None,
)


class RetryingAsyncClient(httpx.AsyncClient):
    """Async client with bounded retry/backoff for transient transport and server failures."""

    def __init__(self, *args, request_retries: int = 3, retry_backoff_seconds: float = 0.5, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.request_retries = max(1, int(request_retries))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))

    async def request(self, method: str, url: httpx.URL | str, *args, **kwargs) -> httpx.Response:
        for attempt in range(1, self.request_retries + 1):
            try:
                global_semaphore = _GLOBAL_SEMAPHORE.get()
                if global_semaphore is None:
                    response = await super().request(method, url, *args, **kwargs)
                else:
                    # Acquire the shared budget at the actual I/O boundary.  Holding it
                    # around a higher-level coroutine can deadlock when that coroutine
                    # starts another gather_limited() batch.
                    async with global_semaphore:
                        response = await super().request(method, url, *args, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt >= self.request_retries:
                    raise
                self._record_retry()
                await asyncio.sleep(self._retry_delay(None, attempt))
                continue
            if response.status_code != 429 and not 500 <= response.status_code <= 599:
                return response
            if attempt >= self.request_retries:
                return response
            await response.aclose()
            self._record_retry()
            await asyncio.sleep(self._retry_delay(response, attempt))
        raise RuntimeError("unreachable retry state")

    @staticmethod
    def _record_retry() -> None:
        metrics = current_metrics()
        if metrics is not None:
            metrics.increment("retries")

    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After", "").strip() if response is not None else ""
        delay: float | None = None
        if retry_after:
            try:
                delay = max(0.0, float(retry_after))
            except ValueError:
                try:
                    when = parsedate_to_datetime(retry_after)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    delay = max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    delay = None
        if delay is None:
            delay = self.retry_backoff_seconds * attempt
        jitter = random.uniform(0.0, min(0.25, delay * 0.1)) if delay > 0 else 0.0
        return delay + jitter


@contextmanager
def request_budget(limit: int):
    """Share a bounded request budget across independent async sources."""

    token = _GLOBAL_SEMAPHORE.set(asyncio.Semaphore(max(1, int(limit))))
    try:
        yield
    finally:
        _GLOBAL_SEMAPHORE.reset(token)


async def gather_limited(awaitables: Iterable[Awaitable[T]], limit: int = 4) -> list[T]:
    semaphore = asyncio.Semaphore(max(1, int(limit)))

    async def run(item: Awaitable[T]) -> T:
        async with semaphore:
            return await item

    return list(await asyncio.gather(*(run(item) for item in awaitables)))
