from __future__ import annotations

import argparse
import re
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
from autodatareport.atomic_io import atomic_write_yaml  # noqa: E402


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
FEISHU_PUBLISH_KEYS = {
    "enabled", "app_id", "app_secret", "folder_token", "title", "title_prefix",
    "doc_url_prefix", "pc_enabled", "pc_title", "pc_title_prefix", "image_width",
    "narrow_image_width", "tall_ratio_threshold", "prevent_upscale", "verify_content",
    "verify_content_lang", "timeout", "request_retries", "retry_backoff_seconds",
}
WECOM_PUBLISH_KEYS = {
    "enabled", "strict", "bot_id", "secret", "single_userid", "single_chatid",
    "group_chatid", "auto_targets", "ws_url", "open_timeout", "ack_timeout",
    "max_message_length",
}
PUBLISH_ENV_KEYS = {
    "FEISHU_APP_ID": ("feishu_doc", "app_id"),
    "FEISHU_APP_SECRET": ("feishu_doc", "app_secret"),
    "FEISHU_DOC_FOLDER_TOKEN": ("feishu_doc", "folder_token"),
    "WECOM_BOT_ID": ("wecom_bot", "bot_id"),
    "WECOM_BOT_SECRET": ("wecom_bot", "secret"),
}


def sanitize_config(
    config: Mapping[str, Any],
    *,
    include_publish_settings: bool = False,
    publish_env: Mapping[str, str] | None = None,
    publish_revision: int = 1,
) -> dict[str, Any]:
    payload = normalize_company_endpoints(config)
    if not str(payload.get("login_url_870") or "").strip():
        base = str(payload.get("base_url") or "").strip()
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme and parsed.netloc:
            payload["login_url_870"] = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    for path in REQUIRED_PATHS:
        value = _get(payload, path)
        if not str(value or "").strip() or contains_placeholder(value):
            raise ValueError(f"内部分发配置缺少真实地址: {'.'.join(path)}")
    clean = _scrub(payload)
    if include_publish_settings:
        clean["feishu_doc"] = _publish_section(payload, "feishu_doc", FEISHU_PUBLISH_KEYS)
        clean["wecom_bot"] = _publish_section(payload, "wecom_bot", WECOM_PUBLISH_KEYS)
        for env_key, (section, key) in PUBLISH_ENV_KEYS.items():
            value = str((publish_env or {}).get(env_key) or "").strip()
            if value:
                clean.setdefault(section, {})[key] = value
        _validate_publish_settings(clean)
        clean["internal_publish_revision"] = max(1, int(publish_revision))
    else:
        for section in ("feishu_doc", "wecom_bot"):
            if isinstance(clean.get(section), dict):
                clean[section]["enabled"] = False
                for key in tuple(clean[section]):
                    if any(part in key.lower() for part in ("user", "chat", "receiver", "target")):
                        clean[section][key] = [] if isinstance(clean[section][key], list) else ""
    return clean


def _publish_section(config: Mapping[str, Any], section: str, allowed_keys: set[str]) -> dict[str, Any]:
    raw = config.get(section)
    if not isinstance(raw, Mapping):
        return {"enabled": False}
    return {str(key): value for key, value in raw.items() if str(key) in allowed_keys}


def _validate_publish_settings(config: Mapping[str, Any]) -> None:
    feishu = config.get("feishu_doc") if isinstance(config.get("feishu_doc"), Mapping) else {}
    if bool(feishu.get("enabled")) and not (str(feishu.get("app_id") or "").strip() and str(feishu.get("app_secret") or "").strip()):
        raise ValueError("内部发布版已启用飞书，但缺少 FEISHU_APP_ID/FEISHU_APP_SECRET")

    wecom = config.get("wecom_bot") if isinstance(config.get("wecom_bot"), Mapping) else {}
    if bool(wecom.get("enabled")):
        if not (str(wecom.get("bot_id") or "").strip() and str(wecom.get("secret") or "").strip()):
            raise ValueError("内部发布版已启用企微，但缺少 bot_id/secret")
        targets = [str(value).strip() for value in (wecom.get("auto_targets") or []) if str(value).strip()]
        if not targets:
            raise ValueError("内部发布版已启用企微，但 auto_targets 为空")
        for target in targets:
            if target == "single" and not str(wecom.get("single_userid") or wecom.get("single_chatid") or "").strip():
                raise ValueError("企微自动目标包含 single，但缺少 single_userid/single_chatid")
            if target == "group" and not str(wecom.get("group_chatid") or "").strip():
                raise ValueError("企微自动目标包含 group，但缺少 group_chatid")


def load_env_file(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
        if not match:
            continue
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[match.group(1)] = value
    return values


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


def prepare(
    source: Path,
    output_dir: Path,
    *,
    include_publish_settings: bool = False,
    publish_env_file: Path | None = None,
    publish_revision: int = 1,
) -> Path:
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise ValueError("配置根节点必须是对象")
    sanitized = sanitize_config(
        raw,
        include_publish_settings=include_publish_settings,
        publish_env=load_env_file(publish_env_file),
        publish_revision=publish_revision,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "company-defaults.yaml"
    atomic_write_yaml(target, sanitized)
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
    parser.add_argument("--include-publish-settings", action="store_true")
    parser.add_argument("--publish-env-file", type=Path)
    parser.add_argument("--publish-revision", type=int, default=1)
    args = parser.parse_args()
    target = prepare(
        args.source.resolve(),
        args.output_dir.resolve(),
        include_publish_settings=args.include_publish_settings,
        publish_env_file=args.publish_env_file.resolve() if args.publish_env_file else None,
        publish_revision=args.publish_revision,
    )
    print(f"Prepared internal defaults: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
