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
    """Run independent source stages concurrently while preserving named results."""

    task_list = list(tasks)
    semaphore = asyncio.Semaphore(max(1, int(max_active_sources)))

    async def execute(task: SourceTask) -> tuple[str, Any, BaseException | None]:
        async with semaphore:
            try:
                return task.name, await task.run(), None
            except BaseException as exc:  # noqa: BLE001
                return task.name, None, exc

    completed = await asyncio.gather(*(execute(task) for task in task_list))
    values: dict[str, Any] = {}
    errors: dict[str, BaseException] = {}
    task_map = {task.name: task for task in task_list}
    for name, value, error in completed:
        if error is None:
            values[name] = value
            continue
        errors[name] = error
        if task_map[name].strict:
            continue
    return SourceBatchResult(values=values, errors=errors)
