from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from browser_auth_refresh import build_pc_bearer, parse_chain_from_bearer
from extra_auth import inspect_fenxi_token
from pc_web_metrics_service import PCWebMetricsService, PCWebSettings
from autodatareport.atomic_io import atomic_write_json
from autodatareport.redaction import redact_sensitive_text


DEFAULT_CHROME_EXE = Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")
DEFAULT_FENXI_URL = "https://fenxi.4399dev.com/analysis/"
DEFAULT_FENXI_PROBE_URLS = ("https://fenxi.4399dev.com/analysis/",)
DEFAULT_PC_LOGIN_URL = "http://yadmin.4399.com/"
DEFAULT_PC_PROBE_URLS = ("http://yadmin.4399.com/#/statistics/game-start",)
DEFAULT_PC_BASE_URL = "http://yapiadmin.4399.com"
DEFAULT_PC_WEB_ORIGIN = "http://yadmin.4399.com"
DEFAULT_FENXI_MEDIAIDS = "media-eb40cb50d15a49a9"
DEFAULT_FENXI_TOPIC = "gamebox_event"

AUTH_REPAIR_TARGETS = {"870", "fenxi", "505", "pc_web"}
AUTH_REPAIR_TARGET_CHOICES = {"auto", "870", "fenxi", "505", "pc_web", "both", "all"}
CHAIN_RE = re.compile(r"(?:^|[&?])chain=(\d+)(?:$|[&])")


class AuthRepairError(RuntimeError):
    """Raised when the deterministic auth repair runbook cannot finish."""


@dataclass(frozen=True)
class AuthRepairSettings:
    extra_auth_file: Path
    output: Path
    profile_dir: Path
    browser: str = "chrome"
    chrome_executable: str = str(DEFAULT_CHROME_EXE)
    pc_login_url: str = DEFAULT_PC_LOGIN_URL
    login_url_870: str = ""
    manage_login_url: str = ""
    pc_probe_urls: Sequence[str] = field(default_factory=lambda: DEFAULT_PC_PROBE_URLS)
    fenxi_url: str = DEFAULT_FENXI_URL
    fenxi_probe_urls: Sequence[str] = field(default_factory=lambda: DEFAULT_FENXI_PROBE_URLS)
    pc_base_url: str = DEFAULT_PC_BASE_URL
    pc_web_origin: str = DEFAULT_PC_WEB_ORIGIN
    pc_request_timeout: int = 20
    hosts_yaml_path: str = ""
    query_proxy_url: str = ""
    timeout_seconds: int = 300
    target: str = "auto"
    auto_close: bool = True
    fenxi_warn_threshold_hours: float = 6.0
    pc_chain_candidates: Sequence[int] = field(default_factory=lambda: (545,))
    log_path: Optional[Path] = None


@dataclass
class AgentRepairCoordinator:
    """Small coordinator for known auth runbooks and session records."""

    settings: AuthRepairSettings
    log_dir: Path

    def run(self, *, reason_text: str = "") -> Dict[str, Any]:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"auth_repair_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        targets = sorted(resolve_repair_targets(self.settings.target, reason_text))
        _append_log(log_path, f"start targets={','.join(targets) or '-'} reason={_redact(reason_text)}")
        if not targets:
            message = "失败原因不属于 fenxi/PC Web 登录态，跳过自动修复。"
            _append_log(log_path, message)
            raise AuthRepairError(message)
        patched_settings = AuthRepairSettings(
            **{
                **self.settings.__dict__,
                "target": "all" if set(targets) == AUTH_REPAIR_TARGETS else ("both" if set(targets) == {"fenxi", "pc_web"} else targets[0]),
                "log_path": log_path,
            }
        )
        try:
            result = recover_auth_with_chrome_profile(patched_settings)
        except Exception as exc:  # noqa: BLE001
            _append_log(log_path, f"failed {type(exc).__name__}: {_redact(str(exc))}")
            raise
        result["log_path"] = str(log_path)
        _append_log(log_path, f"success updated={','.join(result.get('updated_targets') or [])}")
        return result


def classify_auth_failure(text: str) -> Set[str]:
    raw = str(text or "")
    lowered = raw.lower()
    targets: Set[str] = set()

    fenxi_patterns = (
        "fenxi token 预检失败",
        "fenxi登录态不可用",
        "fenxi 登录态不可用",
        "fenxi e_token",
        "e_token",
        "分析后台登录态预检失败",
        "pc会员登录态预检失败",
        "pc会员",
    )
    pc_patterns = (
        "pc后台登录态预检失败",
        "pc 后台登录态预检失败",
        "pc网页端登录态不可用",
        "pc网页端接口失败",
        "pc web",
        "pc_web",
        "status=-100",
        "status = -100",
        "请先登录",
        "bearer 缺失",
        "bearer",
        "admin-token",
        "admin-token 缺失",
    )

    if any(pattern in lowered for pattern in fenxi_patterns):
        targets.add("fenxi")
    if any(pattern in lowered for pattern in pc_patterns):
        targets.add("pc_web")
    if any(pattern in lowered for pattern in ("870登录态", "phpsessid", "session_cookie")):
        targets.add("870")
    if "505后台" in lowered or "505登录态" in lowered:
        targets.add("505")
    return targets


