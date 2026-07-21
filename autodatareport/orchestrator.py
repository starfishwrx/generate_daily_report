from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Awaitable, Callable

from autodatareport.models import ReportArtifact, RunContext, RunOutcome
from autodatareport.events import emit_event


AsyncStage = Callable[[RunContext], Awaitable[Any]]


def _artifact_paths(render_result: Any) -> dict[str, Path]:
    if not isinstance(render_result, dict):
        return {}
    artifacts: dict[str, Path] = {}
    for key, value in render_result.items():
        if isinstance(value, ReportArtifact):
            artifacts[key] = value.path
        elif key == "output_path" and value is not None:
            artifacts["main"] = Path(value)
        elif key == "pc_report_path" and value is not None:
            artifacts["pc"] = Path(value)
    return artifacts


@dataclass(frozen=True)
class PipelineStages:
    authenticate: AsyncStage
    collect: AsyncStage
    calculate: AsyncStage
    render: AsyncStage
    publish: AsyncStage


class RunPipeline:
    """Five-stage application boundary used by the legacy CLI facade."""

    def __init__(self, stages: PipelineStages) -> None:
        self.stages = stages

    async def run(self, context: RunContext) -> RunOutcome:
        stage_results: dict[str, Any] = {}
        for name, stage in (
            ("authenticate", self.stages.authenticate),
            ("collect", self.stages.collect),
            ("calculate", self.stages.calculate),
            ("render", self.stages.render),
            ("publish", self.stages.publish),
        ):
            started = time.perf_counter()
            emit_event("stage_started", name, f"{name}阶段开始")
            stage_results[name] = await stage(context)
            context.shared[name] = stage_results[name]
            emit_event("stage_finished", name, f"{name}阶段完成", duration_seconds=time.perf_counter() - started)
        publish = stage_results.get("publish") if isinstance(stage_results.get("publish"), dict) else {}
        return RunOutcome(
            status="ok",
            artifacts=_artifact_paths(stage_results.get("render")),
            publish_urls={key: str(value) for key, value in publish.items()},
            stage_results=stage_results,
        )
