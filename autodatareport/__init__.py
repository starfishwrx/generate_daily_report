"""AutoDataReport modular runtime introduced in V1.3."""

from .models import (
    AppConfig,
    ExtraStageResult,
    PCStageResult,
    PublishResult,
    ReportArtifact,
    RunContext,
    RunOptions,
    StageEvent,
)

__all__ = [
    "AppConfig",
    "ExtraStageResult",
    "PCStageResult",
    "PublishResult",
    "ReportArtifact",
    "RunContext",
    "RunOptions",
    "StageEvent",
]

__version__ = "1.5.0"
