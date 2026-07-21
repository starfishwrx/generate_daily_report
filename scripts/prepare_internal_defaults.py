from __future__ import annotations

import argparse
import shutil
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_migration import contains_placeholder, normalize_company_endpoints  # noqa: E402


REQUIRED_PATHS = (
    ("base_url",),
    ("login_url_870",),
    ("extra_metrics", "fenxi_base"),
    ("extra_metrics", "manage_base"),
    ("pc_web_metrics", "base"),
    ("pc_web_metrics", "web_origin"),
)
SECRET_KEYS = {
    "session_cookie", "app_id", "app_secret", "folder_token", "bot_id", "bot_secret",
    "webhook", "authorization", "bearer", "password", "token", "secret",
}
SECRET_KEY_PARTS = ("password", "secret", "authorization", "cookie", "token", "bearer", "webhook", "app_id", "bot_id")


def sanitize_config(config: Mapping[str, Any]) -> dict[str, Any]:
    payload = normalize_company_endpoints(config)
    if not str(payload.get("login_url_870") or "").strip():
        base = str(payload.get("base_url") or "").strip()
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme and parsed.netloc:
            payload["login_url_870"] = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "m=user&ac=login", ""))
    for path in REQUIRED_PATHS:
        value = _get(payload, path)
        if not str(value or "").strip() or contains_placeholder(value):
            raise ValueError(f"内部分发配置缺少真实地址: {'.'.join(path)}")
    clean = _scrub(payload)
    for section in ("feishu_doc", "wecom_bot"):
        if isinstance(clean.get(section), dict):
            clean[section]["enabled"] = False
            for key in tuple(clean[section]):
                if any(part in key.lower() for part in ("user", "chat", "receiver", "target")):
                    clean[section][key] = [] if isinstance(clean[section][key], list) else ""
    return clean


def _scrub(value: Any, key: str = "") -> Any:
    if isinstance(value, Mapping):
        return {str(k): _scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    lowered = key.lower()
    if lowered in SECRET_KEYS or any(part in lowered for part in SECRET_KEY_PARTS):
        return ""
    return value


def _get(config: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = config
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def prepare(source: Path, output_dir: Path) -> Path:
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise ValueError("配置根节点必须是对象")
    sanitized = sanitize_config(raw)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "company-defaults.yaml"
    target.write_text(yaml.safe_dump(sanitized, allow_unicode=True, sort_keys=False), encoding="utf-8")
    source_dir = source.parent
    network = raw.get("network") if isinstance(raw.get("network"), Mapping) else {}
    extra = raw.get("extra_metrics") if isinstance(raw.get("extra_metrics"), Mapping) else {}
    for configured, output_name, fallback_name in (
        (network.get("hosts_yaml_path"), "company-hosts-870.yaml", "hosts_870.yaml"),
        (extra.get("hosts_yaml_path"), "company-hosts-505.yaml", "hosts_505.yaml"),
    ):
        if configured:
            candidate = (source_dir / str(configured)).resolve()
            if not candidate.is_file():
                candidate = source_dir / fallback_name
            if candidate.is_file():
                shutil.copy2(candidate, output_dir / output_name)
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    target = prepare(args.source.resolve(), args.output_dir.resolve())
    print(f"Prepared internal defaults: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
