from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class RunOptions:
    """Stable internal view of the legacy argparse namespace."""

    config: Path
    output: Path
    report_date: str | None
    extra_auth_file: Path
    with_extra_metrics: bool
    no_publish: bool
    force_publish: bool
    max_concurrency: int = 4
    max_total_concurrency: int = 8
    event_stream: str = "text"

    @classmethod
    def from_namespace(cls, args: Any) -> "RunOptions":
        return cls(
            config=Path(args.config),
            output=Path(args.output),
            report_date=getattr(args, "date", None),
            extra_auth_file=Path(args.extra_auth_file),
            with_extra_metrics=bool(getattr(args, "with_extra_metrics", False)),
            no_publish=bool(getattr(args, "no_publish", False)),
            force_publish=bool(getattr(args, "force_publish", False)),
            max_concurrency=int(getattr(args, "max_concurrency", 4)),
            max_total_concurrency=int(getattr(args, "max_total_concurrency", 8)),
            event_stream=str(getattr(args, "event_stream", "text") or "text"),
        )


@dataclass(frozen=True)
class AppConfig:
    """Typed boundary around the backwards-compatible YAML mapping."""

    raw: Mapping[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def section(self, key: str) -> dict[str, Any]:
        value = self.raw.get(key) or {}
        if not isinstance(value, Mapping):
            raise TypeError(f"Config section {key!r} must be a mapping.")
        return dict(value)


@dataclass
class RunContext:
    options: RunOptions
    config: AppConfig
    report_date: date
    output_dir: Path
    charts_dir: Path
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    shared: dict[str, Any] = field(default_factory=dict)


class FailureKind(str, Enum):
    CONFIG_MISSING = "config_missing"
    NETWORK_UNREACHABLE = "network_unreachable"
    LOGIN_REQUIRED = "login_required"
    PERMISSION_DENIED = "permission_denied"
    UPSTREAM_ERROR = "upstream_error"
    DATA_INVALID = "data_invalid"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class SourceError(RuntimeError):
    def __init__(self, source: str, kind: FailureKind, message: str, *, action: str = "") -> None:
        self.source = source
        self.kind = kind
        self.action = action
        super().__init__(message)


@dataclass
class PreflightSnapshot:
    source: str
    ok: bool
    message: str = ""
    reusable_responses: dict[str, Any] = field(default_factory=dict)
    module_switch_completed: bool = False


@dataclass(frozen=True)
class PublishIntent:
    target: str
    content_hash: str
    report_date: date


class PublishResolution(str, Enum):
    COMPLETED = "completed"
    RETRY = "retry"
    HOLD = "hold"


@dataclass
class RunOutcome:
    status: str
    artifacts: dict[str, Path] = field(default_factory=dict)
    publish_urls: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    stage_results: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StageEvent:
    kind: str
    stage: str
    message: str = ""
    progress: int | None = None
    duration_seconds: float | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": "autodatareport.event.v1",
            "kind": self.kind,
            "stage": self.stage,
            "message": self.message,
            "timestamp": self.timestamp,
        }
        if self.progress is not None:
            payload["progress"] = max(0, min(100, int(self.progress)))
        if self.duration_seconds is not None:
            payload["duration_seconds"] = round(float(self.duration_seconds), 6)
        if self.details:
            payload["details"] = dict(self.details)
        return payload


@dataclass(frozen=True)
class ReportArtifact:
    kind: str
    path: Path
    image_paths: Mapping[str, str] = field(default_factory=dict)
    content_hash: str = ""


@dataclass(frozen=True)
class PublishResult:
    target: str
    status: str
    url: str = ""
    message_count: int = 0
    skipped: bool = False


@dataclass
class ExtraStageResult:
    data: dict[str, Any] = field(default_factory=dict)
    rendered_block: str | None = None
    payment_images: dict[str, str] = field(default_factory=dict)


@dataclass
class PCStageResult:
    notes: dict[str, str] = field(default_factory=dict)
    member_summary: dict[str, str] = field(default_factory=dict)
    top_games: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
