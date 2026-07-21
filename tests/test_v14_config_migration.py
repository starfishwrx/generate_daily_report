from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app_paths import AppPaths
from config_migration import migrate_internal_config, normalize_company_endpoints
from scripts.prepare_internal_defaults import sanitize_config


def _paths(tmp_path: Path) -> AppPaths:
    bundle = tmp_path / "bundle"
    data = tmp_path / "data"
    bundle.mkdir()
    data.mkdir()
    return AppPaths(
        bundle=bundle,
        data=data,
        config=data / "config.yaml",
        extra_auth=data / "extra_auth.json",
        scheduler_env=data / ".env.scheduler",
        output=data / "output",
    )


def _internal_defaults() -> dict:
    return {
        "base_url": "http://870.internal/?m=sdk",
        "login_url_870": "http://870.internal/login",
        "session_cookie": "",
        "targets": {"total": {"queries": [{"params": {"game_type": 0}}]}},
        "extra_metrics": {
            "enabled": True,
            "fenxi_base": "https://fenxi.internal",
            "manage_base": "http://manage.internal",
        },
        "pc_web_metrics": {
            "enabled": True,
            "base": "http://pc-api.internal",
            "web_origin": "http://pc.internal",
        },
    }


def test_internal_defaults_replace_placeholders_and_preserve_personal_state(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    internal_dir = paths.bundle / "internal_defaults"
    internal_dir.mkdir()
    (internal_dir / "company-defaults.yaml").write_text(
        yaml.safe_dump(_internal_defaults(), sort_keys=False), encoding="utf-8"
    )
    paths.config.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://<YOUR_870_HOST>/",
                "session_cookie": "PHPSESSID=user-cookie",
                "feishu_doc": {"enabled": True, "app_id": "personal"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = migrate_internal_config(paths)
    migrated = yaml.safe_load(paths.config.read_text(encoding="utf-8"))
    assert result.changed is True
    assert result.backup_path and result.backup_path.exists()
    assert migrated["base_url"] == "http://870.internal/?m=sdk"
    assert migrated["session_cookie"] == "PHPSESSID=user-cookie"
    assert migrated["feishu_doc"]["app_id"] == "personal"
    assert migrated["config_schema_version"] == "1.4"

    second = migrate_internal_config(paths)
    assert second.changed is False


def test_placeholder_cookie_is_not_preserved(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    internal_dir = paths.bundle / "internal_defaults"
    internal_dir.mkdir()
    (internal_dir / "company-defaults.yaml").write_text(
        yaml.safe_dump(_internal_defaults(), sort_keys=False), encoding="utf-8"
    )
    paths.config.write_text("session_cookie: PHPSESSID=<YOUR_SESSION_ID>\n", encoding="utf-8")
    migrate_internal_config(paths)
    assert yaml.safe_load(paths.config.read_text(encoding="utf-8"))["session_cookie"] == ""


def test_internal_distribution_scrubs_secrets_and_rejects_placeholders() -> None:
    config = _internal_defaults()
    config.update(
        {
            "session_cookie": "PHPSESSID=secret",
            "feishu_doc": {"app_id": "id", "app_secret": "secret", "folder_token": "folder"},
            "wecom_bot": {"enabled": True, "receiver_user_ids": ["alice"], "access_token": "token"},
        }
    )
    clean = sanitize_config(config)
    assert clean["session_cookie"] == ""
    assert clean["feishu_doc"] == {"app_id": "", "app_secret": "", "folder_token": "", "enabled": False}
    assert clean["wecom_bot"]["enabled"] is False
    assert clean["wecom_bot"]["receiver_user_ids"] == []
    assert clean["wecom_bot"]["access_token"] == ""

    config["base_url"] = "http://<YOUR_870_HOST>/"
    with pytest.raises(ValueError, match="base_url"):
        sanitize_config(config)


def test_known_870_endpoint_is_upgraded_to_https() -> None:
    config = {
        "base_url": "http://admin.buke999.com/?m=sdk&ac=getToolPre",
        "login_url_870": "http://admin.buke999.com/?m=user&ac=login",
    }
    normalized = normalize_company_endpoints(config)
    assert normalized["base_url"].startswith("https://")
    assert normalized["login_url_870"].startswith("https://")
