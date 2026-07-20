#!/usr/bin/env python3
"""
generate_daily_report.py

Fetches timeseries metrics from the 870 data interface, aggregates peak values,
and renders the daily cloud gaming report using a Jinja2 template.
"""

from __future__ import annotations

import asyncio
import argparse
import html
import json
import logging
import math
import os
import re
import sys
import time as monotonic_time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union
import requests
import yaml
from app_paths import ensure_first_run_config, migrate_legacy_runtime_files, resolve_app_paths
from dateutil import parser as date_parser
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from auth_repair import AgentRepairCoordinator, AuthRepairSettings, classify_auth_failure
from extra_auth import build_extra_auth_file, inspect_fenxi_token, load_extra_auth, load_extra_auth_meta
from extra_metrics_render import render_extra_metrics_block, render_payment_table_images
from extra_metrics_service import ExtraMetricsService, ExtraSettings
from feishu_doc import FeishuDocError, FeishuDocSettings, publish_report_to_feishu_doc
from network_hosts import load_hosts_map, rewrite_url_with_hosts_map
from pc_web_metrics_service import PCWebMetricsService, PCWebSettings
from publish_state import PublishStateStore, content_hash
from run_lock import AlreadyRunningError, single_instance_lock
from tz_compat import get_tzinfo
from wecom_longbot import WeComBotError, WeComBotSettings, publish_reports_to_wecom


_FONT_CONFIGURED = False


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def bundle_base_path() -> Path:
    if is_frozen() and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def executable_base_path() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_extra_auth_path() -> Path:
    return resolve_app_paths().extra_auth


def default_config_path() -> Path:
    return resolve_app_paths().config


def default_template_dir() -> Path:
    if is_frozen():
        exe_templates = executable_base_path() / "templates"
        if exe_templates.exists():
            return exe_templates
        cwd_templates = Path.cwd() / "templates"
        if cwd_templates.exists():
            return cwd_templates
    return bundle_base_path() / "templates"


def default_output_dir() -> Path:
    return resolve_app_paths().output


DEFAULT_CONFIG_PATH = default_config_path()
DEFAULT_TEMPLATE_DIR = default_template_dir()
DEFAULT_TEMPLATE_NAME = "report_template.j2"
DEFAULT_OUTPUT_DIR = default_output_dir()
DEFAULT_EXTRA_AUTH_FILE = default_extra_auth_path()
DEFAULT_CHART_DIR = DEFAULT_OUTPUT_DIR / "charts"
DEFAULT_TIME_FIELD = "ctime"
_FONT_CONFIGURED = False
DEFAULT_TEMPLATE_CONTENT = """{{ report_date_cn }}游戏盒云游戏数据
  一、游戏盒云游戏相关数据日报
  总并发最高峰值：{{ targets.total.concurrency.formatted_peak_value }}，时间：{{ targets.total.concurrency.peak_time_label }}。
  总排队最高峰值：{{ targets.total.queue.formatted_peak_value }}，时间：{{ targets.total.queue.peak_time_label }}。
  {{ targets.total.queue_summary }}
{% for sentence in analysis_sentences %}
  {{ sentence }}
{% endfor -%}
[总路线图片]
  ——————————————————————
  页游总并发峰值：{{ targets.page.concurrency.formatted_peak_value }}，时间：{{ targets.page.concurrency.peak_time_label }}。
  {{ targets.page.queue_summary }}
[页游图片]
  ——————————————————————
  主机总并发峰值：{{ targets.console.concurrency.formatted_peak_value }}，时间：{{ targets.console.concurrency.peak_time_label }}。
  主机排队最高峰值：{{ targets.console.queue.formatted_peak_value }}，时间：{{ targets.console.queue.peak_time_label }}。
  {{ targets.console.queue_summary }}
[主机图片]
  ——————————————————————
  手游总并发峰值：{{ targets.mobile.concurrency.formatted_peak_value }}，时间：{{ targets.mobile.concurrency.peak_time_label }}。
  手游排队最高峰值：{{ targets.mobile.queue.formatted_peak_value }}，时间：{{ targets.mobile.queue.peak_time_label }}。
  {{ targets.mobile.queue_summary }}
[手游图片]
  ——————————————————————
  原神总并发峰值：{{ targets.genshin.concurrency.formatted_peak_value }}，时间：{{ targets.genshin.concurrency.peak_time_label }}。
  原神排队最高峰值：{{ targets.genshin.queue.formatted_peak_value }}，时间：{{ targets.genshin.queue.peak_time_label }}。
  {{ targets.genshin.queue_summary }}
[原神图片]
  ——————————————————————
  崩坏星穹铁道总并发峰值：{{ targets.starrail.concurrency.formatted_peak_value }}，时间：{{ targets.starrail.concurrency.peak_time_label }}。
  崩坏星穹铁道排队最高峰值：{{ targets.starrail.queue.formatted_peak_value }}，时间：{{ targets.starrail.queue.peak_time_label }}。
  {{ targets.starrail.queue_summary }}
[星铁图片]
  ——————————————————————
  绝区零总并发峰值：{{ targets.zzz.concurrency.formatted_peak_value }}，时间：{{ targets.zzz.concurrency.peak_time_label }}。
  绝区零排队最高峰值：{{ targets.zzz.queue.formatted_peak_value }}，时间：{{ targets.zzz.queue.peak_time_label }}。
  {{ targets.zzz.queue_summary }}
[绝区零图片]
  ——————————————————————
  高画质总并发峰值：{{ targets.high_quality.concurrency.formatted_peak_value }}，时间：{{ targets.high_quality.concurrency.peak_time_label }}。
  高画质排队最高峰值：{{ targets.high_quality.queue.formatted_peak_value }}，时间：{{ targets.high_quality.queue.peak_time_label }}。
  {{ targets.high_quality.queue_summary }}
[高画质图片]
  ——————————————————————
  PC云游戏总并发峰值：{{ targets.pc_cloud.concurrency.formatted_peak_value }}，时间：{{ targets.pc_cloud.concurrency.peak_time_label }}。
  PC云游戏总排队峰值：{{ targets.pc_cloud.queue.formatted_peak_value }}，时间：{{ targets.pc_cloud.queue.peak_time_label }}。
  {{ targets.pc_cloud.queue_summary }}
[pc云游戏图片]
"""

DEFAULT_PC_TEMPLATE_NAME = "pc_report_template.j2"
DEFAULT_PC_TEMPLATE_CONTENT = """{{ report_date_cn }}游戏盒PC云游戏数据
一、游戏盒PC云游戏相关数据日报
PC云游戏总并发峰值：{{ target.concurrency.formatted_peak_value }}，时间：{{ target.concurrency.peak_time_label }}。
PC云游戏总排队峰值：{{ target.queue.formatted_peak_value }}，时间：{{ target.queue.peak_time_label }}。
{{ target.queue_summary }}
[pc云游戏图片]

备注：
1、游戏的新增用户数为：{{ pc_notes.new_users_text }}，游戏的活跃用户数为：{{ pc_notes.active_users_text }}。
2、会员充值人数：{{ pc_member_summary.recharge_count_text }}，PC首开会员人数：{{ pc_member_summary.first_count_text }}，充值金额：{{ pc_member_summary.recharge_amount_text }}元，环比上周同期{{ pc_member_summary.week_trend_text }}。
{% if pc_warnings %}
备注：部分PC外部接口未取到数据 -> {{ pc_warnings | join('；') }}
{% endif %}

二、云游戏活跃用户top(去重)
{% if pc_top_games %}
| 游戏 | 活跃用户数 |
| :---: | :---: |
{% for item in pc_top_games %}
| {{ item.name }} | {{ item.active_users_text }} |
{% endfor %}
{% else %}
| 游戏 | 活跃用户数 |
| :---: | :---: |
| 暂未获取 | 暂未获取 |
{% endif %}
"""

class ReportError(Exception):
    """Custom exception for report generation failures."""


@dataclass
class TimePoint:
    """Normalized single data point on a time series."""

    timestamp: Optional[datetime]
    raw_label: str
    hour: Optional[int]
    value: float

    def sort_key(self) -> Tuple[int, Union[datetime, str]]:
        """Key for chronological sorting."""
        if self.timestamp is not None:
            return (0, self.timestamp)
        return (1, self.raw_label)


@dataclass
class MetricSummary:
    """Aggregated statistics for a metric."""

    series: List[TimePoint] = field(default_factory=list)
    peak_point: Optional[TimePoint] = None
    formatted_peak_value: str = "0"
    peak_time_label: str = "无"

    @property
    def peak_value(self) -> Optional[float]:
        return None if self.peak_point is None else self.peak_point.value


@dataclass
class TargetResult:
    """Final result for one target (e.g., 总, 手游, 原神)."""

    key: str
    label: str
    concurrency: MetricSummary = field(default_factory=MetricSummary)
    queue: MetricSummary = field(default_factory=MetricSummary)
    queue_summary: str = ""
    total_queue_value: float = 0.0
    chart_path: Optional[Path] = None
    previous_concurrency_peak: Optional[float] = None
    previous_queue_peak: Optional[float] = None


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate daily cloud gaming report.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Runtime data directory (config, auth, output). Defaults to the user-local app directory in packaged builds.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to YAML configuration file.",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Report date in YYYY-MM-DD (defaults to today).",
    )
    parser.add_argument(
        "--cookie",
        type=str,
        default=None,
        help="Override PHP session cookie (format: PHPSESSID=...).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write the rendered report and assets.",
    )
    parser.add_argument(
        "--template-dir",
        type=Path,
        default=DEFAULT_TEMPLATE_DIR,
        help="Directory that contains report templates.",
    )
    parser.add_argument(
        "--template-name",
        type=str,
        default=DEFAULT_TEMPLATE_NAME,
        help="Template filename to render.",
    )
    parser.add_argument(
        "--no-charts",
        action="store_true",
        help="Skip chart generation even if enabled in config.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument(
        "--no-runtime-gui",
        action="store_true",
        help="Disable runtime tkinter prompt for cookie/date input.",
    )
    parser.add_argument(
        "--with-extra-metrics",
        action="store_true",
        help="Fetch extra metrics from fenxi/505 backends (optional).",
    )
    parser.add_argument(
        "--extra-auth-file",
        type=Path,
        default=DEFAULT_EXTRA_AUTH_FILE,
        help="Path to extra auth JSON for fenxi/505/pc_web.",
    )
    parser.add_argument(
        "--build-extra-auth",
        action="store_true",
        help="Build extra auth JSON from HAR files before querying extra metrics.",
    )
    parser.add_argument(
        "--build-extra-auth-only",
        action="store_true",
        help="Build extra auth JSON and exit without querying report data.",
    )
    parser.add_argument(
        "--check-extra-auth",
        action="store_true",
        help="Only check full auth status and exit (870 + fenxi/505 + pc_web).",
    )
    parser.add_argument(
        "--skip-870-auth-preflight",
        action="store_true",
        help="Skip the 870 session-cookie preflight; used by fenxi/PC auth repair paths.",
    )
    parser.add_argument(
        "--repair-auth-on-failure",
        action="store_true",
        help="Open a dedicated Chrome auth repair window when fenxi/PC Web auth preflight fails.",
    )
    parser.add_argument(
        "--repair-auth-only",
        action="store_true",
        help="Run the fenxi/PC Web Chrome auth repair flow, then exit after preflight.",
    )
    parser.add_argument(
        "--auth-repair-browser",
        type=str,
        default=None,
        help="Browser for auth repair (first version supports chrome).",
    )
    parser.add_argument(
        "--auth-repair-profile",
        type=Path,
        default=None,
        help="Dedicated Chrome profile directory for auth repair.",
    )
    parser.add_argument(
        "--auth-repair-timeout-seconds",
        type=int,
        default=None,
        help="Seconds to wait for manual browser login during auth repair.",
    )
    parser.add_argument(
        "--auth-repair-target",
        choices=["auto", "fenxi", "pc_web", "both"],
        default=None,
        help="Auth repair target. auto infers from the failure; no-failure repair defaults to both.",
    )
    parser.add_argument(
        "--extra-auth-max-age-hours",
        type=int,
        default=24,
        help="Warn when extra auth file is older than this hour threshold (default: 24).",
    )
    parser.add_argument(
        "--fenxi-har",
        action="append",
        default=[],
        help="fenxi HAR file path. Can be specified multiple times.",
    )
    parser.add_argument(
        "--manage-har",
        action="append",
        default=[],
        help="505/manage HAR file path. Can be specified multiple times.",
    )
    parser.add_argument(
        "--hosts-yaml-path",
        type=str,
        default=None,
        help="Optional hosts YAML path for fenxi/505 extension requests.",
    )
    parser.add_argument(
        "--query-proxy-url",
        type=str,
        default=None,
        help="Optional proxy URL for fenxi/505 extension requests.",
    )
    parser.add_argument(
        "--proxy-mode",
        type=str,
        choices=["direct", "system", "custom"],
        default=None,
        help="870 request proxy mode: direct/system/custom.",
    )
    parser.add_argument(
        "--http-proxy",
        type=str,
        default=None,
        help="870 HTTP proxy URL when --proxy-mode=custom.",
    )
    parser.add_argument(
        "--https-proxy",
        type=str,
        default=None,
        help="870 HTTPS proxy URL when --proxy-mode=custom.",
    )
    parser.add_argument(
        "--network-hosts-yaml",
        type=str,
        default=None,
        help="Optional hosts YAML path for 870 requests.",
    )
    parser.add_argument(
        "--push-feishu-doc",
        action="store_true",
        help="Publish final report text to Feishu Doc.",
    )
    parser.add_argument(
        "--no-push-feishu-doc",
        action="store_true",
        help="Disable Feishu doc publish for this run.",
    )
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="Generate and validate reports without publishing to Feishu or WeCom.",
    )
    parser.add_argument(
        "--force-publish",
        action="store_true",
        help="Ignore completed publish state and publish again.",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=4,
        help="Maximum concurrent data requests (default: 4).",
    )
    parser.add_argument(
        "--push-report-file",
        type=Path,
        default=None,
        help="Publish an existing report text file to Feishu Doc and exit.",
    )
    parser.add_argument(
        "--push-pc-report-file",
        type=Path,
        default=None,
        help="Publish an existing PC report text file to Feishu Doc and exit.",
    )
    parser.add_argument(
        "--feishu-app-id",
        type=str,
        default=None,
        help="Feishu app_id (overrides env/config).",
    )
    parser.add_argument(
        "--feishu-app-secret",
        type=str,
        default=None,
        help="Feishu app_secret (overrides env/config).",
    )
    parser.add_argument(
        "--feishu-folder-token",
        type=str,
        default=None,
        help="Feishu folder token for new docs (optional).",
    )
    parser.add_argument(
        "--feishu-doc-title",
        type=str,
        default=None,
        help="Custom Feishu doc title (optional).",
    )
    parser.add_argument(
        "--feishu-doc-url-prefix",
        type=str,
        default=None,
        help="Feishu doc URL prefix for output link (default: https://www.feishu.cn/docx/).",
    )
    parser.add_argument(
        "--verify-feishu-content",
        action="store_true",
        help="Verify pushed Feishu doc content via docs/v1/content.",
    )
    parser.add_argument(
        "--push-wecom-reports",
        action="store_true",
        help="Push existing main/pc report files for the date to WeCom long bot and exit.",
    )
    parser.add_argument(
        "--wecom-target",
        choices=["single", "group"],
        default=None,
        help="WeCom long bot target for push-only mode.",
    )
    return parser.parse_args(argv)


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s - %(message)s")


