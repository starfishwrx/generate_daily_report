from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from autodatareport.atomic_io import atomic_write_json
from autodatareport.models import PublishResolution


PUBLISH_STATE_SCHEMA = "autodatareport.publish-state.v2"


class PublishStatus(str, Enum):
    PENDING = "pending"
    PUBLISHING = "publishing"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    COMPLETED = "completed"


@dataclass(frozen=True)
class PublishStateEntry:
    target: str
    status: PublishStatus
    content_hash: str
    result: dict[str, Any]
    error: str = ""


class UncertainPublishError(RuntimeError):
    def __init__(self, target: str, entry: PublishStateEntry) -> None:
        self.target = target
        self.entry = entry
        url = str(entry.result.get("url") or "").strip()
        suffix = f" 可先检查：{url}" if url else ""
        super().__init__(f"{target} 上次发送结果待确认，已阻止自动重发。{suffix} 如确认未发送，请显式使用 --force-publish。")


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
        self._lock = threading.RLock()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._empty()
        if not isinstance(payload, dict):
            return self._empty()
        if not isinstance(payload.get("targets"), dict):
            payload["targets"] = {}
        payload.setdefault("schema", PUBLISH_STATE_SCHEMA)
        return payload

    def _empty(self) -> dict[str, Any]:
        return {"schema": PUBLISH_STATE_SCHEMA, "date": self.report_date.isoformat(), "targets": {}}

    def entry(self, target: str, payload_hash: str = "") -> PublishStateEntry | None:
        raw = (self._load().get("targets") or {}).get(target)
        if not isinstance(raw, dict):
            return None
        content_hash = str(raw.get("content_hash") or "")
        if payload_hash and content_hash != payload_hash:
            return None
        status_text = str(raw.get("status") or "")
        try:
            status = PublishStatus(status_text)
        except ValueError:
            return None
        return PublishStateEntry(
            target=target,
            status=status,
            content_hash=content_hash,
            result=dict(raw.get("result") or {}),
            error=str(raw.get("error") or ""),
        )

    def completed_result(self, target: str, payload_hash: str) -> dict[str, Any] | None:
        entry = self.entry(target, payload_hash)
        if entry is None or entry.status is not PublishStatus.COMPLETED:
            return None
        return dict(entry.result)

    def assert_publish_allowed(self, target: str, payload_hash: str, *, force: bool = False) -> None:
        entry = self.entry(target, payload_hash)
        if entry is not None and entry.status is PublishStatus.PUBLISHING:
            self.mark_uncertain(target, payload_hash, entry.result, "上次发送在完成确认前中断")
            entry = self.entry(target, payload_hash)
        if entry is not None and entry.status is PublishStatus.UNCERTAIN and not force:
            raise UncertainPublishError(target, entry)

    def mark_publishing(self, target: str, payload_hash: str, result: dict[str, Any] | None = None) -> None:
        self._update(target, PublishStatus.PUBLISHING, payload_hash, result=result)

    def update_remote_result(self, target: str, payload_hash: str, result: dict[str, Any]) -> None:
        with self._lock:
            entry = self.entry(target, payload_hash)
            status = entry.status if entry is not None else PublishStatus.PUBLISHING
            merged = dict(entry.result if entry is not None else {})
            merged.update(result)
            self._update(target, status, payload_hash, result=merged, error=entry.error if entry else "")

    def mark_failed(self, target: str, payload_hash: str, error: str) -> None:
        self._update(target, PublishStatus.FAILED, payload_hash, error=error)

    def mark_uncertain(
        self,
        target: str,
        payload_hash: str,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        self._update(target, PublishStatus.UNCERTAIN, payload_hash, result=result, error=error)

    def mark_completed(self, target: str, payload_hash: str, result: dict[str, Any] | None = None) -> None:
        self._update(target, PublishStatus.COMPLETED, payload_hash, result=result)

    def resolve_uncertain(self, target: str, resolution: str | PublishResolution) -> None:
        entry = self.entry(target)
        if entry is None or entry.status is not PublishStatus.UNCERTAIN:
            return
        resolution = PublishResolution(resolution)
        if resolution is PublishResolution.COMPLETED:
            self.mark_completed(target, entry.content_hash, entry.result)
            return
        if resolution is PublishResolution.RETRY:
            self.mark_failed(target, entry.content_hash, "用户确认远端未发送，允许重试")
            return
        if resolution is PublishResolution.HOLD:
            return
        raise ValueError(f"Unknown publish resolution: {resolution}")

    def uncertain_entries(self) -> list[PublishStateEntry]:
        payload = self._load()
        entries: list[PublishStateEntry] = []
        for target in list((payload.get("targets") or {}).keys()):
            entry = self.entry(str(target))
            if entry is not None and entry.status is PublishStatus.PUBLISHING:
                self.mark_uncertain(entry.target, entry.content_hash, entry.result, "上次发送在完成确认前中断")
                entry = self.entry(entry.target)
            if entry is not None and entry.status is PublishStatus.UNCERTAIN:
                entries.append(entry)
        return entries

    def _update(
        self,
        target: str,
        status: PublishStatus,
        payload_hash: str,
        *,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        with self._lock:
            payload = self._load()
            payload["schema"] = PUBLISH_STATE_SCHEMA
            targets = payload.setdefault("targets", {})
            now = datetime.now(timezone.utc).isoformat()
            targets[target] = {
                "status": status.value,
                "content_hash": payload_hash,
                "updated_at": now,
                "result": dict(result or {}),
            }
            if status is PublishStatus.COMPLETED:
                targets[target]["completed_at"] = now
            if error:
                targets[target]["error"] = error
            payload["updated_at"] = now
            self.state_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path, payload)
