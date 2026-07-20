from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Protocol

from .models import PublishResult, ReportArtifact


class DataSource(Protocol):
    async def preflight(self, query_date: date, auth: Mapping[str, Any] | None) -> Mapping[str, Any]: ...

    async def fetch(self, query_date: date, auth: Mapping[str, Any] | None) -> Mapping[str, Any]: ...


class Publisher(Protocol):
    def publish(self, artifact: ReportArtifact) -> PublishResult: ...


class EventSink(Protocol):
    def emit(self, event: Any) -> None: ...