def emit_progress(percent: int, message: str) -> None:
    pct = max(0, min(100, int(percent)))
    logging.info("[PROGRESS] %d|%s", pct, message)


def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise ReportError(f"Configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ReportError(f"Failed to parse YAML config: {exc}") from exc
    if not isinstance(data, dict):
        raise ReportError("Configuration root must be a mapping.")
    return data


def resolve_report_date(date_arg: Optional[str]) -> date:
    if date_arg:
        try:
            parsed = date_parser.parse(date_arg).date()
        except (ValueError, TypeError) as exc:
            raise ReportError(f"Invalid --date value: {date_arg}") from exc
        return parsed
    return datetime.now().date()


def resolve_cookie(cli_cookie: Optional[str], config: Dict[str, Any]) -> str:
    if cli_cookie:
        return cli_cookie.strip()
    cookie = config.get("session_cookie") or os.getenv("REPORT_PHPSESSID")
    if not cookie:
        raise ReportError("Session cookie missing. Provide --cookie or set session_cookie in config.")
    return cookie.strip()


def configure_870_session(
    session: requests.Session,
    args: argparse.Namespace,
    config: Dict[str, Any],
) -> None:
    network_cfg = config.get("network") or {}
    if not isinstance(network_cfg, dict):
        raise ReportError("Config field network must be a mapping when provided.")

    mode = str(args.proxy_mode or network_cfg.get("proxy_mode") or "direct").strip().lower()
    if mode not in {"direct", "system", "custom"}:
        raise ReportError("proxy_mode must be one of: direct/system/custom.")

    if mode == "system":
        session.trust_env = True
        logging.info("870 network mode: system proxy")
        return

    session.trust_env = False
    if mode == "direct":
        logging.info("870 network mode: direct")
        return

    http_proxy = (
        args.http_proxy
        if args.http_proxy is not None
        else str(network_cfg.get("http_proxy") or "")
    ).strip()
    https_proxy = (
        args.https_proxy
        if args.https_proxy is not None
        else str(network_cfg.get("https_proxy") or "")
    ).strip()
    if not http_proxy and not https_proxy:
        raise ReportError("proxy_mode=custom requires --http-proxy/--https-proxy or config network.http_proxy/network.https_proxy.")
    proxies: Dict[str, str] = {}
    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy
    session.proxies.update(proxies)
    logging.info("870 network mode: custom proxy (%s)", ",".join(sorted(proxies.keys())))


def ensure_output_dirs(base_dir: Path) -> Tuple[Path, Path]:
    base_dir.mkdir(parents=True, exist_ok=True)
    charts_dir = base_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    return base_dir, charts_dir


def build_auto_query_params(
    auto_config: Optional[Dict[str, Any]],
    report_date: date,
) -> Dict[str, str]:
    if not auto_config:
        return {}
    auto_params: Dict[str, str] = {}
    for name, settings in auto_config.items():
        if isinstance(settings, str):
            fmt = settings
            offset_days = 0
        elif isinstance(settings, dict):
            fmt = settings.get("format", "%Y-%m-%d")
            offset_days = settings.get("offset_days", 0)
            if not isinstance(offset_days, (int, float)):
                raise ReportError(f"offset_days for {name} must be numeric.")
        else:
            raise ReportError(f"Unsupported auto_query_params entry for {name!r}.")
        target_date = report_date + timedelta(days=offset_days)
        try:
            value = target_date.strftime(fmt)
        except Exception as exc:  # pylint: disable=broad-except
            raise ReportError(f"Invalid format for auto query param {name!r}: {fmt}") from exc
        auto_params[name] = value
    return auto_params


def prompt_runtime_inputs(
    default_cookie: Optional[str],
    default_date: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """Display a small GUI to collect PHPSESSID and optional report date."""
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except Exception as exc:  # pylint: disable=broad-except
        logging.warning("tkinter unavailable (%s); skipping GUI prompt.", exc)
        return default_cookie, default_date

    try:
        root = tk.Tk()
    except Exception as exc:  # pylint: disable=broad-except
        logging.warning("tkinter GUI unavailable (%s); skipping GUI prompt.", exc)
        return default_cookie, default_date

    root.title("云游戏日报参数")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    cookie_var = tk.StringVar(master=root, value=default_cookie or "")
    date_var = tk.StringVar(master=root, value=default_date or "")
    submitted: Dict[str, bool] = {"done": False}

    frame = ttk.Frame(root, padding=16)
    frame.grid(row=0, column=0, sticky="nsew")

    ttk.Label(frame, text="PHPSESSID:").grid(row=0, column=0, sticky="w", pady=(0, 8))
    cookie_entry = ttk.Entry(frame, width=50, textvariable=cookie_var)
    cookie_entry.grid(row=0, column=1, sticky="ew", pady=(0, 8))

    ttk.Label(frame, text="报表日期 (YYYY-MM-DD，可选):").grid(row=1, column=0, sticky="w")
    date_entry = ttk.Entry(frame, width=30, textvariable=date_var)
    date_entry.grid(row=1, column=1, sticky="ew")

    button_bar = ttk.Frame(frame)
    button_bar.grid(row=2, column=0, columnspan=2, pady=(12, 0), sticky="e")

    def on_submit() -> None:
        value = cookie_var.get().strip()
        if not value:
            messagebox.showerror("缺少 Cookie", "PHPSESSID 不能为空。")
            return
        submitted["done"] = True
        root.destroy()

    def on_cancel() -> None:
        submitted["done"] = False
        root.destroy()

    ttk.Button(button_bar, text="取消", command=on_cancel).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(button_bar, text="确认", command=on_submit).grid(row=0, column=1)

    root.protocol("WM_DELETE_WINDOW", on_cancel)
    cookie_entry.focus_set()
    root.mainloop()

    if not submitted["done"]:
        return default_cookie, default_date

    new_cookie = cookie_var.get().strip() or default_cookie
    new_date = date_var.get().strip() or default_date
    return new_cookie, new_date


def format_value(value: Optional[float]) -> str:
    if value is None:
        return "0"
    if math.isclose(value, round(value)):
        return f"{int(round(value))}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def display_optional(value: Optional[float]) -> str:
    return format_value(value) if value is not None else "—"


def prepare_template_directory(template_dir: Path, template_name: str, default_content: str) -> Path:
    template_path = template_dir / template_name
    if template_path.exists():
        return template_dir
    fallback_dir = Path.cwd() / "templates"
    fallback_dir.mkdir(parents=True, exist_ok=True)
    fallback_path = fallback_dir / template_name
    if not fallback_path.exists():
        fallback_path.write_text(default_content, encoding="utf-8")
    logging.warning(
        "Template %s not found in %s. Using fallback at %s.",
        template_name,
        template_dir,
        fallback_path,
    )
    return fallback_dir


def _parse_iso_datetime(raw: str) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def get_extra_auth_age_hours(path: Path, meta: Dict[str, Any]) -> Optional[float]:
    generated_at = _parse_iso_datetime(str(meta.get("generated_at") or ""))
    if generated_at is None:
        try:
            generated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None
    now_utc = datetime.now(timezone.utc)
    delta = now_utc - generated_at.astimezone(timezone.utc)
    return max(0.0, delta.total_seconds() / 3600.0)


def _format_dt_with_tz(dt: datetime, tz_name: str) -> str:
    local_tz = get_tzinfo(tz_name)
    local_dt = dt.astimezone(local_tz)
    return local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def diagnose_fenxi_token(
    extra_auth: Dict[str, Dict[str, Any]],
    timezone_name: str,
    warn_threshold_hours: float = 6.0,
) -> Dict[str, Any]:
    diag = inspect_fenxi_token(
        extra_auth.get("fenxi"),
        warn_threshold_hours=warn_threshold_hours,
    )
    message = str(diag.get("reason") or "")
    iat = diag.get("iat")
    exp = diag.get("exp")
    if isinstance(iat, datetime) and isinstance(exp, datetime):
        message = (
            f"{message}; iat={_format_dt_with_tz(iat, timezone_name)}; "
            f"exp={_format_dt_with_tz(exp, timezone_name)}; "
            f"remaining_min={float(diag.get('remaining_minutes') or 0):.1f}"
        )
    elif isinstance(exp, datetime):
        message = (
            f"{message}; exp={_format_dt_with_tz(exp, timezone_name)}; "
            f"remaining_min={float(diag.get('remaining_minutes') or 0):.1f}"
        )
    diag["message"] = message
    return diag


def select_870_preflight_query(config: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    targets_config = config.get("targets") or {}
    if not isinstance(targets_config, dict) or not targets_config:
        raise ReportError("Config missing targets definitions.")
    ordered_section_keys = config.get("report_section_order") or list(targets_config.keys())
    for key in ordered_section_keys:
        target_cfg = targets_config.get(key)
        if not isinstance(target_cfg, dict):
            continue
        queries = target_cfg.get("queries") or []
        if not isinstance(queries, list):
            continue
        for query in queries:
            if isinstance(query, dict) and isinstance(query.get("params"), dict):
                return key, target_cfg, query
    raise ReportError("870预检失败：未找到可用查询配置。")


def preflight_870_auth(
    config: Dict[str, Any],
    args: argparse.Namespace,
    report_date: date,
) -> Dict[str, Any]:
    base_url = str(config.get("base_url") or "").strip()
    if not base_url:
        return {"ok": False, "message": "870登录态不可用: Config missing base_url."}

    try:
        cookie = resolve_cookie(args.cookie, config)
    except ReportError as exc:
        return {"ok": False, "message": f"870登录态不可用: {exc}"}

    timeout = float(config.get("timeout", 30))
    default_http_method = str(config.get("default_http_method", "post") or "post")
    auto_query_params = build_auto_query_params(config.get("auto_query_params"), report_date)
    try:
        target_key, target_cfg, query = select_870_preflight_query(config)
    except ReportError as exc:
        return {"ok": False, "message": f"870登录态不可用: {exc}"}

    hosts_yaml_path_870 = (
        args.network_hosts_yaml
        if args.network_hosts_yaml is not None
        else str((config.get("network") or {}).get("hosts_yaml_path") or "")
    ).strip()
    hosts_map_870 = load_hosts_map(hosts_yaml_path_870) if hosts_yaml_path_870 else {}

    params = dict(auto_query_params)
    params.update(query.get("params") or {})
    method = str(query.get("method") or default_http_method).strip() or default_http_method

    session = requests.Session()
    try:
        configure_870_session(session, args, config)
        session.headers.update({"Cookie": cookie, "User-Agent": config.get("user_agent", "Mozilla/5.0")})
        fetch_json(session, base_url, params, timeout, method, hosts_map=hosts_map_870)
    except Exception as exc:
        return {"ok": False, "message": f"870登录态不可用: {exc}"}
    finally:
        session.close()

    label = str(target_cfg.get("label") or target_key)
    return {"ok": True, "message": f"870登录态可用: {label}"}


def run_full_auth_preflight(
    config: Dict[str, Any],
    args: argparse.Namespace,
    extra_metrics_cfg: Dict[str, Any],
    extra_auth_file: Path,
) -> None:
    report_date_for_check = resolve_report_date(args.date)

    if not bool(getattr(args, "skip_870_auth_preflight", False)):
        auth_870 = preflight_870_auth(config, args, report_date_for_check)
        logging.info("870: %s", auth_870.get("message", ""))
        if not is_preflight_ok(auth_870.get("ok")):
            raise ReportError(str(auth_870.get("message") or "870登录态预检失败"))

    pc_web_cfg = config.get("pc_web_metrics") or {}
    needs_extension_preflight = bool(args.with_extra_metrics or extra_metrics_cfg.get("enabled") or pc_web_cfg.get("enabled"))
    if not needs_extension_preflight:
        return

    if not extra_auth_file.exists():
        raise ReportError(f"扩展登录态预检失败: 认证文件不存在：{extra_auth_file}")

    extra_auth = load_extra_auth(extra_auth_file)
    extra_auth_meta = load_extra_auth_meta(extra_auth_file)
    timezone_name = str(extra_metrics_cfg.get("timezone", "Asia/Shanghai"))
    fenxi_diag = diagnose_fenxi_token(extra_auth, timezone_name=timezone_name, warn_threshold_hours=6.0)
    logging.info("fenxi_token: %s", fenxi_diag.get("message", ""))

    auth_age_hours = get_extra_auth_age_hours(extra_auth_file, extra_auth_meta)
    if auth_age_hours is not None and auth_age_hours > float(args.extra_auth_max_age_hours):
        logging.warning(
            "扩展认证文件已超过%.1f小时（阈值=%d小时），建议重新手机验证码登录并刷新认证文件。",
            auth_age_hours,
            args.extra_auth_max_age_hours,
        )

    extra_settings = ExtraSettings(
        timezone=str(extra_metrics_cfg.get("timezone", "Asia/Shanghai")),
        request_timeout=int(extra_metrics_cfg.get("request_timeout", 30)),
        query_proxy_url=str((args.query_proxy_url if args.query_proxy_url is not None else extra_metrics_cfg.get("query_proxy_url", ""))).strip(),
        hosts_yaml_path=str((args.hosts_yaml_path if args.hosts_yaml_path is not None else extra_metrics_cfg.get("hosts_yaml_path", ""))).strip(),
        query_debug_log_path=(DEFAULT_OUTPUT_DIR / "query_debug.jsonl"),
        fenxi_base=str(extra_metrics_cfg.get("fenxi_base", "https://<FENXI_HOST>")).strip(),
        manage_base=str(extra_metrics_cfg.get("manage_base", "http://<MANAGE_HOST>")).strip(),
    )
    preflight = asyncio.run(
        ExtraMetricsService(extra_settings).preflight(
            query_date=report_date_for_check,
            fenxi_auth=extra_auth.get("fenxi"),
            manage_auth=extra_auth.get("505"),
        )
    )
    fenxi_ok = is_preflight_ok((preflight.get("fenxi") or {}).get("ok"))
    manage_ok = is_preflight_ok((preflight.get("505") or {}).get("ok"))
    if not bool(fenxi_diag.get("usable")):
        fenxi_ok = False
    logging.info("fenxi: %s", (preflight.get("fenxi") or {}).get("message", ""))
    logging.info("505: %s", (preflight.get("505") or {}).get("message", ""))
    if not bool(fenxi_diag.get("usable")):
        logging.error("fenxi token 预检失败: %s", fenxi_diag.get("message", ""))
    if not fenxi_ok:
        raise ReportError(f"分析后台登录态预检失败: {(preflight.get('fenxi') or {}).get('message', fenxi_diag.get('message', ''))}")
    if not manage_ok:
        raise ReportError(f"505后台登录态预检失败: {(preflight.get('505') or {}).get('message', '')}")

    if bool(pc_web_cfg.get("enabled")):
        strict_mode = bool(pc_web_cfg.get("strict", True))
        pc_auth_key = str(pc_web_cfg.get("auth_key", "pc_web")).strip() or "pc_web"
        pc_auth = extra_auth.get(pc_auth_key) or extra_auth.get("pc_web")
        pc_service = create_pc_web_service(config, args, extra_metrics_cfg)
        pc_preflight = asyncio.run(pc_service.preflight(report_date_for_check, pc_auth))
        pc_ok = bool((pc_preflight or {}).get("ok"))
        pc_message = str((pc_preflight or {}).get("message") or "")
        logging.info("pc_web: %s", pc_message)
        if not pc_ok and strict_mode:
            raise ReportError(f"PC后台登录态预检失败: {pc_message}")
        if bool(pc_web_cfg.get("include_member_metrics", True)):
            member_preflight = asyncio.run(pc_service.preflight_member(report_date_for_check, extra_auth.get("fenxi")))
            member_ok = bool((member_preflight or {}).get("ok"))
            member_msg = str((member_preflight or {}).get("message") or "")
            logging.info("pc_member: %s", member_msg)
            if not bool(fenxi_diag.get("usable")):
                member_ok = False
                member_msg = f"{member_msg}; {fenxi_diag.get('message', '')}".strip("; ")
            if (not member_ok) and strict_mode:
                raise ReportError(f"PC会员登录态预检失败: {member_msg}")


def _env_bool(name: str) -> Optional[bool]:
    value = os.getenv(name)
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _env_int(name: str) -> Optional[int]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _repo_relative_path(raw: Any, default: Path) -> Path:
    path = Path(str(raw or default)).expanduser()
    if path.is_absolute():
        return path
    return (bundle_base_path() / path).resolve()


def auth_repair_enabled(config: Dict[str, Any], args: argparse.Namespace) -> bool:
    env_value = _env_bool("AUTH_REPAIR_ENABLED")
    if env_value is None:
        env_value = _env_bool("RUN_AUTH_REPAIR")
    cfg = config.get("auth_repair") or {}
    return bool(args.repair_auth_on_failure or env_value or cfg.get("enabled", False))


def resolve_auth_repair_settings(
    config: Dict[str, Any],
    args: argparse.Namespace,
    extra_metrics_cfg: Dict[str, Any],
    extra_auth_file: Path,
) -> AuthRepairSettings:
    auth_cfg = config.get("auth_repair") or {}
    if not isinstance(auth_cfg, dict):
        auth_cfg = {}
    pc_web_cfg = config.get("pc_web_metrics") or {}
    if not isinstance(pc_web_cfg, dict):
        pc_web_cfg = {}

    raw_targets = auth_cfg.get("targets")
    cfg_target = str(auth_cfg.get("target") or "").strip()
    if isinstance(raw_targets, list):
        normalized_targets = {str(item).strip().lower() for item in raw_targets if str(item).strip()}
        if {"fenxi", "pc_web"}.issubset(normalized_targets):
            cfg_target = "both"
        elif "fenxi" in normalized_targets:
            cfg_target = "fenxi"
        elif "pc_web" in normalized_targets:
            cfg_target = "pc_web"
    target = str(args.auth_repair_target or os.getenv("AUTH_REPAIR_TARGET") or cfg_target or "auto").strip().lower()

    profile_default = DEFAULT_OUTPUT_DIR / "auth_profiles" / "chrome_daily_report"
    profile = _repo_relative_path(
        args.auth_repair_profile
        or os.getenv("AUTH_REPAIR_PROFILE")
        or auth_cfg.get("profile_dir")
        or profile_default,
        profile_default,
    )
    chain_candidates = auth_cfg.get("pc_chain_candidates") or auth_cfg.get("pc_chains") or [545]
    if not isinstance(chain_candidates, list):
        chain_candidates = [chain_candidates]
    pc_probe_urls = auth_cfg.get("pc_probe_urls") or auth_cfg.get("pc_probe_url") or [
        "http://yadmin.4399.com/#/statistics/game-start"
    ]
    if not isinstance(pc_probe_urls, list):
        pc_probe_urls = [pc_probe_urls]
    fenxi_probe_urls = auth_cfg.get("fenxi_probe_urls") or auth_cfg.get("fenxi_probe_url") or [
        "https://fenxi.4399dev.com/analysis/"
    ]
    if not isinstance(fenxi_probe_urls, list):
        fenxi_probe_urls = [fenxi_probe_urls]

    return AuthRepairSettings(
        extra_auth_file=Path(extra_auth_file),
        output=Path(extra_auth_file),
        profile_dir=profile,
        browser=str(args.auth_repair_browser or os.getenv("AUTH_REPAIR_BROWSER") or auth_cfg.get("browser") or "chrome").strip(),
        chrome_executable=str(
            os.getenv("AUTH_REPAIR_CHROME")
            or auth_cfg.get("chrome_executable")
            or r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
        ).strip(),
        pc_login_url=str(auth_cfg.get("pc_login_url") or pc_web_cfg.get("web_origin") or "http://yadmin.4399.com/").strip(),
        pc_probe_urls=[str(value).strip() for value in pc_probe_urls if str(value).strip()],
        fenxi_url=str(auth_cfg.get("fenxi_url") or "https://fenxi.4399dev.com/analysis/").strip(),
        fenxi_probe_urls=[str(value).strip() for value in fenxi_probe_urls if str(value).strip()],
        pc_base_url=str(pc_web_cfg.get("base") or auth_cfg.get("pc_base_url") or "http://yapiadmin.4399.com").strip(),
        pc_web_origin=str(pc_web_cfg.get("web_origin") or auth_cfg.get("pc_web_origin") or "http://yadmin.4399.com").strip(),
        pc_request_timeout=int(pc_web_cfg.get("request_timeout", extra_metrics_cfg.get("request_timeout", 20))),
        hosts_yaml_path=str(
            args.hosts_yaml_path
            if args.hosts_yaml_path is not None
            else pc_web_cfg.get("hosts_yaml_path") or extra_metrics_cfg.get("hosts_yaml_path", "")
        ).strip(),
        query_proxy_url=str(
            args.query_proxy_url
            if args.query_proxy_url is not None
            else pc_web_cfg.get("query_proxy_url") or extra_metrics_cfg.get("query_proxy_url", "")
        ).strip(),
        timeout_seconds=int(
            args.auth_repair_timeout_seconds
            or _env_int("AUTH_REPAIR_TIMEOUT_SECONDS")
            or auth_cfg.get("timeout_seconds")
            or 300
        ),
        target=target,
        auto_close=bool(auth_cfg.get("auto_close", True)),
        pc_chain_candidates=[int(value) for value in chain_candidates if str(value).strip().lstrip("-").isdigit()],
    )


def write_run_state(output_dir: Path, report_date: date, updates: Dict[str, Any]) -> None:
    state_dir = Path(output_dir) / "run_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / f"{report_date.strftime('%Y%m%d')}.json"
    state: Dict[str, Any] = {}
    if state_path.exists():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except json.JSONDecodeError:
            state = {}
    state.update(updates)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_auth_repair(
    config: Dict[str, Any],
    args: argparse.Namespace,
    extra_metrics_cfg: Dict[str, Any],
    extra_auth_file: Path,
    *,
    reason_text: str = "",
) -> Dict[str, Any]:
    settings = resolve_auth_repair_settings(config, args, extra_metrics_cfg, extra_auth_file)
    coordinator = AgentRepairCoordinator(settings=settings, log_dir=Path(args.output) / "auth_repair_logs")
    try:
        result = coordinator.run(reason_text=reason_text)
    except Exception as exc:  # noqa: BLE001
        raise ReportError(f"登录态自动修复失败: {exc}") from exc
    logging.info("认证自动修复完成: targets=%s log=%s", ",".join(result.get("updated_targets") or []), result.get("log_path", ""))
    return result


def run_full_auth_preflight_with_repair(
    config: Dict[str, Any],
    args: argparse.Namespace,
    extra_metrics_cfg: Dict[str, Any],
    extra_auth_file: Path,
    *,
    phase: str,
) -> None:
    report_date_for_check = resolve_report_date(args.date)
    try:
        run_full_auth_preflight(config, args, extra_metrics_cfg, extra_auth_file)
        write_run_state(Path(args.output), report_date_for_check, {"preflight_result": "ok", "last_phase": phase})
        return
    except ReportError as exc:
        reason_text = str(exc)
        write_run_state(
            Path(args.output),
            report_date_for_check,
            {"last_phase": phase, "failure_stage": "auth_preflight", "failure_reason": reason_text},
        )
        if not auth_repair_enabled(config, args):
            raise
        repair_targets = sorted(classify_auth_failure(reason_text))
        if not repair_targets:
            logging.info("认证失败不属于 fenxi/PC Web 登录态，跳过自动修复: %s", reason_text)
            raise
        emit_progress(9, "登录态失效，打开 Chrome 修复窗口")
        original_repair_target = args.auth_repair_target
        if str(args.auth_repair_target or "auto").strip().lower() == "auto":
            args.auth_repair_target = "both"
        try:
            result = run_auth_repair(config, args, extra_metrics_cfg, extra_auth_file, reason_text=reason_text)
        except ReportError as repair_exc:
            write_run_state(
                Path(args.output),
                report_date_for_check,
                {
                    "last_phase": "auth_repair",
                    "repair_targets": repair_targets,
                    "repair_attempted": True,
                    "repair_result": "failed",
                    "repair_failure_reason": str(repair_exc),
                },
            )
            raise
        finally:
            args.auth_repair_target = original_repair_target
        write_run_state(
            Path(args.output),
            report_date_for_check,
            {
                "last_phase": "auth_repair",
                "repair_targets": repair_targets,
                "repair_attempted": True,
                "repair_result": "ok",
                "auth_repair_log": result.get("log_path", ""),
            },
        )
        run_full_auth_preflight(config, args, extra_metrics_cfg, extra_auth_file)
        write_run_state(
            Path(args.output),
            report_date_for_check,
            {"preflight_result": "ok_after_repair", "last_phase": phase},
        )


def is_preflight_ok(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def should_push_feishu_doc(args: argparse.Namespace, config: Dict[str, Any]) -> bool:
    feishu_cfg = config.get("feishu_doc") or {}
    if not isinstance(feishu_cfg, dict):
        raise ReportError("Config field feishu_doc must be a mapping when provided.")
    if getattr(args, "no_publish", False) or args.no_push_feishu_doc:
        return False
    if args.push_feishu_doc:
        return True
    if "enabled" in feishu_cfg:
        return bool(feishu_cfg.get("enabled"))
    # Default-on: publish to Feishu unless explicitly disabled.
    return True


def resolve_feishu_doc_settings(args: argparse.Namespace, config: Dict[str, Any]) -> FeishuDocSettings:
    feishu_cfg = config.get("feishu_doc") or {}
    if not isinstance(feishu_cfg, dict):
        raise ReportError("Config field feishu_doc must be a mapping when provided.")

    app_id = str(
        args.feishu_app_id
        or os.getenv("FEISHU_APP_ID")
        or feishu_cfg.get("app_id")
        or ""
    ).strip()
    app_secret = str(
        args.feishu_app_secret
        or os.getenv("FEISHU_APP_SECRET")
        or feishu_cfg.get("app_secret")
        or ""
    ).strip()
    if not app_id or not app_secret:
        raise ReportError(
            "飞书推送已启用，但缺少 app_id/app_secret。请通过 --feishu-app-id/--feishu-app-secret 或环境变量 FEISHU_APP_ID/FEISHU_APP_SECRET 提供。"
        )

    folder_token = str(
        args.feishu_folder_token
        or os.getenv("FEISHU_DOC_FOLDER_TOKEN")
        or feishu_cfg.get("folder_token")
        or ""
    ).strip()
    doc_url_prefix = str(
        args.feishu_doc_url_prefix
        or os.getenv("FEISHU_DOC_URL_PREFIX")
        or feishu_cfg.get("doc_url_prefix")
        or "https://www.feishu.cn/docx/"
    ).strip()
    timeout = int(feishu_cfg.get("timeout", 60))
    request_retries = int(feishu_cfg.get("request_retries", 3))
    retry_backoff_seconds = float(feishu_cfg.get("retry_backoff_seconds", 2.0))
    verify_content = bool(args.verify_feishu_content or feishu_cfg.get("verify_content", False))
    verify_lang = str(feishu_cfg.get("verify_content_lang", "zh")).strip() or "zh"
    image_width = int(feishu_cfg.get("image_width", 960))
    narrow_image_width = int(feishu_cfg.get("narrow_image_width", 760))
    tall_ratio_threshold = float(feishu_cfg.get("tall_ratio_threshold", 1.9))
    prevent_upscale_raw = feishu_cfg.get("prevent_upscale", True)
    prevent_upscale = is_preflight_ok(prevent_upscale_raw)

    return FeishuDocSettings(
        app_id=app_id,
        app_secret=app_secret,
        folder_token=folder_token,
        doc_url_prefix=doc_url_prefix,
        timeout=timeout,
        request_retries=request_retries,
        retry_backoff_seconds=retry_backoff_seconds,
        image_width=image_width,
        narrow_image_width=narrow_image_width,
        tall_ratio_threshold=tall_ratio_threshold,
        prevent_upscale=prevent_upscale,
        verify_content_after_publish=verify_content,
        verify_content_lang=verify_lang,
    )


def resolve_feishu_doc_title(args: argparse.Namespace, config: Dict[str, Any]) -> Tuple[str, str]:
    feishu_cfg = config.get("feishu_doc") or {}
    if not isinstance(feishu_cfg, dict):
        raise ReportError("Config field feishu_doc must be a mapping when provided.")
    title_override = str(args.feishu_doc_title or feishu_cfg.get("title") or "").strip()
    title_prefix = str(feishu_cfg.get("title_prefix") or "云游戏日报").strip() or "云游戏日报"
    return title_override, title_prefix


def should_push_feishu_pc_doc(config: Dict[str, Any]) -> bool:
    feishu_cfg = config.get("feishu_doc") or {}
    if not isinstance(feishu_cfg, dict):
        raise ReportError("Config field feishu_doc must be a mapping when provided.")
    if "pc_enabled" in feishu_cfg:
        return bool(feishu_cfg.get("pc_enabled"))
    return True


def resolve_feishu_pc_doc_title(config: Dict[str, Any]) -> Tuple[str, str]:
    feishu_cfg = config.get("feishu_doc") or {}
    if not isinstance(feishu_cfg, dict):
        raise ReportError("Config field feishu_doc must be a mapping when provided.")
    title_override = str(feishu_cfg.get("pc_title") or "").strip()
    title_prefix = str(feishu_cfg.get("pc_title_prefix") or "PC云游戏日报").strip() or "PC云游戏日报"
    return title_override, title_prefix


def build_existing_report_chart_paths(report_base_dir: Path) -> Dict[str, str]:
    charts_dir = (report_base_dir / "charts").resolve()
    return {
        "total": str(charts_dir / "total.png"),
        "page": str(charts_dir / "page.png"),
        "console": str(charts_dir / "console.png"),
        "mobile": str(charts_dir / "mobile.png"),
        "genshin": str(charts_dir / "genshin.png"),
        "starrail": str(charts_dir / "starrail.png"),
        "zzz": str(charts_dir / "zzz.png"),
        "high_quality": str(charts_dir / "high_quality.png"),
        "pc_cloud": str(charts_dir / "pc_cloud.png"),
    }


def build_existing_payment_images(report_base_dir: Path, report_date: date) -> Dict[str, str]:
    charts_dir = (report_base_dir / "charts").resolve()
    date_key = report_date.strftime("%Y%m%d")
    return {
        "page": str(charts_dir / f"505_page_payment_table_{date_key}.png"),
        "mobile": str(charts_dir / f"505_mobile_payment_table_{date_key}.png"),
    }


def build_publish_hash(report_file: Path, image_paths: Iterable[str] = ()) -> str:
    parts: List[Union[str, bytes, Path]] = [report_file]
    for raw_path in sorted(str(value) for value in image_paths if str(value).strip()):
        path = Path(raw_path)
        if not path.is_absolute():
            path = report_file.parent / path
        parts.append(path)
    return content_hash(parts)


def should_push_wecom_bot(config: Dict[str, Any], args: Optional[argparse.Namespace] = None) -> bool:
    wecom_cfg = config.get("wecom_bot") or {}
    if not isinstance(wecom_cfg, dict):
        raise ReportError("Config field wecom_bot must be a mapping when provided.")
    if args is not None and getattr(args, "no_publish", False):
        return False
    return bool(wecom_cfg.get("enabled", False))


def resolve_wecom_bot_settings(config: Dict[str, Any]) -> WeComBotSettings:
    wecom_cfg = config.get("wecom_bot") or {}
    if not isinstance(wecom_cfg, dict):
        raise ReportError("Config field wecom_bot must be a mapping when provided.")

    bot_id = str(os.getenv("WECOM_BOT_ID") or wecom_cfg.get("bot_id") or "").strip()
    secret = str(os.getenv("WECOM_BOT_SECRET") or wecom_cfg.get("secret") or "").strip()
    if not bot_id or not secret:
        raise ReportError("企业微信长连接推送已启用，但缺少 bot_id/secret。")

    return WeComBotSettings(
        bot_id=bot_id,
        secret=secret,
        ws_url=str(wecom_cfg.get("ws_url") or "wss://openws.work.weixin.qq.com").strip() or "wss://openws.work.weixin.qq.com",
        open_timeout=float(wecom_cfg.get("open_timeout", 20.0)),
        ack_timeout=float(wecom_cfg.get("ack_timeout", 20.0)),
        max_message_length=int(wecom_cfg.get("max_message_length", 3200)),
    )


def resolve_wecom_chatid(config: Dict[str, Any], target: str) -> str:
    wecom_cfg = config.get("wecom_bot") or {}
    if not isinstance(wecom_cfg, dict):
        raise ReportError("Config field wecom_bot must be a mapping when provided.")
    normalized = str(target or "").strip().lower()
    if normalized == "single":
        value = wecom_cfg.get("single_userid") or wecom_cfg.get("single_chatid") or ""
        return str(value).strip()
    if normalized == "group":
        return str(wecom_cfg.get("group_chatid") or "").strip()
    raise ReportError(f"未知企业微信推送目标: {target}")


def resolve_wecom_auto_targets(config: Dict[str, Any]) -> List[str]:
    wecom_cfg = config.get("wecom_bot") or {}
    if not isinstance(wecom_cfg, dict):
        return []
    raw = wecom_cfg.get("auto_targets")
    if isinstance(raw, list):
        out = [str(item).strip().lower() for item in raw if str(item).strip()]
        return [item for item in out if item in {"single", "group"}]
    if isinstance(raw, str) and raw.strip():
        item = raw.strip().lower()
        return [item] if item in {"single", "group"} else []
    return ["single"]


def build_report_file_key(report_date: date) -> str:
    return f"{report_date.year}{report_date.month}{report_date.day}"


def extract_report_title_and_body(report_text: str, fallback_title: str) -> Tuple[str, str]:
    lines = report_text.replace("\r", "").split("\n")
    title = fallback_title
    body_lines = lines
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        title = stripped
        body_lines = lines[idx + 1 :]
        break
    body = "\n".join(body_lines).strip()
    return title, body


def build_wecom_link_payload(report_date: date, main_url: str = "", pc_url: str = "") -> List[Dict[str, str]]:
    lines: List[str] = []
    if main_url:
        lines.append(f"主日报飞书：{main_url}")
    if pc_url:
        lines.append(f"PC日报飞书：{pc_url}")
    if not lines:
        return []
    return [
        {
            "title": f"{report_date.year}年{report_date.month}月{report_date.day}日日报飞书链接",
            "content": "\n".join(lines),
        }
    ]


def publish_report_file_to_feishu(
    *,
    config: Dict[str, Any],
    args: argparse.Namespace,
    report_file: Path,
    report_date: date,
) -> Dict[str, Any]:
    report_text = report_file.read_text(encoding="utf-8")
    feishu_settings = resolve_feishu_doc_settings(args, config)
    if report_file.name.endswith("_pc_report.txt"):
        chart_image_paths = build_existing_report_chart_paths(report_file.parent)
        pc_title_override, pc_title_prefix = resolve_feishu_pc_doc_title(config)
        return publish_report_to_feishu_doc(
            settings=feishu_settings,
            report_text=report_text,
            report_date=report_date,
            title_override=pc_title_override,
            title_prefix=pc_title_prefix,
            report_base_dir=report_file.parent,
            chart_image_paths=chart_image_paths,
        )
    chart_image_paths = build_existing_report_chart_paths(report_file.parent)
    payment_images = build_existing_payment_images(report_file.parent, report_date)
    title_override, title_prefix = resolve_feishu_doc_title(args, config)
    return publish_report_to_feishu_doc(
        settings=feishu_settings,
        report_text=report_text,
        report_date=report_date,
        title_override=title_override,
        title_prefix=title_prefix,
        report_base_dir=report_file.parent,
        chart_image_paths=chart_image_paths,
        payment_images=payment_images,
    )


def push_reports_to_wecom_target(
    *,
    config: Dict[str, Any],
    target: str,
    report_date: date,
    main_url: str = "",
    pc_url: str = "",
) -> Dict[str, Any]:
    settings = resolve_wecom_bot_settings(config)
    chatid = resolve_wecom_chatid(config, target)
    if not chatid:
        raise ReportError(f"企业微信 {target} 目标未配置 chatid/userid。")
    payloads = build_wecom_link_payload(report_date, main_url=main_url, pc_url=pc_url)
    if not payloads:
        raise ReportError("没有可推送到企业微信的飞书链接。")
    return publish_reports_to_wecom(settings=settings, chatid=chatid, reports=payloads)

def configure_matplotlib_fonts() -> None:
    global _FONT_CONFIGURED  # pylint: disable=global-statement
    if _FONT_CONFIGURED:
        return
    try:
        import matplotlib
        from matplotlib import font_manager
    except ImportError:
        logging.warning("matplotlib not available; using default fonts.")
        _FONT_CONFIGURED = True
        return

    matplotlib.rcParams["axes.unicode_minus"] = False
    preferred_fonts = [
        "Microsoft YaHei",
        "Microsoft YaHei UI",
        "Microsoft JhengHei",
        "SimHei",
        "SimSun",
        "PingFang SC",
        "Hiragino Sans GB",
        "Noto Sans CJK SC",
    ]
    chosen_font = None
    for font_name in preferred_fonts:
        try:
            font_path = font_manager.findfont(font_name, fallback_to_default=False)
            if font_path:
                chosen_font = font_name
                logging.debug("Using font %s for charts (%s)", font_name, font_path)
                break
        except (ValueError, RuntimeError):
            continue

    if chosen_font:
        matplotlib.rcParams["font.sans-serif"] = [chosen_font]
        matplotlib.rcParams["font.family"] = "sans-serif"
    else:
        logging.warning(
            "No preferred Chinese fonts found; charts may display fallback glyphs."
        )
    _FONT_CONFIGURED = True

def format_time_label(point: Optional[TimePoint]) -> str:
    if point is None:
        return "未知"
    if point.timestamp:
        hour = point.timestamp.hour
        minute = point.timestamp.minute
        if minute:
            return f"{hour}点{minute}分"
        return f"{hour}点"
    label = point.raw_label.strip()
    match = re.match(r"^(\d{1,2})(?::(\d{2}))?$", label)
    if match:
        hour = int(match.group(1))
        minute = match.group(2)
        if minute and int(minute):
            return f"{hour}点{int(minute)}分"
        return f"{hour}点"
    return label or "未知"



def format_hour_cn(hour: Optional[int]) -> str:
    if hour is None:
        return "未知"
    return f"{hour}点"


def parse_timestamp(raw: Any, base_date: date) -> Tuple[Optional[datetime], str, Optional[int]]:
    if raw is None:
        return None, "", None
    if isinstance(raw, datetime):
        return raw, raw.strftime("%H:%M"), raw.hour
    if isinstance(raw, date):
        dt = datetime.combine(raw, time())
        return dt, dt.strftime("%H:%M"), dt.hour
    if isinstance(raw, (int, float)):
        # Treat large integers as epoch seconds.
        try:
            dt = datetime.fromtimestamp(float(raw))
            return dt, dt.strftime("%H:%M"), dt.hour
        except (OverflowError, OSError, ValueError):
            # Fallback: interpret as hour index.
            hour = int(raw)
            dt = datetime.combine(base_date, time(hour=hour))
            return dt, dt.strftime("%H:%M"), hour
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None, "", None
        hour_match = re.match(r"^(\d{1,2})(?::(\d{2}))?$", stripped)
        if hour_match:
            hour = int(hour_match.group(1))
            minute = int(hour_match.group(2)) if hour_match.group(2) else 0
            dt = datetime.combine(base_date, time(hour=hour, minute=minute))
            return dt, dt.strftime("%H:%M"), hour
        try:
            dt = date_parser.parse(stripped)
            hour = dt.hour if isinstance(dt, datetime) else None
            label = dt.strftime("%H:%M") if hour is not None else stripped
            if isinstance(dt, datetime):
                return dt, label, hour
            dt_combined = datetime.combine(dt, time())
            return dt_combined, label, dt_combined.hour
        except (ValueError, TypeError):
            return None, stripped, None
    # Fallback to raw representation.
    return None, str(raw), None


def normalize_series_entry(
    entry: Dict[str, Any],
    base_date: date,
    x_axis: Optional[Sequence[Any]] = None,
) -> Tuple[str, List[TimePoint]]:
    name = entry.get("name") or entry.get("label") or entry.get("title") or ""
    raw_data = (
        entry.get("data")
        if isinstance(entry, dict)
        else None
    )
    if raw_data is None and isinstance(entry, dict):
        for candidate_key in ("values", "list", "points", "items"):
            if candidate_key in entry:
                raw_data = entry[candidate_key]
                break
    if raw_data is None:
        raise ReportError(f"Series entry {name!r} missing data field.")

    data_points: List[Tuple[Any, Any]] = []

    if isinstance(raw_data, dict):
        # Possible structure: {"time": [...], "value": [...]}
        if "time" in raw_data and "value" in raw_data:
            data_points = list(zip(raw_data.get("time", []), raw_data.get("value", [])))
        elif "x" in raw_data and "y" in raw_data:
            data_points = list(zip(raw_data.get("x", []), raw_data.get("y", [])))
        elif "timestamps" in raw_data and "data" in raw_data:
            data_points = list(zip(raw_data.get("timestamps", []), raw_data.get("data", [])))
        else:
            data_points = list(raw_data.items())
    elif isinstance(raw_data, list):
        if raw_data and all(isinstance(item, (int, float)) for item in raw_data):
            if not x_axis or len(x_axis) != len(raw_data):
                raise ReportError(f"Numeric series for {name!r} requires matching x_axis.")
            data_points = list(zip(x_axis, raw_data))
        elif raw_data and all(isinstance(item, dict) for item in raw_data):
            for item in raw_data:
                time_value = item.get("time") or item.get("timestamp") or item.get("name") or item.get("label")
                value = item.get("value")
                if value is None:
                    # Try alternative numeric keys.
                    for value_key in ("count", "num", "val", "y"):
                        if value_key in item:
                            value = item[value_key]
                            break
                data_points.append((time_value, value))
        elif raw_data and all(isinstance(item, (list, tuple)) and len(item) >= 2 for item in raw_data):
            data_points = [(entry[0], entry[1]) for entry in raw_data]
        else:
            for idx, value in enumerate(raw_data):
                if not x_axis or idx >= len(x_axis):
                    raise ReportError(f"Unable to align data for series {name!r}.")
                data_points.append((x_axis[idx], value))
    else:
        raise ReportError(f"Unsupported data type for series {name!r}: {type(raw_data).__name__}")

    normalized: List[TimePoint] = []
    for raw_time, raw_value in data_points:
        if raw_value is None:
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        timestamp, label, hour = parse_timestamp(raw_time, base_date)
        normalized.append(TimePoint(timestamp=timestamp, raw_label=label, hour=hour, value=value))

    normalized.sort(key=lambda point: point.sort_key())
    return name, normalized


def float_or_none(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def extract_series_from_table(
    rows: Sequence[Dict[str, Any]],
    base_date: date,
    time_field: str,
) -> Dict[str, List[TimePoint]]:
    series_map: Dict[str, List[TimePoint]] = defaultdict(list)
    for row in rows:
        if not isinstance(row, dict):
            continue
        time_value = row.get(time_field) or row.get("ctime") or row.get("time")
        timestamp, label, hour = parse_timestamp(time_value, base_date)
        if timestamp is None and not label:
            continue
        for key, raw_value in row.items():
            if key == time_field or key in {"time", "ctime"}:
                continue
            value = float_or_none(raw_value)
            if value is None:
                continue
            point = TimePoint(timestamp=timestamp, raw_label=label, hour=hour, value=value)
            series_map[key].append(point)
    for series in series_map.values():
        series.sort(key=lambda point: point.sort_key())
    return series_map


def extract_series(payload: Any, base_date: date, time_field: str) -> Dict[str, List[TimePoint]]:
    def locate_series_containers(node: Any) -> List[Dict[str, Any]]:
        containers: List[Dict[str, Any]] = []
        if isinstance(node, dict):
            if "series" in node and isinstance(node["series"], list):
                containers.append(node)
            for value in node.values():
                containers.extend(locate_series_containers(value))
        elif isinstance(node, list):
            for item in node:
                containers.extend(locate_series_containers(item))
        return containers

    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            return extract_series_from_table(payload["data"], base_date, time_field)

    containers = []
    if isinstance(payload, dict):
        containers.extend(locate_series_containers(payload))
    elif isinstance(payload, list):
        for item in payload:
            containers.extend(locate_series_containers(item))

    if not containers:
        raise ReportError("Unable to locate series data in response payload.")

    # Use the first matching container by default.
    container = containers[0]
    x_axis = resolve_x_axis(container)
    series_map: Dict[str, List[TimePoint]] = {}
    for entry in container.get("series", []):
        name, series_points = normalize_series_entry(entry, base_date, x_axis)
        series_map[name] = series_points
    return series_map


def resolve_x_axis(container: Dict[str, Any]) -> Optional[Sequence[Any]]:
    x_axis = container.get("xAxis") or container.get("categories")
    if x_axis is None:
        return None
    if isinstance(x_axis, dict):
        for key in ("data", "values", "list"):
            if key in x_axis:
                return x_axis[key]
    if isinstance(x_axis, list):
        return x_axis
    return None


def match_series_keys(
    series_map: Dict[str, List[TimePoint]],
    patterns: Sequence[str],
) -> List[List[TimePoint]]:
    matched: List[List[TimePoint]] = []
    seen: set[str] = set()
    for pattern in patterns:
        compiled = re.compile(pattern)
        for name, series in series_map.items():
            if name in seen:
                continue
            if compiled.search(name):
                matched.append(series)
                seen.add(name)
    return matched


def combine_series(series_list: Sequence[List[TimePoint]]) -> List[TimePoint]:
    combined: Dict[Tuple[Optional[datetime], str], TimePoint] = {}
    for series in series_list:
        for point in series:
            key = (point.timestamp, point.raw_label)
            if key not in combined:
                combined[key] = TimePoint(
                    timestamp=point.timestamp,
                    raw_label=point.raw_label,
                    hour=point.hour,
                    value=0.0,
                )
            combined[key].value += point.value
    sorted_points = sorted(combined.values(), key=lambda point: point.sort_key())
    return sorted_points


def build_metric_summary(points: List[TimePoint]) -> MetricSummary:
    summary = MetricSummary(series=points)
    if not points:
        summary.peak_point = None
        summary.formatted_peak_value = "0"
        summary.peak_time_label = "无"
        return summary
    peak_point = max(points, key=lambda point: point.value)
    summary.peak_point = peak_point
    summary.formatted_peak_value = format_value(peak_point.value)
    summary.peak_time_label = format_time_label(peak_point)
    return summary


def collect_series_for_queries(
    queries: Sequence[Dict[str, Any]],
    auto_params: Dict[str, str],
    concurrency_patterns: Sequence[str],
    queue_patterns: Sequence[str],
    session: requests.Session,
    base_url: str,
    base_date: date,
    timeout: float,
    default_http_method: str,
    time_field: str,
    hosts_map: Optional[Dict[str, str]] = None,
) -> Tuple[List[List[TimePoint]], List[List[TimePoint]]]:
    all_concurrency_series: List[List[TimePoint]] = []
    all_queue_series: List[List[TimePoint]] = []

    for query in queries:
        if "params" not in query:
            raise ReportError("Query entry missing params.")
        params = dict(auto_params)
        params.update(query["params"])
        method = query.get("method") or default_http_method
        payload = fetch_json(session, base_url, params, timeout, method, hosts_map=hosts_map)
        series_map = extract_series(payload, base_date, time_field)
        all_concurrency_series.extend(match_series_keys(series_map, concurrency_patterns))
        all_queue_series.extend(match_series_keys(series_map, queue_patterns))

    return all_concurrency_series, all_queue_series


def extract_positive_hours(points: Iterable[TimePoint]) -> List[int]:
    hours = set()
    for point in points:
        if point.value <= 0:
            continue
        hour = point.hour
        if hour is None:
            match = re.match(r"^(\d{1,2})", point.raw_label or "")
            if match:
                hour = int(match.group(1))
        if hour is not None:
            hours.add(hour)
    return sorted(hours)


def describe_queue_hours(hours: Sequence[int], label: str) -> str:
    if not hours:
        return f"{label}无排队。"
    ranges: List[Tuple[int, int]] = []
    start = prev = hours[0]
    for hour in hours[1:]:
        if hour == prev or hour == prev + 1:
            prev = hour
            continue
        ranges.append((start, prev))
        start = prev = hour
    ranges.append((start, prev))
    segments: List[str] = []
    for start_hour, end_hour in ranges:
        if start_hour == end_hour:
            segments.append(f"{start_hour}点")
        else:
            segments.append(f"{start_hour}点-{end_hour}点")
    return f"于{'、'.join(segments)}有排队。"


def compute_total_queue(points: Iterable[TimePoint]) -> float:
    return sum(point.value for point in points)


def fetch_json(
    session: requests.Session,
    base_url: str,
    params: Dict[str, Any],
    timeout: float,
    method: str,
    hosts_map: Optional[Dict[str, str]] = None,
) -> Any:
    method_lower = method.lower()
    request_url = base_url
    request_headers: Optional[Dict[str, str]] = None
    if hosts_map:
        rewritten_url, host_header = rewrite_url_with_hosts_map(base_url, hosts_map)
        if host_header:
            request_url = rewritten_url
            request_headers = {"Host": host_header}
            logging.debug("870 hosts rewrite: %s -> %s (Host=%s)", base_url, request_url, host_header)
    logging.debug("Fetching %s method=%s params=%s", request_url, method_lower, params)
    if method_lower == "post":
        response = session.post(request_url, data=params, timeout=timeout, allow_redirects=False, headers=request_headers)
    else:
        response = session.get(request_url, params=params, timeout=timeout, allow_redirects=False, headers=request_headers)
    if response.is_redirect:
        raise ReportError("Received redirect response, session cookie likely invalid.")
    response.raise_for_status()
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        content_type = response.headers.get("Content-Type", "")
        snippet = response.text[:180].replace("\n", " ").strip()
        raise ReportError(
            f"Response is not valid JSON for params={params}; content_type={content_type}; "
            f"body_snippet={snippet!r}. Session cookie may be invalid."
        ) from exc


def generate_chart(
    target_result: TargetResult,
    output_path: Path,
) -> Optional[Path]:
    try:
        import matplotlib.pyplot as plt
        from matplotlib import gridspec
        configure_matplotlib_fonts()
    except ImportError:
        logging.warning("matplotlib not available; skipping charts.")
        return None

    series_concurrency = target_result.concurrency.series
    series_queue = target_result.queue.series
    if not series_concurrency and not series_queue:
        return None

    fig = plt.figure(figsize=(12, 9))
    gs = gridspec.GridSpec(nrows=3, ncols=1, height_ratios=[0.5, 1.5, 1.5], hspace=0.35)

    # Summary table
    header_colors = ["#FBD15B", "#FBD15B", "#B7D7F5", "#B7D7F5"]
    table_headers = ["今日并发最高峰值", "今日排队最高峰值", "昨日并发最高峰值", "昨日排队最高峰值"]
    today_concurrency = target_result.concurrency.formatted_peak_value
    today_queue = target_result.queue.formatted_peak_value
    yesterday_concurrency = display_optional(target_result.previous_concurrency_peak)
    yesterday_queue = display_optional(target_result.previous_queue_peak)

    table_values = [today_concurrency, today_queue, yesterday_concurrency, yesterday_queue]

    table_ax = fig.add_subplot(gs[0])
    table_ax.axis("off")
    tbl = table_ax.table(
        cellText=[table_headers, table_values],
        cellLoc="center",
        loc="center",
    )
    for col_idx in range(len(table_headers)):
        color = header_colors[col_idx]
        tbl[(0, col_idx)].set_facecolor(color)
        tbl[(1, col_idx)].set_facecolor(color)
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(11)
    tbl.scale(1, 1.5)
    table_ax.set_title(f"{target_result.label}排队和并发情况", fontsize=14, pad=12)

    def build_xy(points: List[TimePoint]) -> Tuple[List[str], List[float]]:
        labels = []
        values = []
        for idx, point in enumerate(points):
            if point.hour is not None:
                labels.append(f"{point.hour}点")
            elif point.raw_label:
                labels.append(point.raw_label)
            else:
                labels.append(f"{idx}点")
            values.append(point.value)
        if not labels:
            labels = [f"{hour}点" for hour in range(24)]
            values = [0.0] * 24
        return labels, values

    x_labels_conc, y_conc = build_xy(series_concurrency)
    x_labels_queue, y_queue = build_xy(series_queue)

    def render_line(ax, x_labels, y_values, title, color):
        positions = list(range(len(x_labels)))
        ax.plot(positions, y_values, marker="o", color=color, linewidth=2)
        ax.fill_between(positions, y_values, color=color, alpha=0.08)
        ax.set_title(title, fontsize=14, pad=8)
        ax.set_ylabel("人数", fontsize=12)
        ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.6)
        max_y = max(y_values) if y_values else 0
        padding = max(max_y * 0.15, 40 if max_y > 0 else 5)
        ax.set_ylim(bottom=0, top=max_y + padding)
        ax.margins(x=0.02)
        ax.set_xticks(positions)
        ax.set_xticklabels(x_labels, rotation=0, fontsize=9)
        for idx, value in enumerate(y_values):
            offset = 10 if max_y > 0 else 6
            ax.annotate(
                format_value(value),
                xy=(positions[idx], value),
                textcoords="offset points",
                xytext=(0, offset),
                ha="center",
                va="bottom",
                fontsize=10,
            )

    ax_conc = fig.add_subplot(gs[1])
    render_line(ax_conc, x_labels_conc, y_conc, f"{target_result.label}并发情况", "#2c7be5")

    ax_queue = fig.add_subplot(gs[2])
    render_line(ax_queue, x_labels_queue, y_queue, f"{target_result.label}排队情况", "#f6aa1c")
    ax_queue.set_ylabel("人数", fontsize=12)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path


def build_target_result(
    key: str,
    config: Dict[str, Any],
    session: requests.Session,
    base_url: str,
    base_date: date,
    previous_date: date,
    timeout: float,
    default_time_field: str,
    default_http_method: str,
    auto_query_params: Dict[str, str],
    previous_auto_query_params: Dict[str, str],
    hosts_map: Optional[Dict[str, str]] = None,
) -> TargetResult:
    label = config.get("label", key)
    concurrency_patterns = config.get("concurrency_series_patterns") or [r"已用容器数"]
    queue_patterns = config.get("queue_series_patterns") or [r"排队人数"]
    params_list = config.get("queries") or []
    if not params_list:
        raise ReportError(f"Target {key!r} missing queries definition.")
    time_field = config.get("time_field") or default_time_field
    http_method = config.get("http_method") or default_http_method

    all_concurrency_series, all_queue_series = collect_series_for_queries(
        queries=params_list,
        auto_params=auto_query_params,
        concurrency_patterns=concurrency_patterns,
        queue_patterns=queue_patterns,
        session=session,
        base_url=base_url,
        base_date=base_date,
        timeout=timeout,
        default_http_method=http_method,
        time_field=time_field,
        hosts_map=hosts_map,
    )

    prev_concurrency_series, prev_queue_series = collect_series_for_queries(
        queries=params_list,
        auto_params=previous_auto_query_params,
        concurrency_patterns=concurrency_patterns,
        queue_patterns=queue_patterns,
        session=session,
        base_url=base_url,
        base_date=previous_date,
        timeout=timeout,
        default_http_method=http_method,
        time_field=time_field,
        hosts_map=hosts_map,
    )

    if not all_concurrency_series:
        logging.warning("No concurrency series matched for target %s (%s).", key, label)
    if not all_queue_series:
        logging.warning("No queue series matched for target %s (%s).", key, label)

    combined_concurrency = combine_series(all_concurrency_series) if all_concurrency_series else []
    combined_queue = combine_series(all_queue_series) if all_queue_series else []
    prev_combined_concurrency = combine_series(prev_concurrency_series) if prev_concurrency_series else []
    prev_combined_queue = combine_series(prev_queue_series) if prev_queue_series else []

    target_result = TargetResult(key=key, label=label)
    target_result.concurrency = build_metric_summary(combined_concurrency)
    target_result.queue = build_metric_summary(combined_queue)
    previous_concurrency_summary = build_metric_summary(prev_combined_concurrency)
    previous_queue_summary = build_metric_summary(prev_combined_queue)
    target_result.previous_concurrency_peak = previous_concurrency_summary.peak_value
    target_result.previous_queue_peak = previous_queue_summary.peak_value

    positive_hours = extract_positive_hours(target_result.queue.series)
    target_result.queue_summary = describe_queue_hours(positive_hours, label)
    target_result.total_queue_value = compute_total_queue(target_result.queue.series)
    return target_result


def build_top_sentences(
    group_configs: Sequence[Dict[str, Any]],
    results: Dict[str, TargetResult],
) -> List[str]:
    sentences: List[str] = []
    for group in group_configs:
        members = group.get("members") or []
        if not members:
            fallback = group.get("fallback")
            if fallback:
                sentences.append(fallback)
            continue
        member_stats: List[Tuple[str, float]] = []
        for member_key in members:
            result = results.get(member_key)
            if not result:
                continue
            total_queue = result.total_queue_value
            member_stats.append((result.label, total_queue))
        member_stats = [item for item in member_stats if item[1] > 0]
        if not member_stats:
            fallback = group.get('fallback') or f"{group.get('label', '该类别')}排队无显著主导线路。"
            sentences.append(fallback)
            continue
        member_stats.sort(key=lambda item: item[1], reverse=True)
        top_n = max(1, int(group.get("top_n", 1)))
        top_members = member_stats[:top_n]
        formatted_items = "、".join(f"《{name}》" for name, _ in top_members)
        template = group.get("sentence_template") or "{group_label}排队以{items}为主。"
        sentence = template.format(group_label=group.get("label", ""), items=formatted_items)
        sentences.append(sentence)
    return sentences


def build_anomaly_sentences(
    anomaly_rules: Sequence[Dict[str, Any]],
    results: Dict[str, TargetResult],
) -> List[str]:
    sentences: List[str] = []
    for rule in anomaly_rules:
        source_key = rule.get("source")
        if not source_key:
            continue
        result = results.get(source_key)
        if not result:
            continue
        metric_name = rule.get("metric", "queue")
        metric_summary = getattr(result, metric_name, None)
        if not isinstance(metric_summary, MetricSummary):
            continue
        points = metric_summary.series
        if not points:
            continue
        min_value = float(rule.get("min_value", 1))
        hour_range = rule.get("hour_range")
        relevant_points: List[TimePoint] = []
        for point in points:
            if point.value < min_value:
                continue
            if hour_range:
                if point.hour is None:
                    continue
                start_hour, end_hour = hour_range
                if not (start_hour <= point.hour <= end_hour):
                    continue
            relevant_points.append(point)
        hours = extract_positive_hours(relevant_points)
        if not hours:
            continue
        ranges: List[Tuple[int, int]] = []
        start = prev = hours[0]
        for hour in hours[1:]:
            if hour == prev or hour == prev + 1:
                prev = hour
                continue
            ranges.append((start, prev))
            start = prev = hour
        ranges.append((start, prev))
        template = rule.get("message_template") or "{start_hour_cn}-{end_hour_cn}出现排队为{label}数据异常。"
        metric_label = "排队" if metric_name == "queue" else "并发"
        for start_hour, end_hour in ranges:
            context = {
                "label": result.label,
                "metric": metric_label,
                "start_hour": start_hour,
                "end_hour": end_hour,
                "start_hour_cn": format_hour_cn(start_hour),
                "end_hour_cn": format_hour_cn(end_hour),
            }
            sentences.append(template.format(**context))
    return sentences


def render_report(
    template_dir: Path,
    template_name: str,
    output_dir: Path,
    date_cn: str,
    results: Dict[str, TargetResult],
    ordered_sections: Sequence[str],
    analysis_sentences: Sequence[str],
    extra_metrics_block: Optional[str] = None,
) -> Path:
    template_dir = prepare_template_directory(template_dir, template_name, DEFAULT_TEMPLATE_CONTENT)
    # This template renders plain text/Markdown rather than executable HTML.
    env = Environment(  # nosec B701
        loader=FileSystemLoader(str(template_dir)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(template_name)
    output_text = template.render(
        report_date_cn=date_cn,
        targets=results,
        sections=ordered_sections,
        analysis_sentences=analysis_sentences,
    )
    if extra_metrics_block:
        output_text = f"{output_text.rstrip()}\n\n{extra_metrics_block.strip()}\n"
    sanitized_date = (
        date_cn.replace('年', '')
        .replace('月', '')
        .replace('日', '')
        .replace(' ', '')
    )
    date_for_filename = sanitized_date or date_cn
    output_path = output_dir / f"{date_for_filename}_report.txt"
    with output_path.open('w', encoding='utf-8') as fh:
        fh.write(output_text)
    return output_path


def _split_pipe_row(line: str) -> List[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def _render_pipe_table_html(table_lines: Sequence[str]) -> str:
    rows = [_split_pipe_row(line) for line in table_lines if line.strip()]
    if not rows:
        return ""
    has_align = len(rows) > 1 and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in rows[1])
    header = rows[0]
    body = rows[2:] if has_align else rows[1:]
    thead = "<thead><tr>%s</tr></thead>" % "".join(f"<th>{html.escape(cell)}</th>" for cell in header)
    tbody = "<tbody>%s</tbody>" % "".join(
        "<tr>%s</tr>" % "".join(f"<td>{html.escape(cell)}</td>" for cell in row)
        for row in body
    )
    return f"<table class=\"md-table\">{thead}{tbody}</table>"


def build_strict_template_markdown(
    report_text: str,
    chart_image_paths: Dict[str, str],
    payment_images: Optional[Dict[str, str]] = None,
) -> str:
    text = report_text
    refs: Dict[str, str] = {}
    mapping = [
        ("image1", "[总路线图片]", "total"),
        ("image2", "[页游图片]", "page"),
        ("image3", "[主机图片]", "console"),
        ("image4", "[手游图片]", "mobile"),
        ("image5", "[原神图片]", "genshin"),
        ("image6", "[星铁图片]", "starrail"),
        ("image7", "[绝区零图片]", "zzz"),
        ("image8", "[高画质图片]", "high_quality"),
        ("image9", "[pc云游戏图片]", "pc_cloud"),
    ]
    for image_key, marker, section_key in mapping:
        image_path = str(chart_image_paths.get(section_key, "")).strip()
        if not image_path:
            continue
        text = text.replace(marker, f"![][{image_key}]")
        refs[image_key] = image_path

    payment_images = payment_images or {}
    lines = text.splitlines()
    rebuilt: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("页游付费表图片："):
            page_path = stripped.split("：", 1)[1].strip() or str(payment_images.get("page", "")).strip()
            if page_path:
                refs["image10"] = page_path
                rebuilt.append("![][image10]")
            else:
                rebuilt.append(line)
            continue
        if stripped.startswith("手游付费表图片："):
            mobile_path = stripped.split("：", 1)[1].strip() or str(payment_images.get("mobile", "")).strip()
            if mobile_path:
                refs["image11"] = mobile_path
                rebuilt.append("![][image11]")
            else:
                rebuilt.append(line)
            continue
        rebuilt.append(line)

    if refs:
        rebuilt.append("")

        def _ref_sort_key(key: str) -> int:
            matched = re.search(r"(\d+)$", key)
            return int(matched.group(1)) if matched else 10_000

        for key in sorted(refs.keys(), key=_ref_sort_key):
            rebuilt.append(f"[{key}]: {refs[key]}")
    return "\n".join(rebuilt).rstrip() + "\n"


def strict_template_markdown_to_html(markdown_text: str) -> str:
    source_lines = markdown_text.replace("\r", "").split("\n")
    refs: Dict[str, str] = {}
    content_lines: List[str] = []
    for line in source_lines:
        matched = re.match(r"^\[([^\]]+)\]:\s*(.+)$", line.strip())
        if matched:
            refs[matched.group(1).strip()] = matched.group(2).strip()
        else:
            content_lines.append(line)

    output: List[str] = []
    idx = 0
    while idx < len(content_lines):
        line = content_lines[idx].rstrip()
        stripped = line.strip()
        if not stripped:
            output.append('<div class="blank"></div>')
            idx += 1
            continue

        image_match = re.match(r"^!\[\]\[([^\]]+)\]$", stripped)
        if image_match:
            key = image_match.group(1).strip()
            image_src = refs.get(key, "")
            if image_src:
                output.append(
                    "<div class=\"img-block\">"
                    f"<img src=\"{html.escape(image_src)}\" alt=\"{html.escape(key)}\" />"
                    f"<div class=\"img-cap\">[{html.escape(key)}]</div>"
                    "</div>"
                )
            else:
                output.append(f"<div class=\"img-miss\">[{html.escape(key)}]</div>")
            idx += 1
            continue

        if re.fullmatch(r"[—-]{8,}", stripped):
            output.append(f"<div class=\"sep\">{html.escape(stripped)}</div>")
            idx += 1
            continue

        if stripped.startswith("|"):
            table_lines = [line]
            idx += 1
            while idx < len(content_lines) and content_lines[idx].strip().startswith("|"):
                table_lines.append(content_lines[idx])
                idx += 1
            output.append(_render_pipe_table_html(table_lines))
            continue

        if re.match(r"^\d{4}年\d+月\d+日游戏盒云游戏数据", stripped):
            output.append(f"<h1>{html.escape(stripped)}</h1>")
            idx += 1
            continue

        if re.match(r"^[一二三四五六七八九十]+、", stripped):
            output.append(f"<h2>{html.escape(stripped)}</h2>")
            idx += 1
            continue

        output.append(f"<p>{html.escape(stripped)}</p>")
        idx += 1
    return "\n".join(output)


def render_strict_template_html(
    report_path: Path,
    chart_image_paths: Dict[str, str],
    payment_images: Optional[Dict[str, str]] = None,
) -> Path:
    report_text = report_path.read_text(encoding="utf-8")
    markdown_text = build_strict_template_markdown(
        report_text=report_text,
        chart_image_paths=chart_image_paths,
        payment_images=payment_images,
    )
    body_html = strict_template_markdown_to_html(markdown_text)
    output_path = report_path.with_suffix(".html")
    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>云游戏日报</title>
  <style>
    :root {{ --bg:#f5f6f8; --paper:#fff; --ink:#222; --line:#d8dce3; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--ink); font-family:"PingFang SC","Microsoft YaHei",sans-serif; }}
    .page {{ max-width:1000px; margin:18px auto; padding:0 12px; }}
    .paper {{ background:var(--paper); border:1px solid var(--line); border-radius:8px; padding:18px 20px; }}
    h1 {{ margin:0 0 8px; font-size:28px; }}
    h2 {{ margin:8px 0 10px; font-size:20px; }}
    p {{ margin:4px 0; line-height:1.75; font-size:15px; white-space:pre-wrap; }}
    .sep {{ margin:8px 0; font-size:16px; letter-spacing:.5px; }}
    .blank {{ height:10px; }}
    .img-block {{ margin:8px 0 10px; overflow-x:auto; }}
    .img-block img {{ max-width:none; width:auto; height:auto; display:block; border:1px solid #d0d6dd; border-radius:4px; background:#fafafa; min-height:40px; }}
    .img-cap {{ font-size:12px; color:#6a7380; margin-top:3px; }}
    .img-miss {{ margin:6px 0; padding:8px; border:1px dashed #c8ced8; color:#6a7380; font-size:13px; }}
    .md-table {{ width:100%; border-collapse:collapse; margin:8px 0; font-size:14px; }}
    .md-table th,.md-table td {{ border:1px solid #cfd5df; padding:6px 8px; text-align:center; }}
    .md-table th {{ background:#eef2f7; }}
  </style>
</head>
<body>
  <div class="page">
    <div class="paper">
{body_html}
    </div>
  </div>
</body>
</html>
"""
    output_path.write_text(html_doc, encoding="utf-8")
    return output_path


def render_pc_report(
    template_dir: Path,
    template_name: str,
    output_dir: Path,
    date_cn: str,
    target: TargetResult,
    pc_notes: Optional[Dict[str, Any]] = None,
    pc_member_summary: Optional[Dict[str, Any]] = None,
    pc_top_games: Optional[List[Dict[str, Any]]] = None,
    pc_warnings: Optional[List[str]] = None,
) -> Path:
    template_dir = prepare_template_directory(template_dir, template_name, DEFAULT_PC_TEMPLATE_CONTENT)
    # This template renders plain text/Markdown rather than executable HTML.
    env = Environment(  # nosec B701
        loader=FileSystemLoader(str(template_dir)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(template_name)
    output_text = template.render(
        report_date_cn=date_cn,
        target=target,
        pc_notes=pc_notes or {},
        pc_member_summary=pc_member_summary or {},
        pc_top_games=pc_top_games or [],
        pc_warnings=pc_warnings or [],
    )
    sanitized_date = (
        date_cn.replace('年', '')
        .replace('月', '')
        .replace('日', '')
        .replace(' ', '')
    )
    date_for_filename = sanitized_date or date_cn
    output_path = output_dir / f"{date_for_filename}_pc_report.txt"
    with output_path.open('w', encoding='utf-8') as fh:
        fh.write(output_text)
    return output_path


def format_int_text(value: Any) -> str:
    if isinstance(value, bool):
        return "0"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{int(round(value)):,}"
    if isinstance(value, str):
        stripped = value.strip().replace(",", "")
        if not stripped:
            return "暂未获取"
        try:
            return f"{int(round(float(stripped))):,}"
        except ValueError:
            return value.strip()
    return "暂未获取"


def build_pc_member_summary(notes: Dict[str, Any]) -> Dict[str, str]:
    summary = notes.get("member_summary") if isinstance(notes, dict) else {}
    if not isinstance(summary, dict):
        summary = {}
    return {
        "recharge_count_text": format_int_text(summary.get("recharge_count")),
        "first_count_text": format_int_text(summary.get("first_count")),
        "recharge_amount_text": format_int_text(summary.get("recharge_amount")),
        "week_trend_text": str(summary.get("week_trend_text") or "暂未获取"),
    }


def build_pc_notes(notes: Dict[str, Any]) -> Dict[str, str]:
    new_users = notes.get("new_users") if isinstance(notes, dict) else {}
    active_users = notes.get("active_users") if isinstance(notes, dict) else {}
    if not isinstance(new_users, dict):
        new_users = {}
    if not isinstance(active_users, dict):
        active_users = {}
    return {
        "new_users_text": format_int_text(new_users.get("value")),
        "active_users_text": format_int_text(active_users.get("value")),
    }


def build_pc_top_games(top_games: Any) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    if not isinstance(top_games, list):
        return rows
    for item in top_games:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "active_users_text": format_int_text(item.get("active_users")),
            }
        )
    return rows


def create_pc_web_service(
    config: Dict[str, Any],
    args: argparse.Namespace,
    extra_metrics_cfg: Dict[str, Any],
) -> PCWebMetricsService:
    pc_web_cfg = config.get("pc_web_metrics") or {}
    pc_hosts_yaml_path = (
        args.hosts_yaml_path
        if args.hosts_yaml_path is not None
        else str(pc_web_cfg.get("hosts_yaml_path") or extra_metrics_cfg.get("hosts_yaml_path", ""))
    )
    pc_query_proxy_url = (
        args.query_proxy_url
        if args.query_proxy_url is not None
        else str(pc_web_cfg.get("query_proxy_url") or extra_metrics_cfg.get("query_proxy_url", ""))
    )
    return PCWebMetricsService(
        PCWebSettings(
            base_url=str(pc_web_cfg.get("base", "http://yapiadmin.4399.com")).strip(),
            web_origin=str(pc_web_cfg.get("web_origin", "http://yadmin.4399.com")).strip(),
            request_timeout=int(pc_web_cfg.get("request_timeout", extra_metrics_cfg.get("request_timeout", 30))),
            query_proxy_url=pc_query_proxy_url.strip(),
            hosts_yaml_path=str(pc_hosts_yaml_path).strip(),
            fenxi_base=str(extra_metrics_cfg.get("fenxi_base", "https://fenxi.4399dev.com")).strip(),
            timezone=str(extra_metrics_cfg.get("timezone", "Asia/Shanghai")),
            max_concurrency=args.max_concurrency,
        )
    )


def _prepare_runtime_paths(args: argparse.Namespace) -> None:
    paths = resolve_app_paths(args.data_dir)
    if args.data_dir is not None:
        if Path(args.config) == DEFAULT_CONFIG_PATH:
            args.config = paths.config
        if Path(args.output) == DEFAULT_OUTPUT_DIR:
            args.output = paths.output
        if Path(args.extra_auth_file) == DEFAULT_EXTRA_AUTH_FILE:
            args.extra_auth_file = paths.extra_auth
    migrated = migrate_legacy_runtime_files(paths)
    ensure_first_run_config(paths)
    for path in migrated:
        logging.info("Migrated legacy runtime file to user data directory: %s", path)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    setup_logging(args.verbose)
    _prepare_runtime_paths(args)
    if args.max_concurrency < 1:
        raise ReportError("--max-concurrency 必须大于等于 1。")
    lock_path = Path(args.output) / ".autodatareport.lock"
    try:
        with single_instance_lock(lock_path):
            run_with_args(args)
    except AlreadyRunningError as exc:
        raise ReportError(str(exc)) from exc


def run_with_args(args: argparse.Namespace) -> None:
    total_started = monotonic_time.perf_counter()
    emit_progress(2, "初始化参数")

    config = load_config(args.config)
    emit_progress(5, "加载配置完成")
    if args.push_report_file is not None:
        report_file = Path(args.push_report_file)
        if not report_file.exists():
            raise ReportError(f"待推送报告文件不存在：{report_file}")
        report_text = report_file.read_text(encoding="utf-8")
        report_date_for_push = resolve_report_date(args.date)
        chart_image_paths = build_existing_report_chart_paths(report_file.parent)
        payment_images = build_existing_payment_images(report_file.parent, report_date_for_push)
        emit_progress(20, "准备推送已有报告")
        title_override, title_prefix = resolve_feishu_doc_title(args, config)
        feishu_settings = resolve_feishu_doc_settings(args, config)
        publish_store = PublishStateStore(report_file.parent, report_date_for_push)
        publish_hash = build_publish_hash(report_file, [*chart_image_paths.values(), *payment_images.values()])
        completed = None if args.force_publish else publish_store.completed_result("feishu_main", publish_hash)
        if completed is not None:
            logging.info("Feishu doc publish skipped (same content already completed): %s", completed.get("url", ""))
            emit_progress(100, "内容未变化，已跳过重复推送")
            return
        try:
            emit_progress(85, "推送飞书文档中")
            feishu_result = publish_report_to_feishu_doc(
                settings=feishu_settings,
                report_text=report_text,
                report_date=report_date_for_push,
                title_override=title_override,
                title_prefix=title_prefix,
                report_base_dir=report_file.parent,
                chart_image_paths=chart_image_paths,
                payment_images=payment_images,
            )
            publish_store.mark_completed("feishu_main", publish_hash, {"url": feishu_result.get("url", "")})
            logging.info("Feishu doc published: %s", feishu_result.get("url", ""))
            if feishu_result.get("markdown_length"):
                logging.info("Feishu doc markdown length: %s", feishu_result.get("markdown_length"))
        except FeishuDocError as exc:
            raise ReportError(f"飞书文档推送失败: {exc}") from exc
        emit_progress(100, "推送完成")
        return
    if args.push_pc_report_file is not None:
        report_file = Path(args.push_pc_report_file)
        if not report_file.exists():
            raise ReportError(f"待推送PC报告文件不存在：{report_file}")
        report_text = report_file.read_text(encoding="utf-8")
        report_date_for_push = resolve_report_date(args.date)
        chart_image_paths = build_existing_report_chart_paths(report_file.parent)
        emit_progress(20, "准备推送已有PC报告")
        pc_title_override, pc_title_prefix = resolve_feishu_pc_doc_title(config)
        feishu_settings = resolve_feishu_doc_settings(args, config)
        publish_store = PublishStateStore(report_file.parent, report_date_for_push)
        publish_hash = build_publish_hash(report_file, chart_image_paths.values())
        completed = None if args.force_publish else publish_store.completed_result("feishu_pc", publish_hash)
        if completed is not None:
            logging.info("Feishu PC doc publish skipped (same content already completed): %s", completed.get("url", ""))
            emit_progress(100, "内容未变化，已跳过重复推送")
            return
        try:
            emit_progress(85, "推送PC飞书文档中")
            feishu_result = publish_report_to_feishu_doc(
                settings=feishu_settings,
                report_text=report_text,
                report_date=report_date_for_push,
                title_override=pc_title_override,
                title_prefix=pc_title_prefix,
                report_base_dir=report_file.parent,
                chart_image_paths=chart_image_paths,
            )
            publish_store.mark_completed("feishu_pc", publish_hash, {"url": feishu_result.get("url", "")})
            logging.info("Feishu PC doc published: %s", feishu_result.get("url", ""))
            if feishu_result.get("markdown_length"):
                logging.info("Feishu PC doc markdown length: %s", feishu_result.get("markdown_length"))
        except FeishuDocError as exc:
            raise ReportError(f"PC飞书文档推送失败: {exc}") from exc
        emit_progress(100, "PC推送完成")
        return
    if args.push_wecom_reports:
        report_date_for_push = resolve_report_date(args.date)
        output_dir, _ = ensure_output_dirs(args.output)
        date_key = build_report_file_key(report_date_for_push)
        report_files = [
            output_dir / f"{date_key}_report.txt",
            output_dir / f"{date_key}_pc_report.txt",
        ]
        emit_progress(20, "准备推送企业微信日报")
        target = str(args.wecom_target or "single").strip().lower()
        try:
            emit_progress(60, "补发飞书文档")
            main_url = ""
            pc_url = ""
            for report_file in report_files:
                if not report_file.exists():
                    continue
                feishu_result = publish_report_file_to_feishu(
                    config=config,
                    args=args,
                    report_file=report_file,
                    report_date=report_date_for_push,
                )
                if report_file.name.endswith("_pc_report.txt"):
                    pc_url = str(feishu_result.get("url") or "").strip()
                else:
                    main_url = str(feishu_result.get("url") or "").strip()
            emit_progress(85, f"推送企业微信({target})")
            result = push_reports_to_wecom_target(
                config=config,
                target=target,
                report_date=report_date_for_push,
                main_url=main_url,
                pc_url=pc_url,
            )
            logging.info(
                "WeCom bot pushed target=%s chatid=%s messages=%s",
                target,
                result.get("chatid", ""),
                result.get("message_count", 0),
            )
        except WeComBotError as exc:
            raise ReportError(f"企业微信推送失败: {exc}") from exc
        emit_progress(100, "企业微信推送完成")
        return

    extra_metrics_cfg = config.get("extra_metrics") or {}
    extra_auth_file = args.extra_auth_file
    if str(extra_auth_file).strip() == "":
        extra_auth_file = DEFAULT_EXTRA_AUTH_FILE

    if args.build_extra_auth:
        fenxi_hars = [Path(p) for p in (args.fenxi_har or extra_metrics_cfg.get("fenxi_hars") or [])]
        manage_hars = [Path(p) for p in (args.manage_har or extra_metrics_cfg.get("manage_hars") or [])]
        if not fenxi_hars and not manage_hars:
            raise ReportError("No HAR files provided. Use --fenxi-har/--manage-har or config extra_metrics.fenxi_hars/manage_hars.")
        for har_path in [*fenxi_hars, *manage_hars]:
            if not har_path.exists():
                raise ReportError(f"HAR file not found: {har_path}")
        build_extra_auth_file(fenxi_hars=fenxi_hars, manage_hars=manage_hars, output_path=extra_auth_file)
        logging.info("Built extra auth file: %s", extra_auth_file)
        if args.build_extra_auth_only:
            return

    if args.repair_auth_only:
        emit_progress(20, "打开 Chrome 修复 fenxi/PC 登录态")
        result = run_auth_repair(config, args, extra_metrics_cfg, extra_auth_file)
        report_date_for_state = resolve_report_date(args.date)
        write_run_state(
            Path(args.output),
            report_date_for_state,
            {
                "last_phase": "auth_repair_only",
                "repair_targets": result.get("updated_targets") or [],
                "repair_attempted": True,
                "repair_result": "ok",
                "auth_repair_log": result.get("log_path", ""),
            },
        )
        logging.info("fenxi/PC 登录态修复已完成；repair-only 不执行 870/505 全平台预检。")
        write_run_state(Path(args.output), report_date_for_state, {"preflight_result": "skipped_for_repair_only"})
        emit_progress(100, "认证修复完成")
        return

    if args.check_extra_auth:
        emit_progress(20, "执行全平台登录态预检")
        run_full_auth_preflight_with_repair(
            config,
            args,
            extra_metrics_cfg,
            extra_auth_file,
            phase="auth_check",
        )
        logging.info("全平台登录态预检通过。")
        emit_progress(100, "预检通过")
        return

    base_url = config.get("base_url")
    if not base_url:
        raise ReportError("Config missing base_url.")

    default_cookie_source = (
        args.cookie
        or config.get("session_cookie")
        or os.getenv("REPORT_PHPSESSID")
    )
    default_date_input = args.date
    if not args.no_runtime_gui:
        gui_cookie, gui_date = prompt_runtime_inputs(default_cookie_source, default_date_input)
        if gui_cookie:
            args.cookie = gui_cookie
        if gui_date:
            args.date = gui_date

    auth_started = monotonic_time.perf_counter()
    emit_progress(8, "执行全平台登录态预检")
    run_full_auth_preflight_with_repair(
        config,
        args,
        extra_metrics_cfg,
        extra_auth_file,
        phase="daily_report",
    )
    logging.info("[TIMING] stage=auth_preflight seconds=%.3f", monotonic_time.perf_counter() - auth_started)
    emit_progress(10, "全平台登录态预检通过")

    report_date = resolve_report_date(args.date)
    date_cn = f"{report_date.year}年{report_date.month}月{report_date.day}日"
    cookie = resolve_cookie(args.cookie, config)
    emit_progress(12, f"开始处理 {report_date.isoformat()} 数据")
    timeout = float(config.get("timeout", 30))
    network_cfg = config.get("network") or {}
    if not isinstance(network_cfg, dict):
        raise ReportError("Config field network must be a mapping when provided.")
    hosts_yaml_path_870 = (
        args.network_hosts_yaml
        if args.network_hosts_yaml is not None
        else str(network_cfg.get("hosts_yaml_path") or "")
    ).strip()
    hosts_map_870 = load_hosts_map(hosts_yaml_path_870) if hosts_yaml_path_870 else {}
    if hosts_map_870:
        logging.info("870 hosts map loaded: %s (%d hosts)", hosts_yaml_path_870, len(hosts_map_870))

    targets_config = config.get("targets") or {}
    if not targets_config:
        raise ReportError("Config missing targets definitions.")

    ordered_section_keys = config.get("report_section_order") or list(targets_config.keys())
    default_time_field = config.get("default_time_field", DEFAULT_TIME_FIELD)
    default_http_method = config.get("default_http_method", "post")
    auto_query_params = build_auto_query_params(config.get("auto_query_params"), report_date)
    previous_auto_query_params = build_auto_query_params(
        config.get("auto_query_params"),
        report_date - timedelta(days=1),
    )
    output_dir, charts_dir = ensure_output_dirs(args.output)

    collect_started = monotonic_time.perf_counter()
    results: Dict[str, TargetResult] = {}
    configured_keys: List[str] = []
    for key in ordered_section_keys:
        target_cfg = targets_config.get(key)
        if not target_cfg:
            logging.warning("Target %s is listed in order but missing configuration.", key)
            continue
        configured_keys.append(key)

    def collect_target(key: str) -> TargetResult:
        session = requests.Session()
        try:
            configure_870_session(session, args, config)
            session.headers.update({"Cookie": cookie, "User-Agent": config.get("user_agent", "Mozilla/5.0")})
            return build_target_result(
                key=key,
                config=targets_config[key],
                session=session,
                base_url=base_url,
                base_date=report_date,
                previous_date=report_date - timedelta(days=1),
                timeout=timeout,
                default_time_field=default_time_field,
                default_http_method=default_http_method,
                auto_query_params=auto_query_params,
                previous_auto_query_params=previous_auto_query_params,
                hosts_map=hosts_map_870,
            )
        finally:
            session.close()

    total_targets = max(1, len(configured_keys))
    max_workers = min(args.max_concurrency, total_targets)
    emit_progress(15, f"并发抓取870数据（上限 {max_workers}）")
    unordered_results: Dict[str, TargetResult] = {}
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="report-870") as executor:
        futures = {executor.submit(collect_target, key): key for key in configured_keys}
        for idx, future in enumerate(as_completed(futures), start=1):
            key = futures[future]
            unordered_results[key] = future.result()
            emit_progress(15 + int(idx * 40 / total_targets), f"870数据完成：{key}")

    for key in configured_keys:
        result = unordered_results[key]
        if config.get("generate_charts", True) and not args.no_charts:
            chart_filename = f"{key}.png"
            chart_path = charts_dir / chart_filename
            generated = generate_chart(result, chart_path)
            if generated:
                result.chart_path = generated.relative_to(output_dir)
        results[key] = result
    logging.info("[TIMING] stage=collect_870 seconds=%.3f", monotonic_time.perf_counter() - collect_started)
    emit_progress(62, "870数据抓取完成")

    analysis_sentences: List[str] = []
    if config.get("analysis_groups"):
        analysis_sentences.extend(build_top_sentences(config["analysis_groups"], results))
    if config.get("anomaly_rules"):
        analysis_sentences.extend(build_anomaly_sentences(config["anomaly_rules"], results))

    extra_metrics_enabled = bool(args.with_extra_metrics or extra_metrics_cfg.get("enabled"))
    extra_metrics_block: Optional[str] = None
    extra_payment_images: Dict[str, str] = {}
    pc_report_notes: Dict[str, str] = {}
    pc_report_member_summary: Dict[str, str] = {}
    pc_report_top_games: List[Dict[str, str]] = []
    pc_report_warnings: List[str] = []
    extra_started = monotonic_time.perf_counter()
    if extra_metrics_enabled:
        emit_progress(68, "执行分析后台与505预检/抓取")
        extra_metrics_data: Dict[str, Any] = {"notes": {}, "top_games": [], "warnings": [], "payment_tables": {}}
        if not extra_auth_file.exists():
            extra_metrics_data["warnings"].append(f"扩展认证文件不存在：{extra_auth_file}")
        else:
            extra_auth = load_extra_auth(extra_auth_file)
            extra_auth_meta = load_extra_auth_meta(extra_auth_file)
            auth_age_hours = get_extra_auth_age_hours(extra_auth_file, extra_auth_meta)
            if auth_age_hours is not None and auth_age_hours > float(args.extra_auth_max_age_hours):
                extra_metrics_data["warnings"].append(
                    f"扩展认证文件已超过{auth_age_hours:.1f}小时（阈值={args.extra_auth_max_age_hours}小时），建议重新手机验证码登录并刷新认证文件。"
                )
            hosts_yaml_path = (
                args.hosts_yaml_path
                if args.hosts_yaml_path is not None
                else str(extra_metrics_cfg.get("hosts_yaml_path", ""))
            )
            query_proxy_url = (
                args.query_proxy_url
                if args.query_proxy_url is not None
                else str(extra_metrics_cfg.get("query_proxy_url", ""))
            )
            extra_settings = ExtraSettings(
                timezone=str(extra_metrics_cfg.get("timezone", "Asia/Shanghai")),
                request_timeout=int(extra_metrics_cfg.get("request_timeout", timeout)),
                query_proxy_url=query_proxy_url.strip(),
                hosts_yaml_path=hosts_yaml_path.strip(),
                query_debug_log_path=(output_dir / "query_debug.jsonl"),
                fenxi_base=str(extra_metrics_cfg.get("fenxi_base", "https://<FENXI_HOST>")).strip(),
                manage_base=str(extra_metrics_cfg.get("manage_base", "http://<MANAGE_HOST>")).strip(),
                max_concurrency=args.max_concurrency,
            )
            extra_service = ExtraMetricsService(extra_settings)
            try:
                extra_metrics_data = asyncio.run(
                    extra_service.fetch(
                        query_date=report_date,
                        fenxi_auth=extra_auth.get("fenxi"),
                        manage_auth=extra_auth.get("505"),
                    )
                )
                payment_tables = extra_metrics_data.get("payment_tables")
                if isinstance(payment_tables, dict) and payment_tables:
                    try:
                        payment_image_paths = render_payment_table_images(payment_tables, charts_dir)
                        if payment_image_paths:
                            payment_images: Dict[str, str] = {}
                            for key, abs_path in payment_image_paths.items():
                                try:
                                    payment_images[key] = str(abs_path.relative_to(output_dir))
                                except ValueError:
                                    payment_images[key] = str(abs_path)
                            extra_metrics_data["payment_images"] = payment_images
                            extra_payment_images = dict(payment_images)
                    except Exception as exc:  # pylint: disable=broad-except
                        extra_metrics_data.setdefault("warnings", []).append(f"505图表生成失败: {exc}")
                        logging.warning("Extra payment table image render failed: %s", exc)
            except Exception as exc:  # pylint: disable=broad-except
                extra_metrics_data = {"notes": {}, "top_games": [], "warnings": [f"扩展指标请求失败: {exc}"], "payment_tables": {}}
                logging.warning("Extra metrics fetch failed: %s", exc)
        if not extra_payment_images:
            fallback_payment_images = extra_metrics_data.get("payment_images")
            if isinstance(fallback_payment_images, dict):
                extra_payment_images = {
                    str(key): str(value)
                    for key, value in fallback_payment_images.items()
                    if str(value).strip()
                }
        extra_metrics_block = render_extra_metrics_block(extra_metrics_data)
    logging.info("[TIMING] stage=extra_metrics seconds=%.3f", monotonic_time.perf_counter() - extra_started)

    pc_web_cfg = config.get("pc_web_metrics") or {}
    pc_started = monotonic_time.perf_counter()
    if bool(pc_web_cfg.get("enabled")):
        emit_progress(74, "抓取PC后台数据")
        if not extra_auth_file.exists():
            raise ReportError(f"PC后台数据抓取失败: 认证文件不存在：{extra_auth_file}")
        extra_auth = load_extra_auth(extra_auth_file)
        pc_auth_key = str(pc_web_cfg.get("auth_key", "pc_web")).strip() or "pc_web"
        pc_auth = extra_auth.get(pc_auth_key) or extra_auth.get("pc_web")
        pc_service = create_pc_web_service(config, args, extra_metrics_cfg)
        strict_mode = bool(pc_web_cfg.get("strict", True))
        try:
            pc_web_data = asyncio.run(
                pc_service.fetch(
                    query_date=report_date,
                    auth=pc_auth,
                    top_n=int(pc_web_cfg.get("top_n", 10)),
                )
            )
            pc_report_notes = build_pc_notes(pc_web_data.get("notes") if isinstance(pc_web_data, dict) else {})
            pc_report_top_games = build_pc_top_games(pc_web_data.get("top_games") if isinstance(pc_web_data, dict) else [])
        except Exception as exc:
            if strict_mode:
                raise ReportError(f"PC后台数据抓取失败: {exc}") from exc
            pc_report_warnings.append(f"PC后台数据抓取失败: {exc}")

        if bool(pc_web_cfg.get("include_member_metrics", True)):
            try:
                pc_member_data = asyncio.run(
                    pc_service.fetch_member_metrics(
                        query_date=report_date,
                        fenxi_auth=extra_auth.get("fenxi"),
                    )
                )
                pc_report_member_summary = build_pc_member_summary(
                    pc_member_data.get("notes") if isinstance(pc_member_data, dict) else {}
                )
            except Exception as exc:
                if strict_mode:
                    raise ReportError(f"PC会员数据抓取失败: {exc}") from exc
                pc_report_warnings.append(f"PC会员数据抓取失败: {exc}")
    logging.info("[TIMING] stage=pc_metrics seconds=%.3f", monotonic_time.perf_counter() - pc_started)
    emit_progress(80, "渲染日报内容")

    render_started = monotonic_time.perf_counter()
    output_path = render_report(
        template_dir=args.template_dir,
        template_name=args.template_name,
        output_dir=output_dir,
        date_cn=date_cn,
        results=results,
        ordered_sections=ordered_section_keys,
        analysis_sentences=analysis_sentences,
        extra_metrics_block=extra_metrics_block,
    )
    logging.info("Report generated: %s", output_path)
    chart_image_paths = {
        key: str(result.chart_path)
        for key, result in results.items()
        if result.chart_path is not None
    }
    pc_target = results.get("pc_cloud")
    pc_report_path: Optional[Path] = None
    if pc_target:
        pc_report_path = render_pc_report(
            template_dir=args.template_dir,
            template_name=DEFAULT_PC_TEMPLATE_NAME,
            output_dir=output_dir,
            date_cn=date_cn,
            target=pc_target,
            pc_notes=pc_report_notes,
            pc_member_summary=pc_report_member_summary,
            pc_top_games=pc_report_top_games,
            pc_warnings=pc_report_warnings,
        )
        logging.info("PC cloud report generated: %s", pc_report_path)
    logging.info("[TIMING] stage=render_reports seconds=%.3f", monotonic_time.perf_counter() - render_started)

    publish_store = PublishStateStore(output_dir, report_date)
    feishu_settings: Optional[FeishuDocSettings] = None
    feishu_main_url = ""
    if should_push_feishu_doc(args, config):
        emit_progress(90, "推送飞书文档")
        title_override, title_prefix = resolve_feishu_doc_title(args, config)
        feishu_settings = resolve_feishu_doc_settings(args, config)
        main_hash = build_publish_hash(output_path, [*chart_image_paths.values(), *extra_payment_images.values()])
        completed = None if args.force_publish else publish_store.completed_result("feishu_main", main_hash)
        if completed is not None:
            feishu_main_url = str(completed.get("url") or "").strip()
            logging.info("Feishu doc publish skipped (same content already completed): %s", feishu_main_url)
        else:
            publish_started = monotonic_time.perf_counter()
            try:
                feishu_result = publish_report_to_feishu_doc(
                    settings=feishu_settings,
                    report_text=output_path.read_text(encoding="utf-8"),
                    report_date=report_date,
                    title_override=title_override,
                    title_prefix=title_prefix,
                    report_base_dir=output_path.parent,
                    chart_image_paths=chart_image_paths,
                    payment_images=extra_payment_images,
                )
                feishu_main_url = str(feishu_result.get("url") or "").strip()
                publish_store.mark_completed("feishu_main", main_hash, {"url": feishu_main_url})
                logging.info("Feishu doc published: %s", feishu_main_url)
                logging.info("[TIMING] stage=publish_feishu_main seconds=%.3f", monotonic_time.perf_counter() - publish_started)
                if feishu_result.get("markdown_length"):
                    logging.info("Feishu doc markdown length: %s", feishu_result.get("markdown_length"))
            except FeishuDocError as exc:
                raise ReportError(f"飞书文档推送失败: {exc}") from exc

    feishu_pc_url = ""
    if pc_target and pc_report_path is not None:
        if feishu_settings is None and should_push_feishu_doc(args, config):
            feishu_settings = resolve_feishu_doc_settings(args, config)
        if feishu_settings is not None and should_push_feishu_pc_doc(config):
            emit_progress(95, "推送PC飞书文档")
            pc_title_override, pc_title_prefix = resolve_feishu_pc_doc_title(config)
            pc_chart_image_paths: Dict[str, str] = {}
            if pc_target.chart_path is not None:
                pc_chart_image_paths["pc_cloud"] = str(pc_target.chart_path)
            pc_hash = build_publish_hash(pc_report_path, pc_chart_image_paths.values())
            completed = None if args.force_publish else publish_store.completed_result("feishu_pc", pc_hash)
            if completed is not None:
                feishu_pc_url = str(completed.get("url") or "").strip()
                logging.info("Feishu PC doc publish skipped (same content already completed): %s", feishu_pc_url)
            else:
                publish_started = monotonic_time.perf_counter()
                try:
                    pc_result = publish_report_to_feishu_doc(
                        settings=feishu_settings,
                        report_text=pc_report_path.read_text(encoding="utf-8"),
                        report_date=report_date,
                        title_override=pc_title_override,
                        title_prefix=pc_title_prefix,
                        report_base_dir=pc_report_path.parent,
                        chart_image_paths=pc_chart_image_paths,
                    )
                    feishu_pc_url = str(pc_result.get("url") or "").strip()
                    publish_store.mark_completed("feishu_pc", pc_hash, {"url": feishu_pc_url})
                    logging.info("Feishu PC doc published: %s", feishu_pc_url)
                    logging.info("[TIMING] stage=publish_feishu_pc seconds=%.3f", monotonic_time.perf_counter() - publish_started)
                    if pc_result.get("markdown_length"):
                        logging.info("Feishu PC doc markdown length: %s", pc_result.get("markdown_length"))
                except FeishuDocError as exc:
                    raise ReportError(f"PC飞书文档推送失败: {exc}") from exc

    if should_push_wecom_bot(config, args):
        auto_targets = resolve_wecom_auto_targets(config)
        for target in auto_targets:
            emit_progress(98, f"推送企业微信({target})")
            wecom_hash = content_hash([report_date.isoformat(), target, feishu_main_url, feishu_pc_url])
            completed = None if args.force_publish else publish_store.completed_result(f"wecom_{target}", wecom_hash)
            if completed is not None:
                logging.info("WeCom bot publish skipped target=%s (same content already completed)", target)
                continue
            try:
                result = push_reports_to_wecom_target(
                    config=config,
                    target=target,
                    report_date=report_date,
                    main_url=feishu_main_url,
                    pc_url=feishu_pc_url,
                )
                publish_store.mark_completed(f"wecom_{target}", wecom_hash, {"message_count": result.get("message_count", 0)})
                logging.info(
                    "WeCom bot pushed target=%s chatid=%s messages=%s",
                    target,
                    result.get("chatid", ""),
                    result.get("message_count", 0),
                )
            except WeComBotError as exc:
                strict = bool((config.get("wecom_bot") or {}).get("strict", False))
                if strict:
                    raise ReportError(f"企业微信推送失败({target}): {exc}") from exc
                logging.warning("WeCom bot push failed target=%s: %s", target, exc)
    logging.info("[TIMING] stage=total seconds=%.3f", monotonic_time.perf_counter() - total_started)
    emit_progress(100, "任务完成")


if __name__ == "__main__":
    try:
        main()
    except ReportError as exc:
        logging.error("Failed to generate report: %s", exc)
        sys.exit(1)
    except requests.RequestException as exc:
        logging.error("HTTP request failed: %s", exc)
        sys.exit(2)
