from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from config_migration import contains_placeholder


class ReadinessState(str, Enum):
    READY = "ready"
    LOGIN_REQUIRED = "login_required"
    CONFIG_MISSING = "config_missing"
    NETWORK_UNREACHABLE = "network_unreachable"
    PERMISSION_DENIED = "permission_denied"
    UPSTREAM_ERROR = "upstream_error"
    DATA_INVALID = "data_invalid"
    DISABLED = "disabled"
    UNCHECKED = "unchecked"


@dataclass(frozen=True)
class SourceReadiness:
    source: str
    state: ReadinessState
    message: str
    action: str = ""


def classify_failure(message: str) -> ReadinessState:
    text = str(message or "").lower()
    if contains_placeholder(text):
        return ReadinessState.CONFIG_MISSING
    if any(marker in text for marker in (
        "nameresolutionerror", "getaddrinfo failed", "failed to resolve", "name or service not known",
        "connection refused", "connection timed out", "connecttimeout", "proxyerror", "dns",
    )):
        return ReadinessState.NETWORK_UNREACHABLE
    if any(marker in text for marker in ("permission denied", "无权限", "forbidden", "status=403", "status 403")):
        return ReadinessState.PERMISSION_DENIED
    if any(marker in text for marker in (
        "phpsessid", "session cookie", "session_cookie", "请先登录", "登录失效", "登录态",
        "e_token", "admin-token", "bearer", "unauthorized", "status=401", "status 401",
    )):
        return ReadinessState.LOGIN_REQUIRED
    if any(marker in text for marker in ("status=500", "status 500", "status=502", "status=503", "status=504")):
        return ReadinessState.UPSTREAM_ERROR
    if any(marker in text for marker in ("invalid json", "not valid json", "数据格式", "schema")):
        return ReadinessState.DATA_INVALID
    return ReadinessState.UNCHECKED


def validate_configuration(config: Mapping[str, Any]) -> list[SourceReadiness]:
    extra = config.get("extra_metrics") if isinstance(config.get("extra_metrics"), Mapping) else {}
    pc = config.get("pc_web_metrics") if isinstance(config.get("pc_web_metrics"), Mapping) else {}
    checks = (
        ("870", config.get("base_url"), True),
        ("Fenxi", extra.get("fenxi_base"), bool(extra.get("enabled"))),
        ("505", extra.get("manage_base"), bool(extra.get("enabled"))),
        ("PC", pc.get("base"), bool(pc.get("enabled"))),
    )
    results: list[SourceReadiness] = []
    for source, value, enabled in checks:
        if not enabled:
            results.append(SourceReadiness(source, ReadinessState.DISABLED, "未启用"))
        elif not str(value or "").strip() or contains_placeholder(value):
            results.append(SourceReadiness(source, ReadinessState.CONFIG_MISSING, "缺少内部平台配置", "重新安装内部版"))
        else:
            results.append(SourceReadiness(source, ReadinessState.UNCHECKED, "等待连接检查"))
    return results
