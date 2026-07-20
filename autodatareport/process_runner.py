from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path


class TaskProcessRunner:
    """Own the streaming subprocess mechanics used by the Tkinter shell."""

    def open(self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> subprocess.Popen[str]:
        return subprocess.Popen(
            list(command),
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=dict(env),
        )

    @staticmethod
    def stream(process: subprocess.Popen[str], on_line: Callable[[str], None]) -> int:
        if process.stdout is None:
            raise RuntimeError("任务进程没有可读取的输出流。")
        for raw_line in process.stdout:
            on_line(raw_line.rstrip("\n"))
        return process.wait()

    @staticmethod
    def terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.terminate()
