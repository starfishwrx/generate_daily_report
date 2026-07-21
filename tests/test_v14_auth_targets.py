from types import SimpleNamespace

from auth_repair import _browser_login_completed, _extract_cookies, _hosts_from_urls, resolve_repair_targets
from generate_daily_report import select_auth_repair_target


def test_first_setup_targets_all_platforms() -> None:
    assert resolve_repair_targets("all") == {"870", "fenxi", "505", "pc_web"}


def test_auth_failure_selects_specific_platform() -> None:
    assert resolve_repair_targets("auto", "870登录态不可用: PHPSESSID expired") == {"870"}
    assert resolve_repair_targets("auto", "505后台登录态预检失败") == {"505"}


def test_cookie_capture_uses_configured_domain() -> None:
    domains = _hosts_from_urls("http://reports.internal/login")
    cookies = [
        {"domain": ".reports.internal", "name": "PHPSESSID", "value": "abc"},
        {"domain": ".other.internal", "name": "PHPSESSID", "value": "wrong"},
    ]
    assert _extract_cookies(cookies, domains) == {"PHPSESSID": "abc"}


def test_runtime_retry_opens_the_failed_platform() -> None:
    assert select_auth_repair_target(["870"]) == "870"
    assert select_auth_repair_target(["505"]) == "505"
    assert select_auth_repair_target(["fenxi", "pc_web"]) == "both"


def test_stale_cookie_does_not_complete_before_login_redirect() -> None:
    context = SimpleNamespace(pages=[SimpleNamespace(url="http://admin.internal/?m=user&ac=login")])
    assert _browser_login_completed(context, "http://admin.internal/?m=user&ac=login") is False
    context.pages[0].url = "http://admin.internal/?m=index&ac=dashboard"
    assert _browser_login_completed(context, "http://admin.internal/?m=user&ac=login") is True


def test_root_login_url_rejects_html_redirect_to_login() -> None:
    page = SimpleNamespace(
        url="https://admin.buke999.com",
        content=lambda: "<script>top.window.location.href='https://admin.buke999.com/?ac=login';</script>",
    )
    context = SimpleNamespace(pages=[page])
    assert _browser_login_completed(context, "https://admin.buke999.com") is False
