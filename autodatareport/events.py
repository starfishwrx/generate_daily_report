from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, TextIO

from .models import StageEvent
from .atomic_io import atomic_write_json


class JsonlEventSink:
    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout
        self._lock = threading.Lock()

    def emit(self, event: StageEvent) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.stream.write(line + "\n")
            self.stream.flush()


class RuntimeTelemetry:
    def __init__(self) -> None:
        self.sink: JsonlEventSink | None = None
        self.metrics: RunMetricsRecorder | None = None

    def configure(self, *, event_stream: str, metrics: "RunMetricsRecorder") -> None:
        self.sink = JsonlEventSink() if event_stream == "jsonl" else None
        self.metrics = metrics

    def reset(self) -> None:
        self.sink = None
        self.metrics = None


class RunMetricsRecorder:
    def __init__(self, output_dir: Path, report_date: str | None = None) -> None:
        self.output_dir = Path(output_dir)
        self.report_date = str(report_date or "")
        self.started_at = datetime.now(timezone.utc)
        self._started_perf = time.perf_counter()
        self.stages: dict[str, dict[str, Any]] = {}
        self.counters: dict[str, int] = {}
        self._lock = threading.Lock()
        stamp = self.started_at.strftime("%Y%m%d_%H%M%S_%f")
        self.path = self.output_dir / "run_metrics" / f"run_{stamp}.json"

    def record_stage(self, name: str, duration_seconds: float, **details: Any) -> None:
        item: dict[str, Any] = {"duration_seconds": round(float(duration_seconds), 6)}
        if details:
            item.update(details)
        with self._lock:
            self.stages[name] = item

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + int(amount)

    @contextmanager
    def stage(self, name: str, **details: Any) -> Iterator[None]:
        started = time.perf_counter()
        emit_event("stage_started", name, details=details)
        try:
            yield
        except Exception as exc:
            elapsed = time.perf_counter() - started
            self.record_stage(name, elapsed, status="error", error_type=type(exc).__name__, **details)
            emit_event("stage_finished", name, duration_seconds=elapsed, details={"status": "error"})
            raise
        else:
            elapsed = time.perf_counter() - started
            self.record_stage(name, elapsed, status="ok", **details)
            emit_event("stage_finished", name, duration_seconds=elapsed, details={"status": "ok"})

    def finalize(self, *, status: str, error: str = "") -> Path:
        finished_at = datetime.now(timezone.utc)
        payload = {
            "schema": "autodatareport.run_metrics.v1",
            "report_date": self.report_date,
            "status": status,
            "error": error,
            "started_at": self.started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "total_seconds": round(time.perf_counter() - self._started_perf, 6),
            "stages": self.stages,
            "counters": self.counters,
        }
        atomic_write_json(self.path, payload)
        return self.path


_RUNTIME = RuntimeTelemetry()


def configure_runtime_telemetry(*, event_stream: str, metrics: RunMetricsRecorder) -> None:
    _RUNTIME.configure(event_stream=event_stream, metrics=metrics)


def reset_runtime_telemetry() -> None:
    _RUNTIME.reset()


def current_metrics() -> RunMetricsRecorder | None:
    return _RUNTIME.metrics


def emit_event(
    kind: str,
    stage: str,
    message: str = "",
    *,
    progress: int | None = None,
    duration_seconds: float | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    sink = _RUNTIME.sink
    if sink is None:
        return
    sink.emit(
        StageEvent(
            kind=kind,
            stage=stage,
            message=message,
            progress=progress,
            duration_seconds=duration_seconds,
            details=details or {},
        )
    )
