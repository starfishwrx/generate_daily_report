from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autodatareport.atomic_io import atomic_write_json  # noqa: E402
import yaml  # noqa: E402


SENSITIVE_KEYS = {
    "session_cookie",
    "php_sessid",
    "phpsessid",
    "authorization",
    "admin-token",
    "admin_token",
    "e_token",
    "app_secret",
    "secret",
    "access_token",
    "webhook",
}


def _placeholder_value(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return bool(
        re.fullmatch(
            r"(?i)\s*(?:(?:PHPSESSID|Bearer)\s*=?)?\s*(?:<[^>]+>|\$\{[^}]+\}|%[A-Z0-9_]+%)\s*",
            value,
        )
    )


def _structured_secret(value: object, key: str = "") -> bool:
    normalized_key = key.strip().lower()
    if normalized_key in SENSITIVE_KEYS and value not in (None, "", False, [], {}) and not _placeholder_value(value):
        return True
    if isinstance(value, dict):
        return any(_structured_secret(item, str(item_key)) for item_key, item in value.items())
    if isinstance(value, list):
        return any(_structured_secret(item) for item in value)
    return False


def contains_sensitive_value(path: Path) -> bool:
    if path.suffix.lower() not in {".yaml", ".yml", ".json", ".env", ".txt", ".log"}:
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    try:
        if path.suffix.lower() == ".json":
            return _structured_secret(json.loads(text))
        if path.suffix.lower() in {".yaml", ".yml"}:
            return _structured_secret(yaml.safe_load(text))
    except (ValueError, yaml.YAMLError):
        pass
    key_pattern = "|".join(re.escape(key) for key in sorted(SENSITIVE_KEYS))
    return bool(re.search(rf"(?im)^\s*(?:{key_pattern})\s*[:=]\s*(?!['\"]?\s*$)(?!<redacted>\s*$).+", text))


def contains_unapproved_internal_secret(path: Path) -> bool:
    """Allow only the two organization publishing secrets in the designated internal defaults file."""

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return True
    if not isinstance(payload, dict):
        return True
    scrubbed = copy.deepcopy(payload)
    for section, key in (("feishu_doc", "app_secret"), ("wecom_bot", "secret")):
        section_value = scrubbed.get(section)
        if isinstance(section_value, dict):
            section_value[key] = ""
    return _structured_secret(scrubbed)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("version", nargs="?", default="dev")
    parser.add_argument("--allow-internal-publish-config", action="store_true")
    args = parser.parse_args()
    release_dir = args.release_dir.resolve()
    version = args.version
    executables = {}
    for path in sorted(release_dir.glob("*.exe")):
        executables[path.name] = {"sha256": sha256(path), "size_bytes": path.stat().st_size}
    files = [path for path in sorted(release_dir.rglob("*")) if path.is_file() and path.name != "release-manifest.json"]
    forbidden_names = {"config.yaml", "extra_auth.json", ".env.scheduler"}
    sensitive_hits = [
        str(path.relative_to(release_dir))
        for path in files
        if path.name.lower() in forbidden_names
        or "auth_repair_logs" in {part.lower() for part in path.parts}
        or "run_metrics" in {part.lower() for part in path.parts}
    ]
    sensitive_value_hits: list[str] = []
    allowed_publish_path = Path("_internal/internal_defaults/company-defaults.yaml")
    for path in files:
        if not contains_sensitive_value(path):
            continue
        relative = path.relative_to(release_dir)
        if (
            args.allow_internal_publish_config
            and relative.as_posix().lower() == allowed_publish_path.as_posix().lower()
            and not contains_unapproved_internal_secret(path)
        ):
            continue
        sensitive_value_hits.append(str(relative))
    sensitive_hits.extend(path for path in sensitive_value_hits if path not in sensitive_hits)
    manifest = {
        "version": version,
        "distribution_profile": "internal-publish" if args.allow_internal_publish_config else "public",
        "internal_publish_config_included": bool(args.allow_internal_publish_config),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "signed": False,
        "executables": executables,
        "file_count": len(files),
        "total_size_bytes": sum(path.stat().st_size for path in files),
        "files": [{"path": str(path.relative_to(release_dir)).replace("\\", "/"), "size_bytes": path.stat().st_size} for path in files],
        "sensitive_scan": {
            "passed": not sensitive_hits,
            "hits": sensitive_hits,
            "field_value_hits": sensitive_value_hits,
        },
    }
    atomic_write_json(release_dir / "release-manifest.json", manifest)
    if sensitive_hits:
        raise SystemExit(f"Release contains forbidden runtime files: {', '.join(sensitive_hits)}")


if __name__ == "__main__":
    main()
