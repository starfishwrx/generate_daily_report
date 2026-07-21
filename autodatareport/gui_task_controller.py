from __future__ import annotations

import itertools
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from autodatareport.process_runner import TaskProcessRunner


@dataclass
class ActiveTask:
    task_id: int
    kind: str
    label: str
    process: subprocess.Popen[str]
    stage: str = "starting"
    cancel_requested: bool = False

    @property
    def publishing(self) -> bool:
        return self.stage.startswith("publish")


class GuiTaskController:
    """Own exactly one GUI subprocess and reject events from replaced tasks."""

    def __init__(self, runner: TaskProcessRunner | None = None) -> None:
        self.runner = runner or TaskProcessRunner()
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self._active: ActiveTask | None = None

    @property
    def active(self) -> ActiveTask | None:
        with self._lock:
            return self._active

    @property
    def busy(self) -> bool:
        return self.active is not None

    def is_current(self, task_id: int) -> bool:
        active = self.active
        return active is not None and active.task_id == task_id

    def start(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        kind: str,
        label: str,
        on_line: Callable[[int, str], None],
        on_done: Callable[[int, int], None],
    ) -> int:
        with self._lock:
            if self._active is not None:
                raise RuntimeError("已有任务正在运行")
            task_id = next(self._ids)
            process = self.runner.open(command, cwd=cwd, env=env)
            self._active = ActiveTask(task_id, kind, label, process)

        def worker() -> None:
            rc = 1
            try:
                rc = self.runner.stream(process, lambda line: on_line(task_id, line))
            finally:
                on_done(task_id, rc)

        threading.Thread(target=worker, daemon=True, name=f"gui-task-{task_id}").start()
        return task_id

    def set_stage(self, task_id: int, stage: str) -> None:
        with self._lock:
            if self._active is not None and self._active.task_id == task_id:
                self._active.stage = str(stage or "")

    def finish(self, task_id: int) -> bool:
        with self._lock:
            if self._active is None or self._active.task_id != task_id:
                return False
            self._active = None
            return True

    def stop(self, *, wait_seconds: float = 5.0) -> bool:
        active = self.active
        if active is None:
            return False
        active.cancel_requested = True
        self.runner.terminate(active.process)
        try:
            active.process.wait(timeout=max(0.1, float(wait_seconds)))
        except subprocess.TimeoutExpired:
            active.process.kill()
            active.process.wait(timeout=2)
        return True
