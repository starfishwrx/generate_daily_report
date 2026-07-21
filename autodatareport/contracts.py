from __future__ import annotations

from typing import Any, Protocol

from autodatareport.models import PreflightSnapshot, ReportArtifact, RunContext


class SourceAdapter(Protocol):
    async def preflight(self, context: RunContext) -> PreflightSnapshot: ...

    async def fetch(self, context: RunContext, snapshot: PreflightSnapshot | None = None) -> Any: ...


class MetricsCalculator(Protocol):
    def calculate(self, context: RunContext, source_data: dict[str, Any]) -> dict[str, Any]: ...


class ReportRenderer(Protocol):
    def render(self, context: RunContext, metrics: dict[str, Any]) -> dict[str, ReportArtifact]: ...


class ArtifactPublisher(Protocol):
    def publish(self, context: RunContext, artifacts: dict[str, ReportArtifact]) -> dict[str, Any]: ...
