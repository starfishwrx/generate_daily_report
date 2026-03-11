from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


class AuthRecoveryError(RuntimeError):
    """Raised when interactive auth recovery fails."""


CHAIN_RE = re.compile(r"(?:^|[&?])chain=(\d+)(?:$|[&])")
TOKEN_RE = re.compile(r"(?:^|[&?])token=([^&]+)(?:$|[&])")
SMS_URL_HINTS = ("sms", "sendcode", "verifycode", "msgcode", "captcha", "vcode")


def _extract_chain(bearer: str) -> Optional[int]:
    text = str(bearer or "")
    m = CHAIN_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _extract_token_from_bearer(bearer: str) -> str:
    text = str(bearer or "")
    m = TOKEN_RE.search(text)
    if not m:
        return ""
    return m.group(1).strip()


def _choose_best_pc_bearer(candidates: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None

    def rank(item: Dict[str, Any]) -> tuple[int, int, int]:
        url = str(item.get("url") or "").lower()
        bearer = str(item.get("bearer") or "")
        chain = _extract_chain(bearer)
        if "gamedata" in url and "gamestartdata" in url:
            return (3, 0, int(item.get("ts") or 0))
        if chain is not None and chain > 0:
            return (2, chain, int(item.get("ts") or 0))
        if "yapiadmin.4399.com" in url:
            return (1, 0, int(item.get("ts") or 0))
        return (0, 0, int(item.get("ts") or 0))

    return sorted(candidates, key=rank, reverse=True)[0]


def _load_existing(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AuthRecoveryError(f"读取认证文件失败（非JSON）：{path}") from exc
    if not isinstance(data, dict):
        raise AuthRecoveryError(f"认证文件结构异常：{path}")
    return data


def _ask_phone_and_code(default_phone: str = "") -> tuple[str, str]:
    try:
        import tkinter as tk
        from tkinter import simpledialog
    except Exception:  # noqa: BLE001
        return default_phone, ""

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    phone = simpledialog.askstring("登录修复", "请输入手机号（可留空后手动登录）：", initialvalue=default_phone or "")
    code = simpledialog.askstring("登录修复", "请输入短信验证码（可留空后手动输入）：", initialvalue="")
    root.destroy()
    return str(phone or "").strip(), str(code or "").strip()


def _ask_code_only() -> str:
    try:
        import tkinter as tk
        from tkinter import simpledialog
    except Exception:  # noqa: BLE001
        return ""

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    code = simpledialog.askstring("登录修复", "若已收到短信验证码，请输入（留空表示继续手动登录）：", initialvalue="")
    root.destroy()
    return str(code or "").strip()


def _log(msg: str) -> None:
    print(f"[AUTH] {msg}", flush=True)


def _looks_like_sms_request_url(url: str) -> bool:
    lowered = str(url or "").lower()
    return any(x in lowered for x in SMS_URL_HINTS)


def _targets(page: Any) -> List[Any]:
    out: List[Any] = [page]
    for frame in list(getattr(page, "frames", []) or []):
        if frame not in out:
            out.append(frame)
    return out


def _fill_first(page: Any, selectors: Sequence[str], value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    for root in _targets(page):
        for selector in selectors:
            try:
                loc = root.locator(selector).first
                if loc.count() > 0:
                    loc.fill(text, timeout=1500)
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _click_first(page: Any, selectors: Sequence[str]) -> bool:
    for root in _targets(page):
        for selector in selectors:
            try:
                loc = root.locator(selector).first
                if loc.count() > 0:
                    loc.click(timeout=1500)
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _activate_sms_login(page: Any) -> bool:
    sms_tab_selectors = [
        "text=短信验证码登录",
        "text=验证码登录",
        "text=手机验证码",
        "button:has-text('验证码登录')",
        "a:has-text('验证码登录')",
    ]
    return _click_first(page, sms_tab_selectors)


def _ensure_agreement_checked(page: Any) -> bool:
    clicked = False
    agreement_text_selectors = [
        "label:has-text('同意')",
        "text=我已阅读并同意",
        "text=同意《",
        "text=已阅读并同意",
    ]
    if _click_first(page, agreement_text_selectors):
        clicked = True
    # Fallback: click first visible unchecked checkbox.
    for root in _targets(page):
        try:
            loc = root.locator("input[type='checkbox']")
            n = loc.count()
            for i in range(min(n, 3)):
                item = loc.nth(i)
                try:
                    checked = bool(item.is_checked())
                except Exception:  # noqa: BLE001
                    checked = False
                if not checked:
                    item.click(timeout=1200)
                    return True
        except Exception:  # noqa: BLE001
            continue
    return clicked


def _try_autofill_login(page: Any, phone: str, code: str) -> Dict[str, bool]:
    phone_selectors = [
        "input[type='tel']",
        "input[name*='mobile']",
        "input[name*='phone']",
        "input[name*='username']",
        "input[placeholder*='手机号']",
        "input[placeholder*='手机']",
    ]
    code_selectors = [
        "input[name*='code']",
        "input[placeholder*='验证码']",
        "input[id*='code']",
    ]
    send_selectors = [
        "button:has-text('发送验证码')",
        "button:has-text('获取验证码')",
        "text=发送验证码",
        "text=获取验证码",
    ]
    login_selectors = [
        "button:has-text('登录')",
        "button:has-text('确定')",
        "text=登录",
        "text=确定",
    ]
    switched = _activate_sms_login(page)
    phone_filled = _fill_first(page, phone_selectors, phone)
    _ensure_agreement_checked(page)
    send_clicked = False
    if phone:
        send_clicked = _click_first(page, send_selectors)
    code_filled = _fill_first(page, code_selectors, code)
    login_clicked = False
    if code:
        login_clicked = _click_first(page, login_selectors)
    return {
        "sms_tab": switched,
        "phone_filled": phone_filled,
        "send_clicked": send_clicked,
        "code_filled": code_filled,
        "login_clicked": login_clicked,
    }


@dataclass(frozen=True)
class RecoverySettings:
    extra_auth_file: Path
    output: Path
    pc_login_url: str
    fenxi_url: str
    timeout_seconds: int
    browser_channel: str
    phone: str
    sms_code: str
    ask_sms: bool
    auto_fill: bool
    skip_pc: bool
    skip_fenxi: bool


def recover_auth(settings: RecoverySettings) -> Dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        raise AuthRecoveryError(
            "缺少 playwright 运行环境，请先执行: uv add playwright && uv run playwright install chromium"
        ) from exc

    existing = _load_existing(settings.extra_auth_file)
    phone = str(settings.phone or "").strip()
    sms_code = str(settings.sms_code or "").strip()
    if settings.ask_sms:
        phone, sms_code = _ask_phone_and_code(phone)

    pc_bearer_candidates: List[Dict[str, Any]] = []
    pc_origin = "http://yadmin.4399.com"
    pc_referer = "http://yadmin.4399.com/"
    fenxi_cookie_token = ""
    fenxi_jsessionid = ""
    pc_admin_token = ""
    sms_events: List[str] = []

    with sync_playwright() as p:
        launch_args: Dict[str, Any] = {"headless": False}
        if settings.browser_channel:
            launch_args["channel"] = settings.browser_channel
        browser = p.chromium.launch(**launch_args)
        context = browser.new_context(ignore_https_errors=True)

        def _on_request(req: Any) -> None:
            nonlocal pc_origin, pc_referer
            url = str(req.url or "")
            lowered = url.lower()
            headers = {str(k).lower(): str(v) for k, v in (req.headers or {}).items()}
            if "yapiadmin.4399.com" in lowered or "yadmin.4399.com" in lowered:
                bearer = headers.get("bearer", "")
                if bearer:
                    pc_bearer_candidates.append(
                        {
                            "url": url,
                            "bearer": bearer,
                            "origin": headers.get("origin", ""),
                            "referer": headers.get("referer", ""),
                            "ts": int(time.time() * 1000),
                        }
                    )
                if headers.get("origin"):
                    pc_origin = headers["origin"]
                if headers.get("referer"):
                    pc_referer = headers["referer"]
            if _looks_like_sms_request_url(url):
                sms_events.append(url)

        context.on("request", _on_request)

        pages: List[Any] = []
        if not settings.skip_pc:
            pc_page = context.new_page()
            pc_page.goto(settings.pc_login_url, wait_until="domcontentloaded")
            pages.append(pc_page)
        if not settings.skip_fenxi:
            fenxi_page = context.new_page()
            fenxi_page.goto(settings.fenxi_url, wait_until="domcontentloaded")
            pages.append(fenxi_page)

        if settings.auto_fill and (phone or sms_code):
            _log("开始自动填写登录信息")
            auto_stats: List[Dict[str, bool]] = []
            for page in pages:
                auto_stats.append(_try_autofill_login(page, phone=phone, code=sms_code))
            if auto_stats:
                _log(f"自动填写结果: {auto_stats}")
            if phone:
                time.sleep(2.0)
                if not sms_events:
                    _log("未检测到发送验证码请求，请在页面手动点击“发送验证码”并完成登录。")
            if settings.ask_sms and phone and (not sms_code):
                sms_code = _ask_code_only()
                if sms_code:
                    _log("收到验证码输入，尝试自动提交登录")
                    for page in pages:
                        _try_autofill_login(page, phone=phone, code=sms_code)

        deadline = time.time() + max(30, int(settings.timeout_seconds))
        while time.time() < deadline:
            cookies = context.cookies()
            for ck in cookies:
                domain = str(ck.get("domain") or "").lstrip(".").lower()
                name = str(ck.get("name") or "")
                value = str(ck.get("value") or "")
                if domain.endswith("fenxi.4399dev.com"):
                    if name == "e_token" and value:
                        fenxi_cookie_token = value
                    if name == "JSESSIONID" and value:
                        fenxi_jsessionid = value
                if domain.endswith("yadmin.4399.com") or domain.endswith("yapiadmin.4399.com"):
                    if name == "Admin-Token" and value:
                        pc_admin_token = value

            pc_ready = settings.skip_pc
            best_pc = _choose_best_pc_bearer(pc_bearer_candidates)
            if best_pc and (pc_admin_token or _extract_token_from_bearer(str(best_pc.get("bearer") or ""))):
                pc_ready = True
            fenxi_ready = settings.skip_fenxi or bool(fenxi_cookie_token)
            if pc_ready and fenxi_ready:
                break
            time.sleep(1.0)

        browser.close()

    best_pc = _choose_best_pc_bearer(pc_bearer_candidates)
    payload: Dict[str, Any] = dict(existing)
    pc_updated = False
    fenxi_updated = False

    if not settings.skip_pc:
        if not best_pc:
            raise AuthRecoveryError("未捕获到 PC Bearer 请求，请确认已在 yadmin 页面完成登录并触发业务请求。")
        bearer = str(best_pc.get("bearer") or "").strip()
        token_from_bearer = _extract_token_from_bearer(bearer)
        admin_token = pc_admin_token or token_from_bearer
        if not admin_token:
            raise AuthRecoveryError("未捕获到 PC Admin-Token。")
        pc_block = {
            "cookies": {"Admin-Token": admin_token},
            "headers": {
                "Origin": str(best_pc.get("origin") or pc_origin or "http://yadmin.4399.com"),
                "Referer": str(best_pc.get("referer") or pc_referer or "http://yadmin.4399.com/"),
                "Bearer": bearer,
            },
            "token": "",
            "bootstrap_url_template": "",
        }
        payload["pc_web"] = pc_block
        pc_updated = True

    if not settings.skip_fenxi:
        if not fenxi_cookie_token:
            raise AuthRecoveryError("未捕获到 fenxi e_token，请确认已在 fenxi 页面完成短信登录。")
        fenxi_existing = payload.get("fenxi") if isinstance(payload.get("fenxi"), dict) else {}
        fenxi_headers = dict(fenxi_existing.get("headers") or {}) if isinstance(fenxi_existing.get("headers"), dict) else {}
        fenxi_headers.setdefault("mediaids", "media-eb40cb50d15a49a9")
        fenxi_headers.setdefault("topic", "gamebox_event")
        fenxi_cookies = dict(fenxi_existing.get("cookies") or {}) if isinstance(fenxi_existing.get("cookies"), dict) else {}
        fenxi_cookies["e_token"] = fenxi_cookie_token
        if fenxi_jsessionid:
            fenxi_cookies["JSESSIONID"] = fenxi_jsessionid
        payload["fenxi"] = {
            "cookies": fenxi_cookies,
            "headers": fenxi_headers,
            "token": str(fenxi_existing.get("token") or ""),
            "bootstrap_url_template": str(fenxi_existing.get("bootstrap_url_template") or ""),
        }
        fenxi_updated = True

    payload.setdefault("505", {"cookies": {}, "headers": {}, "token": "", "bootstrap_url_template": ""})
    payload.setdefault("fenxi", {"cookies": {}, "headers": {}, "token": "", "bootstrap_url_template": ""})
    payload.setdefault("pc_web", {"cookies": {}, "headers": {}, "token": "", "bootstrap_url_template": ""})
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    updated_meta = dict(meta)
    updated_meta.update(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "playwright_auth_recovery",
        }
    )
    payload["meta"] = updated_meta

    settings.output.parent.mkdir(parents=True, exist_ok=True)
    settings.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "output_path": str(settings.output),
        "pc_updated": pc_updated,
        "fenxi_updated": fenxi_updated,
        "sms_event_count": len(sms_events),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="通过 Playwright 引导登录并自动回填 fenxi + pc_web 认证信息。")
    parser.add_argument("--extra-auth-file", type=Path, default=Path("extra_auth.json"))
    parser.add_argument("--output", type=Path, default=Path("extra_auth.json"))
    parser.add_argument("--pc-login-url", type=str, default="http://yadmin.4399.com/")
    parser.add_argument("--fenxi-url", type=str, default="https://fenxi.4399dev.com/analysis/")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--browser-channel", type=str, default="")
    parser.add_argument("--phone", type=str, default="")
    parser.add_argument("--sms-code", type=str, default="")
    parser.add_argument("--ask-sms", action="store_true", help="弹窗询问手机号和短信验证码。")
    parser.add_argument("--no-auto-fill", action="store_true", help="不自动填充手机号/验证码，仅手动登录。")
    parser.add_argument("--skip-pc", action="store_true")
    parser.add_argument("--skip-fenxi", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    settings = RecoverySettings(
        extra_auth_file=args.extra_auth_file,
        output=args.output,
        pc_login_url=str(args.pc_login_url or "").strip(),
        fenxi_url=str(args.fenxi_url or "").strip(),
        timeout_seconds=int(args.timeout_seconds),
        browser_channel=str(args.browser_channel or "").strip(),
        phone=str(args.phone or "").strip(),
        sms_code=str(args.sms_code or "").strip(),
        ask_sms=bool(args.ask_sms),
        auto_fill=not bool(args.no_auto_fill),
        skip_pc=bool(args.skip_pc),
        skip_fenxi=bool(args.skip_fenxi),
    )
    result = recover_auth(settings)
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
