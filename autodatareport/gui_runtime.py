from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class GuiEvent:
    kind: str
    stage: str
    message: str = ""
    progress: int | None = None
    target: str = ""
    url: str = ""
    path: str = ""
    metrics_path: str = ""

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "GuiEvent":
        details = payload.get("details")
        detail_map = details if isinstance(details, Mapping) else {}
        progress = payload.get("progress")
        return cls(
            kind=str(payload.get("kind") or ""),
            stage=str(payload.get("stage") or ""),
            message=str(payload.get("message") or ""),
            progress=int(progress) if isinstance(progress, (int, float)) else None,
            target=str(detail_map.get("target") or ""),
            url=str(detail_map.get("url") or ""),
            path=str(detail_map.get("path") or ""),
            metrics_path=str(detail_map.get("metrics_path") or ""),
        )

    def log_line(self) -> str:
        parts = [part for part in (self.message, self.path, self.url, self.metrics_path) if part]
        return " · ".join(parts) or f"{self.kind}: {self.stage}"


def parse_event_line(line: str) -> GuiEvent | None:
    """Parse the optional V1 event stream and ignore ordinary legacy log lines."""

    if not line.lstrip().startswith("{"):
        return None
    try:
        payload = json.loads(line)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != "autodatareport.event.v1":
        return None
    return GuiEvent.from_payload(payload)
