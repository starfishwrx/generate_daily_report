from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from autodatareport.events import current_metrics
from autodatareport.redaction import redact_sensitive_text
from publish_state import PublishStateStore


@dataclass(frozen=True)
class PublishCallbacks:
    started: Callable[[], None]
    remote_created: Callable[[dict[str, Any]], None]


class PublishCoordinator:
    """Own the durable transition around an irreversible remote publish."""

    def __init__(self, store: PublishStateStore, *, force: bool = False) -> None:
        self.store = store
        self.force = force

    def completed_result(self, target: str, payload_hash: str) -> dict[str, Any] | None:
        self.store.assert_publish_allowed(target, payload_hash, force=self.force)
        if self.force:
            return None
        return self.store.completed_result(target, payload_hash)

    def callbacks(self, target: str, payload_hash: str) -> PublishCallbacks:
        return PublishCallbacks(
            started=lambda: self.started(target, payload_hash),
            remote_created=lambda result: self.remote_created(target, payload_hash, result),
        )

    def started(self, target: str, payload_hash: str) -> None:
        self.store.mark_publishing(target, payload_hash)
        self._record_transition("publishing")

    def remote_created(self, target: str, payload_hash: str, result: dict[str, Any]) -> None:
        self.store.update_remote_result(target, payload_hash, result)

    def completed(self, target: str, payload_hash: str, result: dict[str, Any] | None = None) -> None:
        self.store.mark_completed(target, payload_hash, result)
        self._record_transition("completed")

    def failed(self, target: str, payload_hash: str, error: BaseException | str, *, remote_started: bool) -> None:
        message = redact_sensitive_text(error)
        if remote_started:
            entry = self.store.entry(target, payload_hash)
            result = entry.result if entry is not None else {}
            self.store.mark_uncertain(target, payload_hash, result, message)
            self._record_transition("uncertain")
        else:
            self.store.mark_failed(target, payload_hash, message)
            self._record_transition("failed")

    @staticmethod
    def _record_transition(status: str) -> None:
        metrics = current_metrics()
        if metrics is not None:
            metrics.increment(f"publish_state_{status}")
