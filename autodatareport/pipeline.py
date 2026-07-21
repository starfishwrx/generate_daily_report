from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable


@dataclass(frozen=True)
class SourceTask:
    name: str
    run: Callable[[], Awaitable[Any]]
    strict: bool = True


@dataclass
class SourceBatchResult:
    values: dict[str, Any]
    errors: dict[str, BaseException]


async def run_source_tasks(tasks: Iterable[SourceTask], *, max_active_sources: int = 3) -> SourceBatchResult:
    """Run sources concurrently, cancelling siblings after the first strict failure."""

    task_list = list(tasks)
    semaphore = asyncio.Semaphore(max(1, int(max_active_sources)))

    async def execute(task: SourceTask) -> tuple[str, Any]:
        async with semaphore:
            return task.name, await task.run()

    values: dict[str, Any] = {}
    errors: dict[str, BaseException] = {}
    scheduled = {asyncio.create_task(execute(task)): task for task in task_list}
    pending = set(scheduled)
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for future in done:
            source = scheduled[future]
            try:
                name, value = future.result()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                errors[source.name] = exc
                if source.strict:
                    for sibling in pending:
                        sibling.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    return SourceBatchResult(values=values, errors=errors)
            else:
                values[name] = value
    return SourceBatchResult(values=values, errors=errors)
