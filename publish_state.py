from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def content_hash(parts: Iterable[str | bytes | Path]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        if isinstance(part, Path):
            digest.update(str(part.name).encode("utf-8"))
            if part.exists() and part.is_file():
                with part.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
        elif isinstance(part, bytes):
            digest.update(part)
        else:
            digest.update(str(part).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


class PublishStateStore:
    def __init__(self, output_dir: Path, report_date: date) -> None:
        self.report_date = report_date
        self.state_dir = Path(output_dir) / "publish_state"
        self.path = self.state_dir / f"{report_date.strftime('%Y%m%d')}.json"

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"date": self.report_date.isoformat(), "targets": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"date": self.report_date.isoformat(), "targets": {}}
        if not isinstance(payload, dict):
            return {"date": self.report_date.isoformat(), "targets": {}}
        if not isinstance(payload.get("targets"), dict):
            payload["targets"] = {}
        return payload

    def completed_result(self, target: str, payload_hash: str) -> dict[str, Any] | None:
        entry = (self._load().get("targets") or {}).get(target)
        if not isinstance(entry, dict):
            return None
        if entry.get("status") != "completed" or entry.get("content_hash") != payload_hash:
            return None
        result = entry.get("result")
        return dict(result) if isinstance(result, dict) else {}

    def mark_completed(self, target: str, payload_hash: str, result: dict[str, Any] | None = None) -> None:
        payload = self._load()
        targets = payload.setdefault("targets", {})
        targets[target] = {
            "status": "completed",
            "content_hash": payload_hash,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "result": dict(result or {}),
        }
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f".tmp-{os.getpid()}")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_path, self.path)
