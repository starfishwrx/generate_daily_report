from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import unquote, urlsplit

TOKEN_RE = re.compile(r"access_token=([^&\"'\s<>]+)")


def _extract_token(text: str) -> Optional[str]:
    if not text:
        return None
    match = TOKEN_RE.search(unquote(text))
    if match:
        return match.group(1)
    return None


def _parse_cookie_header(value: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for segment in value.split(";"):
        part = segment.strip()
        if not part or "=" not in part:
            continue
        name, raw_val = part.split("=", 1)
        key = name.strip()
        if key:
            out[key] = raw_val.strip()
    return out


def _decode_jwt_payload(token: str) -> Optional[Dict[str, Any]]:
    text = str(token or "").strip()
    if not text:
        return None
    parts = text.split(".")
    if len(parts) != 3:
        return None
    payload_part = parts[1]
    payload_part += "=" * (-len(payload_part) % 4)
    try:
        payload_bytes = base64.urlsafe_b64decode(payload_part.encode("utf-8"))
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _successful_har_entry(entry: Dict[str, Any]) -> bool:
    try:
        status = int(entry.get("response", {}).get("status"))
    except (TypeError, ValueError):
        return False
    return 200 <= status < 400


def _har_entry_matches_platform(entry: Dict[str, Any], platform: str) -> bool:
    request = entry.get("request", {})
    request_url = str(request.get("url") or "")
    urls = [request_url]
    for header in entry.get("response", {}).get("headers", []):
        if str(header.get("name") or "").lower() == "location":
            urls.append(str(header.get("value") or ""))

    normalized_urls = [(value, urlsplit(unquote(value))) for value in urls if value]
    if platform == "fenxi":
        return any(
            item.hostname == "fenxi.4399dev.com"
            or "/event-analysis-server/" in item.path
            or "qz4399doc" in unquote(value).lower()
            for value, item in normalized_urls
        )
    if platform == "pc_web":
        return any(item.hostname in {"yapiadmin.4399.com", "yadmin.4399.com"} for _, item in normalized_urls)
    if platform == "505":
        request_cookie_names = {
            str(cookie.get("name") or "").lower() for cookie in request.get("cookies", [])
        }
        return (
            any("manage505" in unquote(value).lower() for value in urls)
            or any(item.path.startswith("/pay/") for _, item in normalized_urls)
            or any(name.startswith("__manage_") for name in request_cookie_names)
        )
    return False


def _collect_auth_data(har_paths: Iterable[Path], platform: str) -> Dict[str, Any]:
    cookies: Dict[str, str] = {}
    headers: Dict[str, str] = {}
    token_candidates: List[str] = []
    bootstrap_candidates: List[str] = []
    last_pc_any_bearer = ""
    last_pc_yapi_bearer = ""
    last_pc_game_start_bearer = ""
    last_pc_yapi_nonzero_chain_bearer = ""
    last_pc_any_authorization = ""
    last_pc_yapi_authorization = ""
    last_pc_game_start_authorization = ""

    def _extract_chain(raw_header: str) -> Optional[int]:
        text = str(raw_header or "")
        match = re.search(r"(?:^|[&?])chain=(\d+)(?:$|[&])", text)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    def _extract_pc_token_from_bearer(raw_header: str) -> str:
        text = str(raw_header or "")
        match = re.search(r"(?:^|[&?])token=([^&]+)(?:$|[&])", text)
        if not match:
            return ""
        return match.group(1).strip()

    for path in har_paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        for entry in data.get("log", {}).get("entries", []):
            if not _successful_har_entry(entry) or not _har_entry_matches_platform(entry, platform):
                continue
            req = entry.get("request", {})
            req_url = str(req.get("url") or "")
            req_headers = {str(h.get("name", "")): str(h.get("value", "")) for h in req.get("headers", [])}
            req_headers_lower = {k.lower(): v for k, v in req_headers.items()}

            for item in req.get("cookies", []):
                name = str(item.get("name") or "").strip()
                value = str(item.get("value") or "").strip()
                if name and value:
                    cookies[name] = value

            cookie_header = req_headers.get("Cookie") or req_headers.get("cookie")
            if cookie_header:
                cookies.update(_parse_cookie_header(cookie_header))

            header_token = (
                req_headers.get("X-Access-Token")
                or req_headers.get("x-access-token")
                or req_headers_lower.get("x-access-token")
            )
            if header_token:
                token_candidates.append(header_token.strip())

            if platform == "fenxi":
                if "event-analysis-server" in req_url:
                    if req_headers.get("mediaids"):
                        headers["mediaids"] = req_headers["mediaids"]
                    if req_headers.get("topic"):
                        headers["topic"] = req_headers["topic"]
                    if req_headers.get("Mediaids") and "mediaids" not in headers:
                        headers["mediaids"] = req_headers["Mediaids"]
                    if req_headers.get("Topic") and "topic" not in headers:
                        headers["topic"] = req_headers["Topic"]
                token = _extract_token(req_url)
                if token:
                    token_candidates.append(token)
                    bootstrap_candidates.append(req_url)

            if platform == "505":
                token = _extract_token(req_url)
                if token:
                    token_candidates.append(token)
                    bootstrap_candidates.append(req_url)

            if platform == "pc_web":
                lowered_url = req_url.lower()
                if "yapiadmin.4399.com" in lowered_url or "yadmin.4399.com" in lowered_url:
                    if req_headers.get("Origin"):
                        headers["Origin"] = req_headers["Origin"]
                    if req_headers.get("Referer"):
                        headers["Referer"] = req_headers["Referer"]
                    if req_headers.get("X-Requested-With"):
                        headers["X-Requested-With"] = req_headers["X-Requested-With"]
                    if req_headers.get("x-requested-with") and "X-Requested-With" not in headers:
                        headers["X-Requested-With"] = req_headers["x-requested-with"]
                    bearer_header = (
                        req_headers.get("Bearer")
                        or req_headers.get("bearer")
                        or req_headers_lower.get("bearer")
                    )
                    authorization_header = (
                        req_headers.get("Authorization")
                        or req_headers.get("authorization")
                        or req_headers_lower.get("authorization")
                    )
                    if bearer_header:
                        last_pc_any_bearer = bearer_header.strip()
                        if "yapiadmin.4399.com" in lowered_url:
                            last_pc_yapi_bearer = bearer_header.strip()
                            chain = _extract_chain(bearer_header)
                            if chain is not None and chain > 0:
                                last_pc_yapi_nonzero_chain_bearer = bearer_header.strip()
                        if "gamedata" in lowered_url and "gamestartdata" in lowered_url:
                            last_pc_game_start_bearer = bearer_header.strip()
                    if authorization_header:
                        last_pc_any_authorization = authorization_header.strip()
                        if "yapiadmin.4399.com" in lowered_url:
                            last_pc_yapi_authorization = authorization_header.strip()
                        if "gamedata" in lowered_url and "gamestartdata" in lowered_url:
                            last_pc_game_start_authorization = authorization_header.strip()

            for hdr in entry.get("response", {}).get("headers", []):
                if str(hdr.get("name", "")).lower() != "location":
                    continue
                loc = str(hdr.get("value") or "")
                token = _extract_token(loc)
                if token:
                    token_candidates.append(token)
                    bootstrap_candidates.append(loc)

    if platform == "fenxi":
        platform_hint = "qz4399doc"
    elif platform == "505":
        platform_hint = "manage505"
    else:
        platform_hint = ""

    matching_tokens = [cand for cand in token_candidates if platform_hint and platform_hint in cand]
    token = matching_tokens[-1] if matching_tokens else (token_candidates[-1] if token_candidates else "")

    matching_bootstrap = [cand for cand in bootstrap_candidates if token and token in cand]
    bootstrap_url = matching_bootstrap[-1] if matching_bootstrap else (bootstrap_candidates[-1] if bootstrap_candidates else "")

    if token and bootstrap_url:
        bootstrap_url = bootstrap_url.replace(token, "{access_token}")

    out_headers = dict(headers)
    if platform == "pc_web":
        selected_bearer = (
            last_pc_game_start_bearer
            or last_pc_yapi_nonzero_chain_bearer
            or last_pc_yapi_bearer
            or last_pc_any_bearer
        )
        selected_authorization = (
            last_pc_game_start_authorization
            or last_pc_yapi_authorization
            or last_pc_any_authorization
        )
        if selected_bearer:
            out_headers["Bearer"] = selected_bearer
        if selected_authorization:
            out_headers["Authorization"] = selected_authorization
        bearer_token = _extract_pc_token_from_bearer(selected_bearer)
        if bearer_token:
            current_admin = str(cookies.get("Admin-Token") or "").strip()
            if (not current_admin) or (current_admin != bearer_token):
                cookies["Admin-Token"] = bearer_token
    if token and platform != "pc_web":
        out_headers.setdefault("X-Access-Token", token)

    return {
        "cookies": cookies,
        "headers": out_headers,
        "token": token,
        "bootstrap_url_template": bootstrap_url,
    }


def build_extra_auth_file(
    fenxi_hars: Iterable[Path],
    manage_hars: Iterable[Path],
    output_path: Path,
    pc_hars: Iterable[Path] | None = None,
) -> Path:
    fenxi_paths = [Path(p) for p in fenxi_hars]
    manage_paths = [Path(p) for p in manage_hars]
    pc_paths = [Path(p) for p in (pc_hars or [])]
    payload = {
        "fenxi": _collect_auth_data(fenxi_paths, "fenxi"),
        "505": _collect_auth_data(manage_paths, "505"),
        "pc_web": _collect_auth_data(pc_paths, "pc_web"),
        "meta": {
            "fenxi_hars": [str(p) for p in fenxi_paths],
            "manage_hars": [str(p) for p in manage_paths],
            "pc_hars": [str(p) for p in pc_paths],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path


def load_extra_auth(path: Path) -> Dict[str, Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: Dict[str, Dict[str, Any]] = {}
    for key in ("fenxi", "505", "pc_web"):
        raw = data.get(key) or {}
        token = str(raw.get("token") or "").strip()
        template = str(raw.get("bootstrap_url_template") or "").strip()
        bootstrap_url = template
        if token and template and "{access_token}" in template:
            bootstrap_url = template.replace("{access_token}", token)

        headers = raw.get("headers") if isinstance(raw.get("headers"), dict) else {}
        cookies = raw.get("cookies") if isinstance(raw.get("cookies"), dict) else {}

        clean_headers: Dict[str, str] = {}
        for hk, hv in headers.items():
            if isinstance(hk, str) and isinstance(hv, str):
                clean_headers[hk] = hv

        clean_cookies: Dict[str, str] = {}
        for ck, cv in cookies.items():
            if isinstance(ck, str) and isinstance(cv, str):
                clean_cookies[ck] = cv

        if key != "pc_web" and token and "X-Access-Token" not in clean_headers:
            clean_headers["X-Access-Token"] = token

        out[key] = {
            "cookies": clean_cookies,
            "headers": clean_headers,
            "bootstrap_url": bootstrap_url,
            "token": token,
        }
    return out


def load_extra_auth_meta(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    meta = data.get("meta")
    if isinstance(meta, dict):
        return meta
    return {}


def inspect_fenxi_token(
    auth: Dict[str, Any] | None,
    warn_threshold_hours: float = 6.0,
    now_utc: datetime | None = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "present": False,
        "decodable": False,
        "expired": False,
        "about_to_expire": False,
        "usable": False,
        "remaining_minutes": None,
        "ttl_hours": None,
        "iat": None,
        "exp": None,
        "reason": "",
    }
    source = auth if isinstance(auth, dict) else {}
    cookies = source.get("cookies") if isinstance(source.get("cookies"), dict) else {}
    token = str(cookies.get("e_token") or "").strip()
    if not token:
        result["reason"] = "fenxi e_token 缺失"
        return result
    result["present"] = True
    payload = _decode_jwt_payload(token)
    if payload is None:
        result["reason"] = "fenxi e_token 无法解析"
        return result
    result["decodable"] = True

    exp_raw = payload.get("exp")
    iat_raw = payload.get("iat")
    if not isinstance(exp_raw, (int, float)):
        result["reason"] = "fenxi e_token 缺少 exp"
        return result

    now_value = (now_utc or datetime.now(timezone.utc)).timestamp()
    exp_value = float(exp_raw)
    iat_value = float(iat_raw) if isinstance(iat_raw, (int, float)) else None
    remaining_minutes = (exp_value - now_value) / 60.0
    result["remaining_minutes"] = remaining_minutes
    result["exp"] = datetime.fromtimestamp(exp_value, tz=timezone.utc)
    if iat_value is not None:
        result["iat"] = datetime.fromtimestamp(iat_value, tz=timezone.utc)
        result["ttl_hours"] = (exp_value - iat_value) / 3600.0

    if remaining_minutes <= 0:
        result["expired"] = True
        result["reason"] = "fenxi e_token 已过期"
        return result

    threshold_minutes = max(0.0, float(warn_threshold_hours)) * 60.0
    if remaining_minutes < threshold_minutes:
        result["about_to_expire"] = True
        result["reason"] = f"fenxi e_token 剩余{remaining_minutes:.1f}分钟，低于阈值{threshold_minutes:.0f}分钟"
        return result

    result["usable"] = True
    result["reason"] = "fenxi e_token 可用"
    return result
