from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from extra_auth import inspect_fenxi_token
from pc_web_metrics_service import PCWebMetricsService, PCWebSettings

CHAIN_RE = re.compile(r"(?:^|[&?])chain=(\d+)(?:$|[&])")

DEFAULT_FENXI_MEDIAIDS = "media-eb40cb50d15a49a9"
DEFAULT_FENXI_TOPIC = "gamebox_event"


class BrowserAuthRefreshError(RuntimeError):
    """Raised when browser-based auth refresh fails."""


@dataclass(frozen=True)
class BrowserRefreshSettings:
    browser: str = "auto"
    extra_auth_path: Path = Path("extra_auth.json")
    output_path: Path = Path("extra_auth.json")
    hosts_yaml_path: str = "hosts_505.yaml"
    query_proxy_url: str = ""
    pc_base_url: str = "http://yapiadmin.4399.com"
    pc_web_origin: str = "http://yadmin.4399.com"
    pc_request_timeout: int = 20
    pc_chain_candidates: Sequence[int] = (545,)
    pc_scan_start: Optional[int] = None
    pc_scan_end: Optional[int] = None
    fenxi_warn_threshold_hours: float = 6.0
    cookie_file: str = ""
    key_file: str = ""
    pc_only: bool = False


def parse_chain_from_bearer(bearer: str) -> Optional[int]:
    text = str(bearer or "").strip()
    if not text:
        return None
    match = CHAIN_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def build_pc_bearer(admin_token: str, chain: int) -> str:
    token = str(admin_token or "").strip()
    if not token:
        raise ValueError("admin_token is empty")
    return f"token={token}&chain={int(chain)}"


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BrowserAuthRefreshError(f"读取认证文件失败（非JSON）：{path}") from exc
    if not isinstance(raw, dict):
        raise BrowserAuthRefreshError(f"认证文件结构异常：{path}")
    return raw


def _import_browser_cookie3():
    try:
        import browser_cookie3  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise BrowserAuthRefreshError(
            "缺少依赖 browser-cookie3。请先执行: uv add browser-cookie3 (或 pip install browser-cookie3)"
        ) from exc
    return browser_cookie3


def _load_cookie_jar(browser: str, cookie_file: str = "", key_file: str = ""):
    bc3 = _import_browser_cookie3()
    target = str(browser or "auto").strip().lower()
    cookie_path = str(cookie_file or "").strip()
    key_path = str(key_file or "").strip()
    if cookie_path:
        key_arg = key_path or None
        # Custom cookie database path: useful for non-standard Chromium forks such as Arc.
        return bc3.chromium(cookie_file=cookie_path, key_file=key_arg)
    loaders = {
        "chrome": bc3.chrome,
        "chromium": bc3.chromium,
        "edge": bc3.edge,
        "firefox": bc3.firefox,
        "safari": bc3.safari,
        "brave": getattr(bc3, "brave", None),
        "opera": getattr(bc3, "opera", None),
        "arc": None,
    }
    if target == "arc":
        candidates = [
            (
                Path.home() / "Library/Application Support/Arc/User Data/Default/Cookies",
                Path.home() / "Library/Application Support/Arc/User Data/Local State",
            ),
            (
                Path.home() / "Library/Application Support/Arc/User Data/Profile 1/Cookies",
                Path.home() / "Library/Application Support/Arc/User Data/Local State",
            ),
        ]
        errors: List[str] = []
        for cookie_db, key_db in candidates:
            if not cookie_db.exists():
                continue
            try:
                return bc3.chromium(cookie_file=str(cookie_db), key_file=str(key_db if key_db.exists() else ""))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{cookie_db}:{exc}")
        raise BrowserAuthRefreshError(
            "Arc Cookie读取失败，请使用 --cookie-file 指定Cookies数据库路径。"
            "常见路径: ~/Library/Application Support/Arc/User Data/Default/Cookies"
            + (f" errors={'; '.join(errors[:2])}" if errors else "")
        )
    if target == "atlas":
        atlas_root = Path.home() / "Library/Application Support/com.openai.atlas/browser-data/host"
        key_db = atlas_root / "Local State"
        candidates = [
            *sorted(atlas_root.glob("user-*/Cookies")),
            atlas_root / "Default/Cookies",
        ]
        errors: List[str] = []
        for cookie_db in candidates:
            if not cookie_db.exists():
                continue
            try:
                return bc3.chromium(cookie_file=str(cookie_db), key_file=str(key_db if key_db.exists() else ""))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{cookie_db}:{exc}")
        raise BrowserAuthRefreshError(
            "Atlas Cookie读取失败，请使用 --cookie-file 指定具体Cookies路径。"
            "常见路径: ~/Library/Application Support/com.openai.atlas/browser-data/host/user-*/Cookies"
            + (f" errors={'; '.join(errors[:2])}" if errors else "")
        )
    if target and target != "auto":
        loader = loaders.get(target)
        if loader is None:
            raise BrowserAuthRefreshError(f"不支持的浏览器: {browser}")
        return loader()
    # Auto mode: pick first available browser profile.
    errors: List[str] = []
    for name in ("chrome", "edge", "chromium", "brave", "firefox", "safari", "opera", "atlas"):
        loader = loaders.get(name)
        if name == "atlas":
            try:
                return _load_cookie_jar("atlas")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"atlas:{exc}")
                continue
        if loader is None:
            continue
        try:
            return loader()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}:{exc}")
            continue
    raise BrowserAuthRefreshError(
        "无法自动读取浏览器Cookie，请确认浏览器已登录并允许本地读取。"
        "可改用 --browser 指定类型，或 --cookie-file/--key-file 指定数据库路径。"
        f"errors={'; '.join(errors[:3])}"
    )


