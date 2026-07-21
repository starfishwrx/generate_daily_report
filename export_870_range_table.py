#!/usr/bin/env python3
"""
Export merged 870 hourly concurrency and queue tables for a custom ID list.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Sequence

import requests

from generate_daily_report import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_DIR,
    ReportError,
    build_auto_query_params,
    collect_series_for_queries,
    combine_series,
    configure_870_session,
    format_value,
    load_config,
    resolve_cookie,
    resolve_report_date,
)
from network_hosts import load_hosts_map


DEFAULT_CONCURRENCY_PATTERN = "used_container_num_0"
DEFAULT_QUEUE_PATTERN = "line_member_num_0"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export merged 870 hourly concurrency/queue tables for a custom ID list."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to YAML configuration file.",
    )
    parser.add_argument(
        "--start-date",
        required=True,
        help="Start date in YYYY-MM-DD.",
    )
    parser.add_argument(
        "--end-date",
        required=True,
        help="End date in YYYY-MM-DD.",
    )
    parser.add_argument(
        "--game-id",
        nargs="+",
        required=True,
        help="870 game/container IDs. Supports spaces or commas.",
    )
    parser.add_argument(
        "--label",
        default="洛克王国：世界",
        help="Label used in output headers.",
    )
    parser.add_argument(
        "--slug",
        default="rock_kingdom_world",
        help="ASCII slug used in output filenames.",
    )
    parser.add_argument(
        "--cookie",
        default=None,
        help="Override PHP session cookie (format: PHPSESSID=...).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR / "custom_tables",
        help="Directory to write exported tables.",
    )
    parser.add_argument(
        "--concurrency-pattern",
        action="append",
        default=[],
        help="Regex used to match concurrency series. Can be repeated.",
    )
    parser.add_argument(
        "--queue-pattern",
        action="append",
        default=[],
        help="Regex used to match queue series. Can be repeated.",
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
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args(argv)


def normalize_game_ids(values: Sequence[str]) -> List[str]:
    ordered: List[str] = []
    seen: set[str] = set()
    for raw in values:
        for part in re.split(r"[\s,，]+", str(raw).strip()):
            if not part:
                continue
            if not re.fullmatch(r"\d+", part):
                raise ReportError(f"Invalid game/container ID: {part}")
            if part in seen:
                continue
            ordered.append(part)
            seen.add(part)
    if not ordered:
        raise ReportError("At least one valid game/container ID is required.")
    return ordered


def sanitize_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip())
    slug = slug.strip("_")
    return slug or "custom_target"


def iter_dates(start_date: date, end_date: date) -> List[date]:
    if end_date < start_date:
        raise ReportError("--end-date must be on or after --start-date.")
    days: List[date] = []
    current = start_date
    while current <= end_date:
        days.append(current)
        current += timedelta(days=1)
    return days


def resolve_hour(point) -> int | None:
    if point.hour is not None and 0 <= int(point.hour) <= 23:
        return int(point.hour)
    match = re.match(r"^(\d{1,2})", str(point.raw_label or "").strip())
    if not match:
        return None
    hour = int(match.group(1))
    if 0 <= hour <= 23:
        return hour
    return None


def collapse_to_hourly(points) -> Dict[int, float]:
    buckets: Dict[int, List[float]] = {hour: [] for hour in range(24)}
    for point in points:
        hour = resolve_hour(point)
        if hour is None:
            continue
        buckets[hour].append(float(point.value))
    hourly: Dict[int, float] = {}
    for hour in range(24):
        values = buckets[hour]
        if len(values) > 1:
            logging.debug("Hour %02d has %d values, using max=%s", hour, len(values), format_value(max(values)))
        hourly[hour] = max(values) if values else 0.0
    return hourly


def serialize_number(value: float) -> str:
    return format_value(value)


def build_daily_rows(
    session: requests.Session,
    config: Dict[str, object],
    args: argparse.Namespace,
    query_date: date,
    game_ids: Sequence[str],
    hosts_map: Dict[str, str] | None,
) -> List[Dict[str, str]]:
    base_url = str(config.get("base_url") or "").strip()
    if not base_url:
        raise ReportError("Config missing base_url.")

    default_http_method = str(config.get("default_http_method", "post") or "post")
    default_time_field = str(config.get("default_time_field", "ctime") or "ctime")
    timeout = float(config.get("timeout", 30))
    auto_params = build_auto_query_params(config.get("auto_query_params"), query_date)

    queries = [{"params": {"game_id": game_id}} for game_id in game_ids]
    concurrency_patterns = args.concurrency_pattern or [DEFAULT_CONCURRENCY_PATTERN]
    queue_patterns = args.queue_pattern or [DEFAULT_QUEUE_PATTERN]

    all_concurrency_series, all_queue_series = collect_series_for_queries(
        queries=queries,
        auto_params=auto_params,
        concurrency_patterns=concurrency_patterns,
        queue_patterns=queue_patterns,
        session=session,
        base_url=base_url,
        base_date=query_date,
        timeout=timeout,
        default_http_method=default_http_method,
        time_field=default_time_field,
        hosts_map=hosts_map,
    )

    combined_concurrency = combine_series(all_concurrency_series) if all_concurrency_series else []
    combined_queue = combine_series(all_queue_series) if all_queue_series else []
    hourly_concurrency = collapse_to_hourly(combined_concurrency)
    hourly_queue = collapse_to_hourly(combined_queue)

    concurrency_header = f"{args.label}总并发"
    queue_header = f"{args.label}总排队"
    rows: List[Dict[str, str]] = []
    for hour in range(24):
        rows.append(
            {
                "日期": query_date.isoformat(),
                "小时": f"{hour:02d}:00",
                concurrency_header: serialize_number(hourly_concurrency[hour]),
                queue_header: serialize_number(hourly_queue[hour]),
            }
        )
    return rows


def write_csv(output_path: Path, rows: Sequence[Dict[str, str]]) -> None:
    if not rows:
        raise ReportError("No rows to write.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with output_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(
    output_path: Path,
    rows: Sequence[Dict[str, str]],
    label: str,
    start_date: date,
    end_date: date,
    game_ids: Sequence[str],
) -> None:
    if not rows:
        raise ReportError("No rows to write.")
    headers = list(rows[0].keys())
    lines = [
        f"# {label} 870 小时明细表",
        "",
        f"- 日期范围：{start_date.isoformat()} 到 {end_date.isoformat()}",
        f"- 合并 ID：{', '.join(game_ids)}",
        "",
        f"| {' | '.join(headers)} |",
        f"| {' | '.join(['---'] * len(headers))} |",
    ]
    for row in rows:
        lines.append(f"| {' | '.join(row[h] for h in headers)} |")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_session(config: Dict[str, object], args: argparse.Namespace) -> requests.Session:
    cookie = resolve_cookie(args.cookie, config)
    session = requests.Session()
    configure_870_session(session, args, config)
    session.headers.update(
        {
            "Cookie": cookie,
            "User-Agent": str(config.get("user_agent", "Mozilla/5.0")),
        }
    )
    return session


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        config = load_config(args.config)
        game_ids = normalize_game_ids(args.game_id)
        start_date = resolve_report_date(args.start_date)
        end_date = resolve_report_date(args.end_date)
        query_dates = iter_dates(start_date, end_date)
        slug = sanitize_slug(args.slug)
        network_cfg = config.get("network") or {}
        if not isinstance(network_cfg, dict):
            raise ReportError("Config field network must be a mapping when provided.")
        hosts_yaml_path = (
            args.network_hosts_yaml
            if args.network_hosts_yaml is not None
            else str(network_cfg.get("hosts_yaml_path") or "")
        ).strip()
        hosts_map = load_hosts_map(hosts_yaml_path) if hosts_yaml_path else None

        all_rows: List[Dict[str, str]] = []
        session = build_session(config, args)
        try:
            for query_date in query_dates:
                logging.info("Fetching 870 rows for %s", query_date.isoformat())
                all_rows.extend(build_daily_rows(session, config, args, query_date, game_ids, hosts_map))
        finally:
            session.close()

        date_part = f"{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}"
        csv_path = args.output_dir / f"{slug}_{date_part}_hourly.csv"
        md_path = args.output_dir / f"{slug}_{date_part}_hourly.md"
        write_csv(csv_path, all_rows)
        write_markdown(md_path, all_rows, args.label, start_date, end_date, game_ids)

        print(f"CSV: {csv_path}")
        print(f"Markdown: {md_path}")
        print(f"Rows: {len(all_rows)}")
        return 0
    except Exception as exc:  # pylint: disable=broad-except
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
