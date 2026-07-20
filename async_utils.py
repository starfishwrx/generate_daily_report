from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterable
from typing import TypeVar


T = TypeVar("T")


async def gather_limited(awaitables: Iterable[Awaitable[T]], limit: int = 4) -> list[T]:
    semaphore = asyncio.Semaphore(max(1, int(limit)))

    async def run(item: Awaitable[T]) -> T:
        async with semaphore:
            return await item

    return list(await asyncio.gather(*(run(item) for item in awaitables)))