def _cookie_matches_domain(cookie_domain: str, targets: Iterable[str]) -> bool:
    domain = str(cookie_domain or "").lstrip(".").lower()
    if not domain:
        return False
    for target in targets:
        t = str(target or "").lstrip(".").lower()
        if not t:
            continue
        if domain == t or domain.endswith("." + t):
            return True
    return False


def _extract_domain_cookies(cookie_jar, targets: Iterable[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in cookie_jar:
        domain = getattr(item, "domain", "")
        if not _cookie_matches_domain(domain, targets):
            continue
        name = str(getattr(item, "name", "") or "").strip()
        value = str(getattr(item, "value", "") or "").strip()
        if name and value:
            out[name] = value
    return out


def _iter_atlas_cookie_dbs(cookie_file: str = "") -> List[Path]:
    cookie_path = str(cookie_file or "").strip()
    if cookie_path:
        path = Path(cookie_path).expanduser()
        return [path] if path.exists() else []
    atlas_root = Path.home() / "Library/Application Support/com.openai.atlas/browser-data/host"
    candidates = [
        *sorted(atlas_root.glob("user-*/Cookies")),
        *sorted(atlas_root.glob("login-staging-*/Cookies")),
        atlas_root / "Default/Cookies",
    ]
    out: List[Path] = []
    for path in candidates:
        if path.exists() and path not in out:
            out.append(path)
    return out


def _extract_cookie_value_from_sqlite(
    db_path: Path,
    *,
    cookie_name: str,
    target_domains: Sequence[str],
) -> tuple[str, bool]:
    encrypted_only = False
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except Exception:  # noqa: BLE001
        return "", False
    try:
        rows = conn.execute(
            """
            SELECT host_key, value, encrypted_value
            FROM cookies
            WHERE name = ?
            ORDER BY last_access_utc DESC
            """,
            (cookie_name,),
        )
        for host_key, raw_value, encrypted_value in rows.fetchall():
            if not _cookie_matches_domain(str(host_key or ""), target_domains):
                continue
            value = str(raw_value or "").strip()
            if value:
                return value, False
            if encrypted_value:
                encrypted_only = True
    except Exception:  # noqa: BLE001
        return "", False
    finally:
        conn.close()
    return "", encrypted_only


def _fallback_admin_token_from_atlas(cookie_file: str = "") -> tuple[str, bool]:
    encrypted_only = False
    for db in _iter_atlas_cookie_dbs(cookie_file):
        value, is_encrypted_only = _extract_cookie_value_from_sqlite(
            db,
            cookie_name="Admin-Token",
            target_domains=("yadmin.4399.com", "yapiadmin.4399.com"),
        )
        if value:
            return value, False
        if is_encrypted_only:
            encrypted_only = True
    return "", encrypted_only


def _collect_chain_candidates(existing_payload: Dict[str, Any], settings: BrowserRefreshSettings) -> List[int]:
    out: List[int] = []
    raw_pc = existing_payload.get("pc_web") if isinstance(existing_payload.get("pc_web"), dict) else {}
    headers = raw_pc.get("headers") if isinstance(raw_pc.get("headers"), dict) else {}
    existing_chain = parse_chain_from_bearer(str(headers.get("Bearer") or ""))
    if existing_chain is not None:
        out.append(existing_chain)
    for value in settings.pc_chain_candidates:
        try:
            chain = int(value)
        except (TypeError, ValueError):
            continue
        out.append(chain)
    deduped: List[int] = []
    for chain in out:
        if chain not in deduped:
            deduped.append(chain)
    return deduped


async def _probe_pc_chain(
    *,
    admin_token: str,
    chain_candidates: Sequence[int],
    settings: BrowserRefreshSettings,
    scan_start: Optional[int],
    scan_end: Optional[int],
) -> int:
    service = PCWebMetricsService(
        PCWebSettings(
            base_url=settings.pc_base_url,
            web_origin=settings.pc_web_origin,
            request_timeout=int(settings.pc_request_timeout),
            query_proxy_url=str(settings.query_proxy_url or "").strip(),
            hosts_yaml_path=str(settings.hosts_yaml_path or "").strip(),
        )
    )
    probe_date = date.today() - timedelta(days=1)

    async def _is_ok(chain: int) -> bool:
        auth = {
            "headers": {
                "Bearer": build_pc_bearer(admin_token, chain),
                "Origin": settings.pc_web_origin,
                "Referer": f"{settings.pc_web_origin.rstrip('/')}/",
                "X-Requested-With": "XMLHttpRequest",
            },
            "cookies": {"Admin-Token": admin_token},
        }
        result = await service.preflight(probe_date, auth)
        return bool(result.get("ok"))

    for chain in chain_candidates:
        if await _is_ok(chain):
            return int(chain)

    if scan_start is not None and scan_end is not None:
        lo = int(min(scan_start, scan_end))
        hi = int(max(scan_start, scan_end))
        for chain in range(lo, hi + 1):
            if chain in chain_candidates:
                continue
            if await _is_ok(chain):
                return int(chain)

    raise BrowserAuthRefreshError(
        "无法探测到可用的 pc_web Bearer chain。请确认 yadmin 已登录，或手动补充 --pc-chain/--pc-scan-start/--pc-scan-end。"
    )


def refresh_extra_auth_from_browser(settings: BrowserRefreshSettings) -> Dict[str, Any]:
    existing = _load_json(settings.extra_auth_path)
    cookie_jar = _load_cookie_jar(settings.browser, settings.cookie_file, settings.key_file)

    fenxi_existing = existing.get("fenxi") if isinstance(existing.get("fenxi"), dict) else {}
    fenxi_block = dict(fenxi_existing)
    fenxi_diag: Dict[str, Any] = {"usable": False, "reason": "未刷新"}
    if not settings.pc_only:
        fenxi_cookies = _extract_domain_cookies(cookie_jar, ("fenxi.4399dev.com",))
        if "e_token" not in fenxi_cookies:
            present = ",".join(sorted(fenxi_cookies.keys())[:8])
            raise BrowserAuthRefreshError(
                "未从浏览器读取到 fenxi e_token，请先登录分析后台。"
                + (f" 当前已读到fenxi cookies: {present}" if present else "")
            )

        fenxi_headers = dict(fenxi_existing.get("headers") or {}) if isinstance(fenxi_existing.get("headers"), dict) else {}
        fenxi_token = str(fenxi_existing.get("token") or "").strip()
        fenxi_headers.setdefault("mediaids", DEFAULT_FENXI_MEDIAIDS)
        fenxi_headers.setdefault("topic", DEFAULT_FENXI_TOPIC)
        if fenxi_token:
            fenxi_headers.setdefault("X-Access-Token", fenxi_token)

        fenxi_block = {
            "cookies": fenxi_cookies,
            "headers": fenxi_headers,
            "token": fenxi_token,
            "bootstrap_url_template": str(fenxi_existing.get("bootstrap_url_template") or ""),
        }
        fenxi_diag = inspect_fenxi_token(fenxi_block, warn_threshold_hours=settings.fenxi_warn_threshold_hours)

    pc_cookies = _extract_domain_cookies(cookie_jar, ("yadmin.4399.com", "yapiadmin.4399.com"))
    admin_token = str(pc_cookies.get("Admin-Token") or "").strip()
    if not admin_token:
        browser_name = str(settings.browser or "").strip().lower()
        if browser_name in {"atlas", "auto"}:
            admin_token, encrypted_only = _fallback_admin_token_from_atlas(settings.cookie_file)
            if encrypted_only and not admin_token:
                raise BrowserAuthRefreshError(
                    "检测到 Atlas 存在 yadmin Admin-Token，但为加密存储，当前进程无法直接解密。"
                    "请改用 GUI 的“上传PC HAR并更新”按钮。"
                )
        if not admin_token:
            raise BrowserAuthRefreshError(
                "未从浏览器读取到 yadmin Admin-Token。请先登录PC后台，或使用 GUI 的“上传PC HAR并更新”。"
            )

    chain_candidates = _collect_chain_candidates(existing, settings)
    selected_chain = asyncio.run(
        _probe_pc_chain(
            admin_token=admin_token,
            chain_candidates=chain_candidates,
            settings=settings,
            scan_start=settings.pc_scan_start,
            scan_end=settings.pc_scan_end,
        )
    )

    pc_bearer = build_pc_bearer(admin_token, selected_chain)
    pc_block = {
        "cookies": {"Admin-Token": admin_token},
        "headers": {
            "Bearer": pc_bearer,
            "Origin": settings.pc_web_origin,
            "Referer": f"{settings.pc_web_origin.rstrip('/')}/",
            "X-Requested-With": "XMLHttpRequest",
        },
        "token": "",
        "bootstrap_url_template": "",
    }

    manage_block = existing.get("505") if isinstance(existing.get("505"), dict) else {
        "cookies": {},
        "headers": {},
        "token": "",
        "bootstrap_url_template": "",
    }

    meta = existing.get("meta") if isinstance(existing.get("meta"), dict) else {}
    updated_meta = dict(meta)
    updated_meta.update(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "browser_cookie_refresh",
            "browser": settings.browser,
            "pc_chain": int(selected_chain),
        }
    )

    payload = {
        "fenxi": fenxi_block,
        "505": manage_block,
        "pc_web": pc_block,
        "meta": updated_meta,
    }

    settings.output_path.parent.mkdir(parents=True, exist_ok=True)
    settings.output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    fenxi_diag = inspect_fenxi_token(fenxi_block, warn_threshold_hours=settings.fenxi_warn_threshold_hours)
    return {
        "output_path": str(settings.output_path),
        "pc_chain": int(selected_chain),
        "fenxi_e_token_usable": bool(fenxi_diag.get("usable")),
        "fenxi_message": str(fenxi_diag.get("reason") or ""),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从本机浏览器自动刷新 fenxi + pc_web 登录态到 extra_auth.json（无需HAR）。")
    parser.add_argument("--browser", type=str, default="auto", help="浏览器: auto/chrome/edge/chromium/brave/firefox/safari/opera/arc/atlas")
    parser.add_argument("--cookie-file", type=str, default="", help="可选：自定义Cookies数据库文件路径（用于非标准浏览器）")
    parser.add_argument("--key-file", type=str, default="", help="可选：自定义Local State路径（Chromium系解密密钥）")
    parser.add_argument("--extra-auth-file", type=Path, default=Path("extra_auth.json"), help="已有 extra_auth.json（用于保留505与历史信息）")
    parser.add_argument("--output", type=Path, default=Path("extra_auth.json"), help="输出路径")
    parser.add_argument("--hosts-yaml-path", type=str, default="hosts_505.yaml", help="用于PC chain探测的hosts映射")
    parser.add_argument("--query-proxy-url", type=str, default="", help="用于PC chain探测的代理")
    parser.add_argument("--pc-base-url", type=str, default="http://yapiadmin.4399.com")
    parser.add_argument("--pc-web-origin", type=str, default="http://yadmin.4399.com")
    parser.add_argument("--pc-request-timeout", type=int, default=20)
    parser.add_argument("--pc-chain", action="append", type=int, default=[], help="优先尝试的chain，可多次传入")
    parser.add_argument("--pc-scan-start", type=int, default=None, help="可选：chain扫描起点")
    parser.add_argument("--pc-scan-end", type=int, default=None, help="可选：chain扫描终点")
    parser.add_argument("--pc-only", action="store_true", help="仅刷新 pc_web 登录态，不校验/更新 fenxi")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    defaults: List[int] = [545]
    explicit = [int(v) for v in (args.pc_chain or [])]
    settings = BrowserRefreshSettings(
        browser=str(args.browser or "auto").strip() or "auto",
        extra_auth_path=args.extra_auth_file,
        output_path=args.output,
        hosts_yaml_path=str(args.hosts_yaml_path or "").strip(),
        query_proxy_url=str(args.query_proxy_url or "").strip(),
        pc_base_url=str(args.pc_base_url or "").strip(),
        pc_web_origin=str(args.pc_web_origin or "").strip(),
        pc_request_timeout=int(args.pc_request_timeout),
        pc_chain_candidates=tuple(explicit + defaults),
        pc_scan_start=args.pc_scan_start,
        pc_scan_end=args.pc_scan_end,
        cookie_file=str(args.cookie_file or "").strip(),
        key_file=str(args.key_file or "").strip(),
        pc_only=bool(args.pc_only),
    )
    result = refresh_extra_auth_from_browser(settings)
    print(
        json.dumps(
            {
                "ok": True,
                "output_path": result.get("output_path"),
                "pc_chain": result.get("pc_chain"),
                "fenxi_e_token_usable": result.get("fenxi_e_token_usable"),
                "fenxi_message": result.get("fenxi_message"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
