from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


APP_DIR_NAME = "AutoDataReport"
MIGRATABLE_FILES = (
    "config.yaml",
    "extra_auth.json",
    "hosts_870.yaml",
    "hosts_505.yaml",
    ".env.scheduler",
)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_data_root() -> Path:
    override = str(os.getenv("AUTODATAREPORT_HOME") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if not is_frozen():
        return bundle_root()
    if sys.platform == "win32":
        base = str(os.getenv("LOCALAPPDATA") or "").strip()
        if base:
            return (Path(base) / APP_DIR_NAME).resolve()
    return (Path.home() / ".autodatareport").resolve()


@dataclass(frozen=True)
class AppPaths:
    bundle: Path
    data: Path
    config: Path
    extra_auth: Path
    scheduler_env: Path
    output: Path


def resolve_app_paths(data_dir: Path | str | None = None) -> AppPaths:
    data = Path(data_dir).expanduser().resolve() if data_dir else default_data_root()
    return AppPaths(
        bundle=bundle_root(),
        data=data,
        config=data / "config.yaml",
        extra_auth=data / "extra_auth.json",
        scheduler_env=data / ".env.scheduler",
        output=data / "output",
    )


def migrate_legacy_runtime_files(
    paths: AppPaths,
    *,
    filenames: Iterable[str] = MIGRATABLE_FILES,
) -> list[Path]:
    """Copy legacy runtime files out of the executable directory without deleting them."""
    paths.data.mkdir(parents=True, exist_ok=True)
    migrated: list[Path] = []
    if paths.bundle.resolve() == paths.data.resolve():
        return migrated
    source_roots = [paths.bundle]
    previous_release = paths.bundle.parent / "windows-release"
    if previous_release.exists() and previous_release.resolve() != paths.bundle.resolve():
        source_roots.append(previous_release)
    for filename in filenames:
        target = paths.data / filename
        if target.exists():
            continue
        for source_root in source_roots:
            source = source_root / filename
            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                migrated.append(target)
                break
    return migrated


def ensure_first_run_config(paths: AppPaths) -> Path:
    """Create an editable config from the bundled example when no legacy config exists."""
    if paths.config.exists():
        return paths.config
    candidates = (
        paths.bundle / "config.example.yaml",
        Path(getattr(sys, "_MEIPASS", paths.bundle)) / "config.example.yaml",
    )
    for source in candidates:
        if source.exists():
            paths.data.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, paths.config)
            return paths.config
    return paths.config


def prepare_runtime_config(paths: AppPaths):
    """Create/migrate runtime files, then apply optional internal distribution defaults."""
    migrate_legacy_runtime_files(paths)
    ensure_first_run_config(paths)
    from config_migration import migrate_internal_config

    return migrate_internal_config(paths)