def resolve_repair_targets(target: str, reason_text: str = "") -> Set[str]:
    normalized = str(target or "auto").strip().lower()
    if normalized not in AUTH_REPAIR_TARGET_CHOICES:
        raise AuthRepairError(f"未知认证修复目标: {target}")
    if normalized == "both":
        return {"fenxi", "pc_web"}
    if normalized == "all":
        return set(AUTH_REPAIR_TARGETS)
    if normalized in AUTH_REPAIR_TARGETS:
        return {normalized}
    inferred = classify_auth_failure(reason_text)
    return inferred or set(AUTH_REPAIR_TARGETS if not str(reason_text or "").strip() else set())


def choose_best_pc_bearer(candidates: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None

    def rank(item: Dict[str, Any]) -> Tuple[int, int, int]:
        url = str(item.get("url") or "").lower()
        bearer = str(item.get("bearer") or "")
        chain = parse_chain_from_bearer(bearer)
        ts = int(item.get("ts") or 0)
        if "gamedata" in url and "gamestartdata" in url:
            return (4, chain or 0, ts)
        if chain is not None and chain > 0:
            return (3, chain, ts)
        if "yapiadmin.4399.com" in url:
            return (2, 0, ts)
        if bearer:
            return (1, 0, ts)
        return (0, 0, ts)

    return sorted(candidates, key=rank, reverse=True)[0]


def merge_repaired_auth(
    existing: Dict[str, Any],
    *,
    fenxi_block: Optional[Dict[str, Any]] = None,
    pc_block: Optional[Dict[str, Any]] = None,
    manage_block: Optional[Dict[str, Any]] = None,
    browser: str = "chrome",
    pc_chain: Optional[int] = None,
) -> Dict[str, Any]:
    payload = dict(existing or {})
    if fenxi_block is not None:
        payload["fenxi"] = fenxi_block
    if pc_block is not None:
        payload["pc_web"] = pc_block
    if manage_block is not None:
        payload["505"] = manage_block
    if "505" not in payload:
        payload["505"] = {"cookies": {}, "headers": {}, "token": "", "bootstrap_url_template": ""}

    meta = dict(payload.get("meta") or {}) if isinstance(payload.get("meta"), dict) else {}
    meta.update(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "chrome_profile_auth_repair",
            "browser": browser,
        }
    )
    if pc_chain is not None:
        meta["pc_chain"] = int(pc_chain)
    payload["meta"] = meta
    return payload


