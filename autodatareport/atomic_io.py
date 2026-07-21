from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import yaml


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Durably replace a small runtime file without exposing partial contents."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        with temp.open("w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def atomic_write_yaml(path: Path, payload: Any) -> None:
    atomic_write_text(path, yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))
