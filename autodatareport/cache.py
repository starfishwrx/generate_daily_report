from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Iterable


def hash_payload(parts: Iterable[Any]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        if isinstance(part, bytes):
            payload = part
        else:
            payload = json.dumps(part, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


class ArtifactCache:
    """Small content-addressed manifest for deterministic rendered artifacts."""

    def __init__(self, output_dir: Path) -> None:
        self.path = Path(output_dir) / ".artifact_cache_v1.json"
        self._lock = threading.RLock()
        self.entries: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def is_fresh(self, key: str, input_hash: str, outputs: Iterable[Path]) -> bool:
        with self._lock:
            entry = self.entries.get(key) or {}
            return entry.get("input_hash") == input_hash and all(Path(path).exists() for path in outputs)

    def update(self, key: str, input_hash: str, outputs: Iterable[Path]) -> None:
        with self._lock:
            self.entries[key] = {
                "input_hash": input_hash,
                "outputs": [str(Path(path)) for path in outputs],
            }

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
            temp_path.write_text(json.dumps(self.entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temp_path, self.path)