def recover_auth_with_chrome_profile(settings: AuthRepairSettings) -> Dict[str, Any]:
    # Playwright's bundled Node runtime may emit DEP0169 for its own legacy URL code.
    # It is not actionable for users and obscures the login instructions.
    os.environ.setdefault("NODE_NO_WARNINGS", "1")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        raise AuthRepairError("缺少 playwright 运行环境，请先安装 playwright 并安装 Chromium/Chrome 支持。") from exc

    targets = resolve_repair_targets(settings.target)
    existing = _load_json(settings.extra_auth_file)
    settings.profile_dir.mkdir(parents=True, exist_ok=True)
    settings.output.parent.mkdir(parents=True, exist_ok=True)

    chrome_exe = resolve_chrome_executable(settings.browser, settings.chrome_executable)
    urls = []
    if "870" in targets:
        urls.append(settings.login_url_870)
    if "505" in targets:
        urls.append(settings.manage_login_url)
    if "pc_web" in targets:
        urls.extend(_pc_start_urls(settings))
    if "fenxi" in targets:
        urls.extend(_fenxi_start_urls(settings))
    if not urls:
        raise AuthRepairError("没有可修复的认证目标。")

    proc: Optional[subprocess.Popen[Any]] = None
    browser: Any = None
    context: Any = None
    pc_candidates: List[Dict[str, Any]] = []
    seen_bearers: Set[str] = set()
    pc_probe_cache: Dict[str, Tuple[bool, str]] = {}
    fenxi_block: Optional[Dict[str, Any]] = None
    pc_block: Optional[Dict[str, Any]] = None
    manage_block: Optional[Dict[str, Any]] = None
    session_cookie_870 = ""
    selected_chain: Optional[int] = None
    pc_triggered_chains: Set[int] = set()
    last_pc_diag_at = 0.0
    last_fenxi_diag_at = 0.0

    def on_request(request: Any) -> None:
        url = str(getattr(request, "url", "") or "")
        if "yadmin.4399.com" not in url and "yapiadmin.4399.com" not in url:
            return
        try:
            headers = dict(request.headers)
        except Exception:  # noqa: BLE001
            return
        bearer = _normalize_bearer(headers.get("bearer") or headers.get("Bearer") or headers.get("authorization"))
        if not bearer or bearer in seen_bearers:
            return
        chain = parse_chain_from_bearer(bearer)
        if chain != 545:
            return
        seen_bearers.add(bearer)
        pc_candidates.append({"bearer": bearer, "url": url, "ts": int(time.time())})

    with sync_playwright() as pw:
        try:
            proc, endpoint = _launch_chrome_for_cdp(chrome_exe, settings.profile_dir, urls)
            browser = pw.chromium.connect_over_cdp(endpoint)
            contexts = list(browser.contexts or [])
            if not contexts:
                raise AuthRepairError("Chrome CDP 未返回可用浏览器上下文。")
            context = contexts[0]
            context.on("request", on_request)
            _open_missing_pages(context, urls)
            _trim_managed_pages(context, urls)

            deadline = time.monotonic() + max(30, int(settings.timeout_seconds))
            last_message = ""
            while time.monotonic() < deadline:
                cookies = _cookies_from_context(context)
                if "870" in targets and not session_cookie_870:
                    cookie_map = _extract_cookies(cookies, _hosts_from_urls(settings.login_url_870))
                    raw_session = str(cookie_map.get("PHPSESSID") or "").strip()
                    if raw_session and _browser_login_completed(context, settings.login_url_870):
                        session_cookie_870 = f"PHPSESSID={raw_session}"
                    elif raw_session:
                        last_message = "已发现旧 870 Cookie，等待登录页面成功跳转"
                if "505" in targets and manage_block is None:
                    manage_cookies = _extract_cookies(cookies, _hosts_from_urls(settings.manage_login_url))
                    if manage_cookies and _browser_login_completed(context, settings.manage_login_url):
                        existing_manage = existing.get("505") if isinstance(existing.get("505"), dict) else {}
                        manage_block = {
                            "cookies": manage_cookies,
                            "headers": dict(existing_manage.get("headers") or {}),
                            "token": str(existing_manage.get("token") or ""),
                            "bootstrap_url_template": str(existing_manage.get("bootstrap_url_template") or ""),
                        }
                storage_entries = _pc_storage_entries(context, settings) if "pc_web" in targets else {}
                storage_admin_token = _extract_admin_token_from_storage(storage_entries)
                pc_cookie_names = sorted(_extract_cookies(cookies, ("yadmin.4399.com", "yapiadmin.4399.com")).keys())
                fenxi_storage_entries = _fenxi_storage_entries(context, settings) if "fenxi" in targets else {}
                fenxi_storage_token = _extract_fenxi_token_from_storage(fenxi_storage_entries)
                if "fenxi" in targets and time.monotonic() >= last_fenxi_diag_at:
                    fenxi_cookie_names = sorted(_extract_cookies(cookies, ("fenxi.4399dev.com",)).keys())
                    _append_log(
                        settings.log_path,
                        "fenxi_diag "
                        f"cookie_keys={','.join(fenxi_cookie_names) or '-'} "
                        f"storage_keys={','.join(sorted(fenxi_storage_entries.keys())) or '-'}",
                    )
                    last_fenxi_diag_at = time.monotonic() + 15.0
                if "pc_web" in targets:
                    effective_admin_token = _pc_admin_token_from_cookies(cookies) or storage_admin_token
                    if effective_admin_token:
                        _trigger_pc_bearer_requests(
                            context=context,
                            existing=existing,
                            settings=settings,
                            admin_token=effective_admin_token,
                            triggered_chains=pc_triggered_chains,
                        )
                    if time.monotonic() >= last_pc_diag_at:
                        _log_pc_diagnostics(
                            settings.log_path,
                            cookie_names=pc_cookie_names,
                            storage_keys=sorted(storage_entries.keys()),
                            bearer_count=len(pc_candidates),
                            triggered_chains=sorted(pc_triggered_chains),
                        )
                        last_pc_diag_at = time.monotonic() + 15.0
                if "fenxi" in targets and fenxi_block is None:
                    candidate, message = _build_fenxi_block(
                        existing,
                        _cookies_with_fenxi_token(cookies, fenxi_storage_token),
                        settings,
                    )
                    if candidate is not None:
                        fenxi_block = candidate
                    else:
                        last_message = message

                if "pc_web" in targets and pc_block is None:
                    candidate, chain, message = _build_pc_block(
                        existing=existing,
                        cookies=_cookies_with_storage_admin_token(cookies, storage_admin_token),
                        bearer_candidates=pc_candidates,
                        settings=settings,
                        probe_cache=pc_probe_cache,
                    )
                    if candidate is not None:
                        pc_block = candidate
                        selected_chain = chain
                    else:
                        last_message = message

                if (
                    ("870" not in targets or bool(session_cookie_870))
                    and ("fenxi" not in targets or fenxi_block is not None)
                    and ("505" not in targets or manage_block is not None)
                    and ("pc_web" not in targets or pc_block is not None)
                ):
                    payload = merge_repaired_auth(
                        existing,
                        fenxi_block=fenxi_block,
                        pc_block=pc_block,
                        manage_block=manage_block,
                        browser=settings.browser,
                        pc_chain=selected_chain,
                    )
                    atomic_write_json(settings.output, payload)
                    return {
                        "ok": True,
                        "output_path": str(settings.output),
                        "updated_targets": sorted(targets),
                        "session_cookie_870": session_cookie_870,
                        "pc_chain": selected_chain,
                        "fenxi_message": inspect_fenxi_token(fenxi_block or {}).get("reason") if fenxi_block else "",
                    }
                time.sleep(1.0)
            raise AuthRepairError(f"登录态修复超时，请确认 Chrome 登录已完成。最后状态: {last_message}")
        finally:
            if settings.auto_close and browser is not None:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
            if settings.auto_close and proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass


