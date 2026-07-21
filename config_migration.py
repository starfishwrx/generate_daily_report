from __future__ import annotations

import copy
import re
import shutil
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import yaml

from app_paths import AppPaths
from autodatareport.atomic_io import atomic_write_text, atomic_write_yaml


SCHEMA_VERSION = "1.5"
PLACEHOLDER_RE = re.compile(r"(?:<[^>]+>|%3c[^%]+%3e|your[_-]|replace[_-]?me|example\.com)", re.I)
PERSONAL_TOP_LEVEL_KEYS = {"session_cookie", "feishu_doc", "wecom_bot", "schedule"}
HTTPS_ONLY_HOSTS = {"admin.buke999.com"}


@dataclass(frozen=True)
class ConfigMigrationResult:
    changed: bool = False
    installed_internal_defaults: bool = False
    backup_path: Path | None = None
    message: str = ""
    changed_paths: tuple[str, ...] = ()


def contains_placeholder(value: object) -> bool:
    return bool(PLACEHOLDER_RE.search(str(value or "")))


def normalize_company_endpoints(config: Mapping[str, Any]) -> dict[str, Any]:
    """Upgrade endpoints that no longer support authenticated HTTP sessions."""
    payload = copy.deepcopy(dict(config))
    for key in ("base_url", "login_url_870"):
        value = str(payload.get(key) or "").strip()
        parsed = urllib.parse.urlsplit(value)
        if parsed.hostname and parsed.hostname.lower() in HTTPS_ONLY_HOSTS:
            if key == "login_url_870":
                payload[key] = urllib.parse.urlunsplit(("https", parsed.netloc, "", "", ""))
            elif parsed.scheme.lower() == "http":
                payload[key] = urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    return payload


def find_internal_defaults(paths: AppPaths) -> Path | None:
    roots = (paths.bundle, Path(getattr(sys, "_MEIPASS", paths.bundle)))
    for root in roots:
        candidate = root / "internal_defaults" / "company-defaults.yaml"
        if candidate.is_file():
            return candidate
    return None


def migrate_internal_config(paths: AppPaths) -> ConfigMigrationResult:
    """Overlay company-managed defaults while preserving each user's credentials/settings."""
    defaults_path = find_internal_defaults(paths)
    if defaults_path is None:
        return ConfigMigrationResult(message="未找到内部分发默认配置。")

    defaults = normalize_company_endpoints(_load_yaml(defaults_path))
    current = _load_yaml(paths.config) if paths.config.exists() else {}
    merged = _overlay_managed_defaults(current, defaults)
    for key in PERSONAL_TOP_LEVEL_KEYS:
        if key in current:
            merged[key] = copy.deepcopy(current[key])

    cookie = str(current.get("session_cookie") or "").strip()
    merged["session_cookie"] = "" if contains_placeholder(cookie) else cookie
    merged["config_schema_version"] = SCHEMA_VERSION

    changed = merged != current
    changed_paths = tuple(_changed_paths(current, merged))
    backup: Path | None = None
    if changed:
        paths.data.mkdir(parents=True, exist_ok=True)
        if paths.config.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = paths.config.with_name(f"config.pre-v15.{stamp}.yaml")
            shutil.copy2(paths.config, backup)
        atomic_write_yaml(paths.config, merged)

    _install_hosts(defaults_path.parent, paths)
    return ConfigMigrationResult(
        changed=changed,
        installed_internal_defaults=True,
        backup_path=backup,
        message="内部平台配置已安装。" if changed else "内部平台配置已是最新。",
        changed_paths=changed_paths,
    )


def _overlay_managed_defaults(current: Mapping[str, Any], defaults: Mapping[str, Any]) -> dict[str, Any]:
    """Let shipped fields win while retaining user-owned fields unknown to the bundle."""

    merged = copy.deepcopy(dict(current))
    for key, default_value in defaults.items():
        if key in PERSONAL_TOP_LEVEL_KEYS:
            continue
        current_value = merged.get(key)
        if isinstance(current_value, Mapping) and isinstance(default_value, Mapping):
            merged[key] = _overlay_managed_defaults(current_value, default_value)
        else:
            merged[key] = copy.deepcopy(default_value)
    return merged


def _changed_paths(before: Mapping[str, Any], after: Mapping[str, Any], prefix: str = "") -> list[str]:
    changed: list[str] = []
    for key in sorted(set(before) | set(after)):
        path = f"{prefix}.{key}" if prefix else str(key)
        left = before.get(key)
        right = after.get(key)
        if isinstance(left, Mapping) and isinstance(right, Mapping):
            changed.extend(_changed_paths(left, right, path))
        elif left != right:
            changed.append(path)
    return changed


def _install_hosts(source_dir: Path, paths: AppPaths) -> None:
    for source_name, target_name in (
        ("company-hosts-870.yaml", "hosts_870.yaml"),
        ("company-hosts-505.yaml", "hosts_505.yaml"),
    ):
        source = source_dir / source_name
        target = paths.data / target_name
        if source.is_file() and (not target.exists() or source.read_bytes() != target.read_bytes()):
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(target, source.read_text(encoding="utf-8"))


def _load_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"配置根节点必须是对象: {path}")
    return dict(raw)