def resolve_chrome_executable(browser: str, explicit: str = "") -> Path:
    normalized = str(browser or "chrome").strip().lower()
    if normalized != "chrome":
        raise AuthRepairError("第一版认证自动修复只支持本机 Chrome。")
    candidates = [
        Path(explicit).expanduser() if str(explicit or "").strip() else DEFAULT_CHROME_EXE,
        DEFAULT_CHROME_EXE,
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate
    raise AuthRepairError(f"未找到 Chrome 可执行文件，请确认路径存在: {explicit or DEFAULT_CHROME_EXE}")


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AuthRepairError(f"读取认证文件失败（非JSON）：{path}") from exc
    if not isinstance(raw, dict):
        raise AuthRepairError(f"认证文件结构异常：{path}")
    return raw


def _append_log(path: Optional[Path], message: str) -> None:
    if path is None:
        return
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _redact(text: str) -> str:
    return redact_sensitive_text(text)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _launch_chrome_for_cdp(chrome_exe: Path, profile_dir: Path, urls: Sequence[str]) -> Tuple[subprocess.Popen[Any], str]:
    port = _find_free_port()
    endpoint = f"http://127.0.0.1:{port}"
    cmd = [
        str(chrome_exe),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        *[u for u in urls if str(u or "").strip()],
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    version_url = f"{endpoint}/json/version"
    deadline = time.monotonic() + 20
    last_error = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AuthRepairError(f"Chrome 启动失败，退出码={proc.returncode}")
        try:
            with urllib.request.urlopen(version_url, timeout=1.5) as response:  # noqa: S310
                if response.status == 200:
                    return proc, endpoint
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            time.sleep(0.5)
    raise AuthRepairError(f"无法连接 Chrome CDP: {last_error}")


def _open_missing_pages(context: Any, urls: Sequence[str]) -> None:
    pages = list(getattr(context, "pages", []) or [])
    for url in urls:
        matched = False
        wanted_host = _url_host(url)
        for page in pages:
            open_url = str(getattr(page, "url", "") or "")
            if _url_host(open_url) != wanted_host:
                continue
            matched = True
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception:  # noqa: BLE001
                try:
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception:  # noqa: BLE001
                    pass
        if matched:
            continue
        try:
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            pages.append(page)
        except Exception:  # noqa: BLE001
            continue


def _trim_managed_pages(context: Any, urls: Sequence[str]) -> None:
    wanted_by_host = {_url_host(url): str(url) for url in urls if _url_host(url)}
    kept_hosts: Set[str] = set()
    pages = list(getattr(context, "pages", []) or [])
    for page in pages:
        current = str(getattr(page, "url", "") or "")
        host = _url_host(current)
        if not host or host not in wanted_by_host:
            continue
        if host in kept_hosts:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
            continue
        kept_hosts.add(host)
        try:
            if current != wanted_by_host[host]:
                page.goto(wanted_by_host[host], wait_until="domcontentloaded", timeout=30000)
        except Exception:  # noqa: BLE001
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception:  # noqa: BLE001
                pass


def _url_host(url: str) -> str:
    try:
        return urllib.parse.urlsplit(str(url or "")).netloc.lower()
    except Exception:  # noqa: BLE001
        return ""


def _browser_login_completed(context: Any, login_url: str) -> bool:
    """Require a target-domain page that is no longer showing a login route."""
    wanted_host = _url_host(login_url)
    if not wanted_host:
        return False
    for page in list(getattr(context, "pages", []) or []):
        current = str(getattr(page, "url", "") or "")
        if _url_host(current) != wanted_host:
            continue
        parsed = urllib.parse.urlsplit(current)
        query = urllib.parse.parse_qs(parsed.query)
        action = str((query.get("ac") or [""])[0]).strip().lower()
        path = parsed.path.rstrip("/").lower()
        if action == "login" or path.endswith("/login") or path.endswith("/oauth"):
            continue
        content_fn = getattr(page, "content", None)
        if callable(content_fn):
            try:
                html = str(content_fn() or "").lower()
            except Exception:  # noqa: BLE001
                html = ""
            redirects_to_login = "ac=login" in html and "location.href" in html
            renders_login_form = "ac=login" in html and ("type=\"password\"" in html or "type='password'" in html)
            if redirects_to_login or renders_login_form:
                continue
        return True
    return False


def _pc_probe_urls(settings: AuthRepairSettings) -> List[str]:
    urls: List[str] = []
    for value in list(settings.pc_probe_urls or DEFAULT_PC_PROBE_URLS):
        url = str(value or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def _pc_start_urls(settings: AuthRepairSettings) -> List[str]:
    return _pc_probe_urls(settings)


def _fenxi_probe_urls(settings: AuthRepairSettings) -> List[str]:
    urls: List[str] = []
    for value in list(settings.fenxi_probe_urls or DEFAULT_FENXI_PROBE_URLS):
        url = str(value or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def _fenxi_start_urls(settings: AuthRepairSettings) -> List[str]:
    urls: List[str] = []
    for value in [settings.fenxi_url, *_fenxi_probe_urls(settings)]:
        url = str(value or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def _cookies_from_context(context: Any) -> List[Dict[str, Any]]:
    try:
        cookies = context.cookies()
    except Exception:  # noqa: BLE001
        return []
    return [dict(item) for item in cookies if isinstance(item, dict)]


def _domain_matches(cookie_domain: str, targets: Iterable[str]) -> bool:
    domain = str(cookie_domain or "").lstrip(".").lower()
    for target in targets:
        clean = str(target or "").lstrip(".").lower()
        if domain == clean or domain.endswith("." + clean) or clean.endswith("." + domain):
            return True
    return False


def _extract_cookies(cookies: Sequence[Dict[str, Any]], domains: Iterable[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for cookie in cookies:
        if not _domain_matches(str(cookie.get("domain") or ""), domains):
            continue
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "").strip()
        if name and value:
            out[name] = value
    return out


def _hosts_from_urls(*urls: str) -> Tuple[str, ...]:
    hosts: List[str] = []
    for value in urls:
        host = str(urllib.parse.urlparse(str(value or "")).hostname or "").strip().lower()
        if host and host not in hosts:
            hosts.append(host)
    return tuple(hosts)


def _pc_admin_token_from_cookies(cookies: Sequence[Dict[str, Any]]) -> str:
    return str(_extract_cookies(cookies, ("yadmin.4399.com", "yapiadmin.4399.com")).get("Admin-Token") or "").strip()


def _cookies_with_fenxi_token(cookies: Sequence[Dict[str, Any]], e_token: str) -> List[Dict[str, Any]]:
    out = [dict(item) for item in cookies if isinstance(item, dict)]
    if not str(e_token or "").strip() or _extract_cookies(out, ("fenxi.4399dev.com",)).get("e_token"):
        return out
    out.append({"domain": "fenxi.4399dev.com", "name": "e_token", "value": str(e_token).strip()})
    return out


def _cookies_with_storage_admin_token(cookies: Sequence[Dict[str, Any]], admin_token: str) -> List[Dict[str, Any]]:
    out = [dict(item) for item in cookies if isinstance(item, dict)]
    if not str(admin_token or "").strip() or _pc_admin_token_from_cookies(out):
        return out
    out.append({"domain": "yadmin.4399.com", "name": "Admin-Token", "value": str(admin_token).strip()})
    return out


def _pc_storage_entries(context: Any, settings: AuthRepairSettings) -> Dict[str, str]:
    entries: Dict[str, str] = {}
    page = _ensure_pc_origin_page(context, settings, navigate=False)
    if page is None:
        return entries
    try:
        raw = page.evaluate(
            """
            () => {
              const out = {};
              for (const [storeName, store] of [['localStorage', window.localStorage], ['sessionStorage', window.sessionStorage]]) {
                for (let i = 0; i < store.length; i += 1) {
                  const key = store.key(i);
                  if (!key) continue;
                  const lower = String(key).toLowerCase();
                  if (lower.includes('token') || lower.includes('admin') || lower.includes('bearer') || lower.includes('chain')) {
                    out[`${storeName}.${key}`] = String(store.getItem(key) || '');
                  }
                }
              }
              return out;
            }
            """
        )
    except Exception:  # noqa: BLE001
        return entries
    if isinstance(raw, dict):
        for key, value in raw.items():
            clean_key = str(key or "").strip()
            clean_value = str(value or "").strip()
            if clean_key and clean_value:
                entries[clean_key] = clean_value
    return entries


def _fenxi_storage_entries(context: Any, settings: AuthRepairSettings) -> Dict[str, str]:
    entries: Dict[str, str] = {}
    page = _ensure_fenxi_page(context, settings, navigate=False)
    if page is None:
        return entries
    try:
        raw = page.evaluate(
            """
            () => {
              const out = {};
              for (const [storeName, store] of [['localStorage', window.localStorage], ['sessionStorage', window.sessionStorage]]) {
                for (let i = 0; i < store.length; i += 1) {
                  const key = store.key(i);
                  if (!key) continue;
                  const lower = String(key).toLowerCase();
                  if (lower.includes('e_token') || lower.includes('token') || lower.includes('access')) {
                    out[`${storeName}.${key}`] = String(store.getItem(key) || '');
                  }
                }
              }
              return out;
            }
            """
        )
    except Exception:  # noqa: BLE001
        return entries
    if isinstance(raw, dict):
        for key, value in raw.items():
            clean_key = str(key or "").strip()
            clean_value = str(value or "").strip()
            if clean_key and clean_value:
                entries[clean_key] = clean_value
    return entries


def _extract_fenxi_token_from_storage(entries: Dict[str, str]) -> str:
    preferred: List[Tuple[str, str]] = []
    fallback: List[Tuple[str, str]] = []
    for key, value in entries.items():
        clean_value = str(value or "").strip()
        if not clean_value:
            continue
        lower = str(key or "").lower()
        if "e_token" in lower:
            preferred.append((key, clean_value))
        elif "token" in lower or "access" in lower:
            fallback.append((key, clean_value))
    for _key, value in [*preferred, *fallback]:
        block = {"cookies": {"e_token": value}}
        diag = inspect_fenxi_token(block, warn_threshold_hours=0.0)
        if bool(diag.get("present")) and bool(diag.get("decodable")) and not bool(diag.get("expired")):
            return value
    return ""


def _extract_admin_token_from_storage(entries: Dict[str, str]) -> str:
    preferred: List[Tuple[str, str]] = []
    fallback: List[Tuple[str, str]] = []
    for key, value in entries.items():
        clean_value = str(value or "").strip()
        if not clean_value:
            continue
        lower = str(key or "").lower()
        if "admin" in lower and "token" in lower:
            preferred.append((key, clean_value))
        elif lower.endswith(".token") or lower.endswith("token"):
            fallback.append((key, clean_value))
    for _key, value in [*preferred, *fallback]:
        if 8 <= len(value) <= 256 and "&" not in value and "=" not in value:
            return value
    return ""


def _ensure_fenxi_page(context: Any, settings: AuthRepairSettings, *, navigate: bool) -> Optional[Any]:
    target_url = _fenxi_probe_urls(settings)[0]
    for page in list(getattr(context, "pages", []) or []):
        url = str(getattr(page, "url", "") or "")
        if "fenxi.4399dev.com" in url:
            if navigate:
                try:
                    page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                except Exception:  # noqa: BLE001
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=30000)
                    except Exception:  # noqa: BLE001
                        pass
            return page
    if not navigate:
        return None
    try:
        page = context.new_page()
        page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        return page
    except Exception:  # noqa: BLE001
        return None


def _refresh_fenxi_pages(context: Any, settings: AuthRepairSettings) -> None:
    pages = list(getattr(context, "pages", []) or [])
    for probe_url in _fenxi_probe_urls(settings):
        matched = False
        for page in pages:
            current = str(getattr(page, "url", "") or "")
            if "fenxi.4399dev.com" not in current:
                continue
            matched = True
            try:
                page.goto(probe_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:  # noqa: BLE001
                try:
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception:  # noqa: BLE001
                    pass
        if matched:
            continue
        try:
            page = context.new_page()
            page.goto(probe_url, wait_until="domcontentloaded", timeout=30000)
            pages.append(page)
        except Exception:  # noqa: BLE001
            continue


def _ensure_pc_origin_page(context: Any, settings: AuthRepairSettings, *, navigate: bool) -> Optional[Any]:
    origin = settings.pc_web_origin.rstrip("/")
    target_url = _pc_probe_urls(settings)[0]
    for page in list(getattr(context, "pages", []) or []):
        url = str(getattr(page, "url", "") or "")
        if url.startswith(origin):
            if navigate:
                try:
                    page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                except Exception:  # noqa: BLE001
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=30000)
                    except Exception:  # noqa: BLE001
                        pass
            return page
    if not navigate:
        return None
    try:
        page = context.new_page()
        page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        return page
    except Exception:  # noqa: BLE001
        return None


def _refresh_pc_pages(context: Any, settings: AuthRepairSettings) -> None:
    pages = list(getattr(context, "pages", []) or [])
    for probe_url in _pc_probe_urls(settings):
        matched = False
        for page in pages:
            current = str(getattr(page, "url", "") or "")
            if current.split("#", 1)[0].rstrip("/") != probe_url.split("#", 1)[0].rstrip("/"):
                continue
            matched = True
            try:
                page.goto(probe_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:  # noqa: BLE001
                try:
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception:  # noqa: BLE001
                    pass
        if matched:
            continue
        try:
            page = context.new_page()
            page.goto(probe_url, wait_until="domcontentloaded", timeout=30000)
            pages.append(page)
        except Exception:  # noqa: BLE001
            continue


def _trigger_pc_bearer_requests(
    *,
    context: Any,
    existing: Dict[str, Any],
    settings: AuthRepairSettings,
    admin_token: str,
    triggered_chains: Set[int],
) -> None:
    token = str(admin_token or "").strip()
    if not token:
        return
    probe_date = date.today() - timedelta(days=1)
    start_date = probe_date - timedelta(days=1)
    endpoint = f"{settings.pc_base_url.rstrip('/')}/?m=gameData&ac=gameStartData"
    chain_candidates = _collect_chain_candidates(existing, settings) or [545]
    pending_chains = [int(chain) for chain in chain_candidates if int(chain) not in triggered_chains]
    if not pending_chains:
        return
    page = _ensure_pc_origin_page(context, settings, navigate=True)
    if page is None:
        return
    for chain_value in pending_chains:
        bearer = build_pc_bearer(token, chain_value)
        body = f"time_start={start_date.isoformat()}&time_end={probe_date.isoformat()}&gameids="
        try:
            page.evaluate(
                """
                async ({ endpoint, bearer, body }) => {
                  try {
                    await fetch(endpoint, {
                      method: 'POST',
                      mode: 'cors',
                      credentials: 'include',
                      headers: {
                        'Bearer': bearer,
                        'X-Requested-With': 'XMLHttpRequest',
                        'Content-Type': 'application/x-www-form-urlencoded'
                      },
                      body
                    });
                  } catch (error) {
                    return { ok: false, message: String(error && error.message || error) };
                  }
                  return { ok: true };
                }
                """,
                {"endpoint": endpoint, "bearer": bearer, "body": body},
            )
        except Exception:  # noqa: BLE001
            pass
        triggered_chains.add(chain_value)


def _log_pc_diagnostics(
    log_path: Optional[Path],
    *,
    cookie_names: Sequence[str],
    storage_keys: Sequence[str],
    bearer_count: int,
    triggered_chains: Sequence[int],
) -> None:
    if log_path is None:
        return
    _append_log(
        log_path,
        "pc_diag "
        f"cookie_keys={','.join(cookie_names) or '-'} "
        f"storage_keys={','.join(storage_keys) or '-'} "
        f"bearer_candidates={bearer_count} "
        f"triggered_chains={','.join(str(x) for x in triggered_chains) or '-'}",
    )


def _build_fenxi_block(
    existing: Dict[str, Any],
    cookies: Sequence[Dict[str, Any]],
    settings: AuthRepairSettings,
) -> Tuple[Optional[Dict[str, Any]], str]:
    fenxi_cookies = _extract_cookies(cookies, ("fenxi.4399dev.com",))
    if "e_token" not in fenxi_cookies:
        return None, "等待 fenxi e_token"

    existing_fenxi = existing.get("fenxi") if isinstance(existing.get("fenxi"), dict) else {}
    headers = dict(existing_fenxi.get("headers") or {}) if isinstance(existing_fenxi.get("headers"), dict) else {}
    token = str(existing_fenxi.get("token") or "").strip()
    headers.setdefault("mediaids", DEFAULT_FENXI_MEDIAIDS)
    headers.setdefault("topic", DEFAULT_FENXI_TOPIC)
    if token:
        headers.setdefault("X-Access-Token", token)
    block = {
        "cookies": fenxi_cookies,
        "headers": headers,
        "token": token,
        "bootstrap_url_template": str(existing_fenxi.get("bootstrap_url_template") or ""),
    }
    diag = inspect_fenxi_token(block, warn_threshold_hours=settings.fenxi_warn_threshold_hours)
    if not bool(diag.get("usable")):
        return None, str(diag.get("reason") or "fenxi e_token 不可用")
    return block, str(diag.get("reason") or "fenxi e_token 可用")


def _build_pc_block(
    *,
    existing: Dict[str, Any],
    cookies: Sequence[Dict[str, Any]],
    bearer_candidates: Sequence[Dict[str, Any]],
    settings: AuthRepairSettings,
    probe_cache: Dict[str, Tuple[bool, str]],
) -> Tuple[Optional[Dict[str, Any]], Optional[int], str]:
    pc_cookies = _extract_cookies(cookies, ("yadmin.4399.com", "yapiadmin.4399.com"))
    admin_token = str(pc_cookies.get("Admin-Token") or "").strip()
    if not admin_token:
        return None, None, "等待 PC Admin-Token"

    candidates = [
        item
        for item in bearer_candidates
        if parse_chain_from_bearer(str(item.get("bearer") or "")) == 545
    ]
    existing_pc = existing.get("pc_web") if isinstance(existing.get("pc_web"), dict) else {}
    existing_headers = existing_pc.get("headers") if isinstance(existing_pc.get("headers"), dict) else {}
    existing_bearer = _normalize_bearer(str(existing_headers.get("Bearer") or ""))
    if existing_bearer and parse_chain_from_bearer(existing_bearer) == 545:
        candidates.append({"bearer": existing_bearer, "url": "existing", "ts": 0})

    ranked = []
    best = choose_best_pc_bearer(candidates)
    if best is not None:
        ranked.append(best)
    ranked.extend([item for item in candidates if item is not best])

    tried: Set[str] = set()
    for item in ranked:
        bearer = _normalize_bearer(str(item.get("bearer") or ""))
        if not bearer or bearer in tried:
            continue
        tried.add(bearer)
        ok, message = _pc_preflight_cached(bearer, admin_token, settings, probe_cache)
        if ok:
            chain = parse_chain_from_bearer(bearer)
            return _pc_auth_block(admin_token, bearer, settings), chain, message

    chain_candidates = [545]
    for chain in chain_candidates:
        bearer = build_pc_bearer(admin_token, chain)
        ok, message = _pc_preflight_cached(bearer, admin_token, settings, probe_cache)
        if ok:
            return _pc_auth_block(admin_token, bearer, settings), int(chain), message
    return None, None, "等待可通过预检的 PC Bearer/chain"


def _normalize_bearer(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower().startswith("bearer "):
        return text[7:].strip()
    return text


def _collect_chain_candidates(existing: Dict[str, Any], settings: AuthRepairSettings) -> List[int]:
    return [545]


def _pc_auth_block(admin_token: str, bearer: str, settings: AuthRepairSettings) -> Dict[str, Any]:
    return {
        "cookies": {"Admin-Token": admin_token},
        "headers": {
            "Bearer": bearer,
            "Origin": settings.pc_web_origin,
            "Referer": f"{settings.pc_web_origin.rstrip('/')}/",
            "X-Requested-With": "XMLHttpRequest",
        },
        "token": "",
        "bootstrap_url_template": "",
    }


def _pc_preflight_cached(
    bearer: str,
    admin_token: str,
    settings: AuthRepairSettings,
    cache: Dict[str, Tuple[bool, str]],
) -> Tuple[bool, str]:
    if bearer in cache:
        return cache[bearer]
    auth = _pc_auth_block(admin_token, bearer, settings)
    service = PCWebMetricsService(
        PCWebSettings(
            base_url=settings.pc_base_url,
            web_origin=settings.pc_web_origin,
            request_timeout=int(settings.pc_request_timeout),
            query_proxy_url=str(settings.query_proxy_url or "").strip(),
            hosts_yaml_path=str(settings.hosts_yaml_path or "").strip(),
        )
    )

    try:
        result = _run_coroutine_blocking(service.preflight(date.today() - timedelta(days=1), auth))
    except Exception as exc:  # noqa: BLE001
        out = (False, f"PC预检异常: {exc}")
    else:
        out = (bool(result.get("ok")), str(result.get("message") or ""))
    cache[bearer] = out
    return out


def _run_coroutine_blocking(coro: Any) -> Any:
    result_box: Dict[str, Any] = {}
    error_box: Dict[str, BaseException] = {}

    def runner() -> None:
        try:
            result_box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            error_box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in error_box:
        raise error_box["error"]
    return result_box.get("value")
