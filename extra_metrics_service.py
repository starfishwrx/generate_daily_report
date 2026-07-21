from __future__ import annotations

import json
import random
import re
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from autodatareport.events import current_metrics

from async_utils import RetryingAsyncClient, gather_limited
from network_hosts import load_hosts_map, rewrite_url_with_hosts_map
from tz_compat import get_tzinfo


@dataclass(frozen=True)
class ExtraSettings:
    timezone: str
    request_timeout: int
    query_proxy_url: str
    hosts_yaml_path: str
    query_debug_log_path: Path
    fenxi_base: str
    manage_base: str
    max_concurrency: int = 4


class DebugLogStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")

    def write(self, event: dict[str, Any]) -> None:
        self._rotate_if_needed()
        item = dict(event)
        item.setdefault("ts", datetime.now(timezone.utc).isoformat())
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def _rotate_if_needed(self, max_bytes: int = 5 * 1024 * 1024, backups: int = 3) -> None:
        if not self.path.exists() or self.path.stat().st_size < max_bytes:
            return
        oldest = self.path.with_name(f"{self.path.name}.{backups}")
        if oldest.exists():
            oldest.unlink()
        for index in range(backups - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        self.path.write_text("", encoding="utf-8")

    def tail(self, lines: int = 200) -> list[dict[str, Any]]:
        raw = self.path.read_text(encoding="utf-8").splitlines()
        out = []
        for row in raw[-max(1, lines):]:
            try:
                out.append(json.loads(row))
            except json.JSONDecodeError:
                continue
        return out


FENXI_MEDIA_ID = "media-eb40cb50d15a49a9"
FENXI_TOPIC = "gamebox_event"

FENXI_COMPONENT_PAY_RATE = "124012e0_9fda_11ee_97d3_63552b12082d"
FENXI_COMPONENT_MEMBER_RECHARGE = "2f106fb0_9fd9_11ee_97d3_63552b12082d"
FENXI_COMPONENT_MEMBER_DAILY = "f2967120_8ff1_11ee_a5c5_d316c750b6d6"


BI_PAYLOAD_PAY_RATE: dict[str, Any] = {
    "reportId": 656,
    "componentId": FENXI_COMPONENT_PAY_RATE,
    "modelId": 658,
    "fieldList": [
        {
            "showname": "订单数",
            "nullDisplay": "-",
            "describe": "",
            "fieldId": "thvxagsslrbm",
            "uniqueId": "thvxagsslrbm",
            "dateCompare": None,
            "rawFieldId": "thvxagsslrbm",
            "role": "measure",
        },
        {
            "showname": "活跃用户",
            "nullDisplay": "-",
            "describe": "全局_云游戏进入",
            "fieldId": "j58uhvh_kui4",
            "uniqueId": "j58uhvh_kui4",
            "dateCompare": None,
            "rawFieldId": "j58uhvh_kui4",
            "role": "measure",
        },
        {
            "showname": "付费率",
            "nullDisplay": "-",
            "describe": "订单数/活跃用户",
            "fieldId": "fl6dt6_kps8e",
            "uniqueId": "fl6dt6_kps8e",
            "dateCompare": None,
            "rawFieldId": "fl6dt6_kps8e",
            "role": "measure",
        },
    ],
    "orderBy": [],
    "page": 1,
    "pageSize": 100,
    "aggFunction": [
        {"agg": "SUM", "fieldId": "thvxagsslrbm", "uniqueId": "thvxagsslrbm"},
        {"agg": "AVG", "fieldId": "j58uhvh_kui4", "uniqueId": "j58uhvh_kui4"},
    ],
    "showDataFormat": [
        {"fieldId": "thvxagsslrbm", "uniqueId": "thvxagsslrbm", "isThousand": True, "isPercent": False, "decimalDigits": 0},
        {"fieldId": "j58uhvh_kui4", "uniqueId": "j58uhvh_kui4", "isThousand": True, "isPercent": False, "decimalDigits": 0},
        {"fieldId": "fl6dt6_kps8e", "uniqueId": "fl6dt6_kps8e", "isThousand": True, "isPercent": True, "decimalDigits": 2},
    ],
    "showDateFormat": [],
    "componentType": "normal-table",
    "isCheckBigTable": False,
    "componentTitle": "付费率",
    "statisticsDirection": "UP",
    "variables": [
        {
            "showName": "datekeyPh",
            "dateFormat": "yyyyMMdd",
            "dataType": "DATE",
            "type": "placeholder",
            "key": "datekeyPh",
            "impactPage": "ALL_PAGE",
            "filterList": [
                {
                    "key": None,
                    "value": [],
                    "operator": "DURING",
                    "dataType": None,
                    "range": [],
                    "period": "YESTERDAY",
                    "timeType": "STANDARD",
                    "role": None,
                    "decimalDigits": None,
                    "dateLimitTip": None,
                    "error": "",
                }
            ],
            "filterRule": "({0})",
            "__for": "inner",
            "sourceType": "component",
        }
    ],
    "filter": {"logicalOperator": "AND", "conditions": []},
    "fieldFilter": [],
}

BI_PAYLOAD_MEMBER_RECHARGE: dict[str, Any] = {
    "reportId": 656,
    "componentId": FENXI_COMPONENT_MEMBER_RECHARGE,
    "modelId": 659,
    "fieldList": [
        {
            "showname": "当前充值金额",
            "nullDisplay": "-",
            "describe": "",
            "fieldId": "2quuthxeb6el",
            "uniqueId": "2quuthxeb6el",
            "dateCompare": None,
            "rawFieldId": "2quuthxeb6el",
            "role": "measure",
        },
        {
            "showname": "对比充值金额",
            "nullDisplay": "-",
            "describe": "",
            "fieldId": "mcc48oncx466",
            "uniqueId": "mcc48oncx466",
            "dateCompare": None,
            "rawFieldId": "mcc48oncx466",
            "role": "measure",
        },
        {
            "showname": "涨幅",
            "nullDisplay": "-",
            "describe": "",
            "fieldId": "r717ar12dmx0",
            "uniqueId": "r717ar12dmx0",
            "dateCompare": None,
            "rawFieldId": "r717ar12dmx0",
            "role": "measure",
        },
    ],
    "orderBy": [],
    "page": 1,
    "pageSize": 100,
    "aggFunction": [
        {"agg": "SUM", "fieldId": "2quuthxeb6el", "uniqueId": "2quuthxeb6el"},
        {"agg": "SUM", "fieldId": "mcc48oncx466", "uniqueId": "mcc48oncx466"},
    ],
    "showDataFormat": [
        {"fieldId": "2quuthxeb6el", "uniqueId": "2quuthxeb6el", "isThousand": True, "isPercent": False, "decimalDigits": 0},
        {"fieldId": "mcc48oncx466", "uniqueId": "mcc48oncx466", "isThousand": True, "isPercent": False, "decimalDigits": 0},
        {"fieldId": "r717ar12dmx0", "uniqueId": "r717ar12dmx0", "isThousand": True, "isPercent": True, "decimalDigits": 2},
    ],
    "showDateFormat": [],
    "componentType": "normal-table",
    "isCheckBigTable": False,
    "componentTitle": "充值涨幅",
    "statisticsDirection": "UP",
    "variables": [
        {
            "showName": "datekeyPh1",
            "dateFormat": "yyyyMMdd",
            "dataType": "DATE",
            "type": "placeholder",
            "key": "datekeyPh1",
            "impactPage": "ALL_PAGE",
            "filterList": [
                {"key": None, "value": [], "operator": "DURING", "dataType": None, "range": [-1, -1], "period": None, "timeType": "DYNAMIC", "role": None, "decimalDigits": None}
            ],
            "filterRule": "({0})",
            "__for": "inner",
            "sourceType": "component",
        },
        {
            "showName": "datekeyPh2",
            "dateFormat": "yyyyMMdd",
            "dataType": "DATE",
            "type": "placeholder",
            "key": "datekeyPh2",
            "impactPage": "ALL_PAGE",
            "filterList": [
                {"key": None, "value": [], "operator": "DURING", "dataType": None, "range": [-8, -8], "period": None, "timeType": "DYNAMIC", "role": None, "decimalDigits": None}
            ],
            "filterRule": "({0})",
            "__for": "inner",
            "sourceType": "component",
        },
    ],
    "filter": {"logicalOperator": "AND", "conditions": []},
    "fieldFilter": [],
}

BI_PAYLOAD_MEMBER_DAILY: dict[str, Any] = {
    "reportId": 656,
    "componentId": FENXI_COMPONENT_MEMBER_DAILY,
    "modelId": 636,
    "fieldList": [
        {"showname": "统计日期", "nullDisplay": "-", "describe": "", "fieldId": "yewst5mvg2xk", "uniqueId": "yewst5mvg2xk", "dateCompare": None, "rawFieldId": "yewst5mvg2xk", "role": "dimension"},
        {"showname": "占比", "nullDisplay": "-", "describe": "", "fieldId": "22hyzofn_0oo", "uniqueId": "22hyzofn_0oo", "dateCompare": None, "rawFieldId": "22hyzofn_0oo", "role": "measure"},
        {"showname": "当日云游戏会员数", "nullDisplay": "-", "describe": "", "fieldId": "ngins6tydctq", "uniqueId": "ngins6tydctq", "dateCompare": None, "rawFieldId": "ngins6tydctq", "role": "measure"},
        {"showname": "玩49云游戏人数", "nullDisplay": "-", "describe": "", "fieldId": "extrxio_x1gw", "uniqueId": "extrxio_x1gw", "dateCompare": None, "rawFieldId": "extrxio_x1gw", "role": "measure"},
    ],
    "orderBy": [{"order": "desc", "orderIdx": 1, "fieldId": "yewst5mvg2xk", "uniqueId": "yewst5mvg2xk"}],
    "page": 1,
    "pageSize": 100,
    "aggFunction": [
        {"agg": "SUM", "fieldId": "22hyzofn_0oo", "uniqueId": "22hyzofn_0oo"},
        {"agg": "SUM", "fieldId": "ngins6tydctq", "uniqueId": "ngins6tydctq"},
        {"agg": "AVG", "fieldId": "extrxio_x1gw", "uniqueId": "extrxio_x1gw"},
    ],
    "showDataFormat": [
        {"fieldId": "22hyzofn_0oo", "uniqueId": "22hyzofn_0oo", "isThousand": True, "isPercent": True, "decimalDigits": 2},
        {"fieldId": "ngins6tydctq", "uniqueId": "ngins6tydctq", "isThousand": True, "isPercent": False, "decimalDigits": 0},
        {"fieldId": "extrxio_x1gw", "uniqueId": "extrxio_x1gw", "isThousand": True, "isPercent": False, "decimalDigits": 0},
    ],
    "showDateFormat": [],
    "componentType": "normal-table",
    "isCheckBigTable": False,
    "componentTitle": "49云游戏会员占比",
    "statisticsDirection": "UP",
    "variables": [
        {
            "showName": "datekeyPh",
            "dateFormat": "yyyyMMdd",
            "dataType": "DATE",
            "type": "placeholder",
            "key": "datekeyPh",
            "impactPage": "ALL_PAGE",
            "filterList": [
                {"key": None, "value": [], "operator": "DURING", "dataType": None, "range": [-14, -1], "period": None, "timeType": "DYNAMIC", "role": None, "decimalDigits": None}
            ],
            "filterRule": "({0})",
            "__for": "outter",
            "sourceType": "component",
        }
    ],
    "filter": {"logicalOperator": "AND", "conditions": []},
    "fieldFilter": [],
}


class ExtraMetricsService:
    def __init__(self, settings: ExtraSettings) -> None:
        self.settings = settings
        self.fenxi_base = str(settings.fenxi_base or "").rstrip("/")
        self.manage_base = str(settings.manage_base or "").rstrip("/")
        if not self.fenxi_base:
            self.fenxi_base = "https://<FENXI_HOST>"
        if not self.manage_base:
            self.manage_base = "http://<MANAGE_HOST>"
        self.debug_log = DebugLogStore(settings.query_debug_log_path)
        self._shared_clients: dict[str, httpx.AsyncClient] = {}
        self._reuse_clients = False

    async def _record_request(self, request: httpx.Request) -> None:
        metrics = current_metrics()
        if metrics is not None:
            metrics.increment("requests")
            source = "fenxi" if request.url.host == urlsplit(self.fenxi_base).hostname else "505"
            metrics.increment(f"requests_{source}")

    def _client(self) -> httpx.AsyncClient:
        metrics = current_metrics()
        if metrics is not None:
            metrics.increment("http_clients_created")
        return RetryingAsyncClient(
            timeout=self.settings.request_timeout,
            follow_redirects=True,
            proxy=self.settings.query_proxy_url or None,
            trust_env=False,
            event_hooks={"request": [self._record_request]},
        )

    def enable_client_reuse(self) -> None:
        self._reuse_clients = True

    async def aclose(self) -> None:
        clients = list(self._shared_clients.values())
        self._shared_clients.clear()
        for client in clients:
            await client.aclose()

    @asynccontextmanager
    async def _client_scope(self, key: str):
        if self._reuse_clients:
            client = self._shared_clients.get(key)
            if client is None:
                client = self._client()
                self._shared_clients[key] = client
            yield client
            return
        async with self._client() as client:
            yield client

    async def fetch(
        self,
        query_date: date,
        fenxi_auth: dict[str, Any] | None,
        manage_auth: dict[str, Any] | None,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {"notes": {}, "top_games": [], "warnings": [], "payment_tables": {}}

        async def fetch_fenxi() -> tuple[dict[str, Any], str]:
            if not fenxi_auth:
                return {}, "fenxi未登录，已跳过新增/活跃/会员指标"
            try:
                fenxi_data = await self._fetch_fenxi_metrics(query_date, fenxi_auth)
                return fenxi_data, ""
            except Exception as exc:  # noqa: BLE001
                msg = f"fenxi指标拉取失败: {exc}"
                self.debug_log.write({"event": "extra_fenxi_error", "error": str(exc), "query_date": query_date.isoformat()})
                return {}, msg

        async def fetch_manage() -> tuple[dict[str, Any], str]:
            if not manage_auth:
                return {}, "505未登录，已跳过充值明细"
            try:
                manage_data = await self._fetch_manage_metrics(query_date, manage_auth)
                return manage_data, ""
            except Exception as exc:  # noqa: BLE001
                msg = f"manage充值指标拉取失败: {exc}"
                self.debug_log.write({"event": "extra_manage_error", "error": str(exc), "query_date": query_date.isoformat()})
                return {}, msg

        (fenxi_data, fenxi_warning), (manage_data, manage_warning) = await gather_limited(
            [fetch_fenxi(), fetch_manage()],
            min(2, self.settings.max_concurrency),
        )
        out["notes"].update(fenxi_data.get("notes", {}))
        out["notes"].update(manage_data.get("notes", {}))
        out["top_games"] = fenxi_data.get("top_games", [])
        payment_tables = manage_data.get("payment_tables")
        if isinstance(payment_tables, dict):
            out["payment_tables"] = payment_tables
        out["warnings"].extend(value for value in (fenxi_warning, manage_warning) if value)

        return out

    async def preflight(
        self,
        query_date: date,
        fenxi_auth: dict[str, Any] | None,
        manage_auth: dict[str, Any] | None,
    ) -> dict[str, dict[str, Any]]:
        async def check_fenxi() -> dict[str, Any]:
            if not fenxi_auth:
                return {"ok": False, "message": "fenxi认证信息缺失"}
            try:
                auth_headers = self._auth_headers(fenxi_auth)
                async with self._client_scope("fenxi") as client:
                    self._apply_auth(client, fenxi_auth)
                    await self._fenxi_module_switch(client, auth_headers)
                return {"ok": True, "message": "fenxi登录态可用"}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"fenxi登录态不可用: {exc}"}

        async def check_manage() -> dict[str, Any]:
            if not manage_auth:
                return {"ok": False, "message": "505认证信息缺失"}
            try:
                auth_headers = self._auth_headers(manage_auth)
                hosts_map = load_hosts_map(self.settings.hosts_yaml_path)
                async with self._client_scope("manage") as client:
                    self._apply_auth(client, manage_auth)
                    await self._bootstrap_callback(client, manage_auth, hosts_map)
                    await self._manage_recharge_detail(client, hosts_map, "gz_web", query_date, auth_headers)
                return {"ok": True, "message": "505登录态可用"}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"505登录态不可用: {exc}"}

        fenxi_result, manage_result = await gather_limited(
            [check_fenxi(), check_manage()],
            min(2, self.settings.max_concurrency),
        )
        result = {"fenxi": fenxi_result, "505": manage_result}

        self.debug_log.write({"event": "extra_auth_preflight", "query_date": query_date.isoformat(), "result": result})
        return result

    async def _fetch_fenxi_metrics(self, query_date: date, auth: dict[str, Any]) -> dict[str, Any]:
        notes: dict[str, Any] = {}
        top_games: list[dict[str, Any]] = []
        auth_headers = self._auth_headers(auth)

        async with self._client_scope("fenxi") as client:
            self._apply_auth(client, auth)
            await self._fenxi_module_switch(client, auth_headers)

            active_last_day, active_last_week, new_last_day, new_last_week = await gather_limited(
                [
                    self._fenxi_event_query(client, self._build_active_payload(query_date, "LAST_PERIOD"), auth_headers),
                    self._fenxi_event_query(client, self._build_active_payload(query_date, "LAST_WEEK"), auth_headers),
                    self._fenxi_event_query(client, self._build_new_payload(query_date, "LAST_PERIOD"), auth_headers),
                    self._fenxi_event_query(client, self._build_new_payload(query_date, "LAST_WEEK"), auth_headers),
                ],
                self.settings.max_concurrency,
            )

            notes["active_users"] = self._extract_compare_metric(active_last_day, active_last_week)
            notes["new_users"] = self._extract_compare_metric(new_last_day, new_last_week)
            try:
                # Top payload format changes frequently upstream; do not block core metrics on top failure.
                top_payload = await self._fenxi_event_query(client, self._build_top_payload(query_date), auth_headers)
                top_games = self._extract_top_games(top_payload, top_n=10)
            except Exception as exc:  # noqa: BLE001
                self.debug_log.write(
                    {
                        "event": "extra_fenxi_top_error",
                        "error": str(exc),
                        "query_date": query_date.isoformat(),
                    }
                )

            offset = self._date_offset(query_date)
            pay_rate_payload = self._payload_pay_rate(offset)
            member_recharge_payload = self._payload_member_recharge(offset)
            member_daily_payload = self._payload_member_daily(offset)

            pay_rate_data, member_recharge_data, member_daily_data = await gather_limited(
                [
                    self._fenxi_render_data(client, FENXI_COMPONENT_PAY_RATE, pay_rate_payload, auth_headers),
                    self._fenxi_render_data(client, FENXI_COMPONENT_MEMBER_RECHARGE, member_recharge_payload, auth_headers),
                    self._fenxi_render_data(client, FENXI_COMPONENT_MEMBER_DAILY, member_daily_payload, auth_headers),
                ],
                self.settings.max_concurrency,
            )

            pay_rate_row = self._first_row(pay_rate_data)
            recharge_row = self._first_row(member_recharge_data)
            daily_row = self._find_date_row(member_daily_data, query_date)

            if pay_rate_row:
                notes["member_open_count"] = self._to_int(pay_rate_row.get("thvxagsslrbm"))
                notes["member_pay_rate"] = str(pay_rate_row.get("fl6dt6_kps8e") or "")

            if recharge_row:
                notes["member_recharge_amount"] = self._to_int(recharge_row.get("2quuthxeb6el"))
                notes["member_recharge_week_ratio"] = self._normalize_ratio_text(recharge_row.get("r717ar12dmx0"))

            if daily_row:
                notes["member_valid_count"] = self._to_int(daily_row.get("ngins6tydctq"))

        return {"notes": notes, "top_games": top_games}

    async def _fetch_manage_metrics(self, query_date: date, auth: dict[str, Any]) -> dict[str, Any]:
        d0 = query_date
        d7 = query_date - timedelta(days=7)
        hosts_map = load_hosts_map(self.settings.hosts_yaml_path)
        auth_headers = self._auth_headers(auth)

        async with self._client_scope("manage") as client:
            self._apply_auth(client, auth)
            await self._bootstrap_callback(client, auth, hosts_map)

            (
                (web_0, web_0_rows),
                (web_7, web_7_rows),
                (xm_0, xm_0_rows),
                (xm_7, xm_7_rows),
                (mobile_0, mobile_0_rows),
                (mobile_7, mobile_7_rows),
            ) = await gather_limited(
                [
                    self._manage_recharge_detail(client, hosts_map, "gz_web", d0, auth_headers),
                    self._manage_recharge_detail(client, hosts_map, "gz_web", d7, auth_headers),
                    self._manage_recharge_detail(client, hosts_map, "xiamen_night", d0, auth_headers),
                    self._manage_recharge_detail(client, hosts_map, "xiamen_night", d7, auth_headers),
                    self._manage_recharge_detail(client, hosts_map, "mobile_game", d0, auth_headers),
                    self._manage_recharge_detail(client, hosts_map, "mobile_game", d7, auth_headers),
                ],
                self.settings.max_concurrency,
            )

        night_0 = web_0 + xm_0
        night_7 = web_7 + xm_7
        page_today_rows = self._merge_game_rows(web_0_rows, xm_0_rows)
        page_week_rows = self._merge_game_rows(web_7_rows, xm_7_rows)
        page_compare_rows = self._build_compare_rows(page_today_rows, page_week_rows)

        mobile_today_sorted = self._sort_game_rows(mobile_0_rows, drop_zero=True)
        mobile_week_sorted = self._sort_game_rows(mobile_7_rows, drop_zero=True)

        return {
            "notes": {
                "web_night_recharge": int(round(night_0)),
                "web_night_recharge_week_delta": int(round(night_0 - night_7)),
                "mobile_recharge": int(round(mobile_0)),
                "mobile_recharge_week_delta": int(round(mobile_0 - mobile_7)),
            },
            "payment_tables": {
                "page": {
                    "title": "页游付费数据",
                    "today_date": d0.isoformat(),
                    "week_date": d7.isoformat(),
                    "rows": page_compare_rows,
                    "total_today": int(round(night_0)),
                    "total_week": int(round(night_7)),
                    "total_delta": int(round(night_0 - night_7)),
                },
                "mobile": {
                    "title": "手游付费数据",
                    "today_date": d0.isoformat(),
                    "week_date": d7.isoformat(),
                    "today_rows": mobile_today_sorted,
                    "week_rows": mobile_week_sorted,
                    "total_today": int(round(mobile_0)),
                    "total_week": int(round(mobile_7)),
                    "total_delta": int(round(mobile_0 - mobile_7)),
                },
            },
        }

    async def _fenxi_module_switch(self, client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
        url = (
            f"{self.fenxi_base}/event-analysis-server/app_auth/getModuleSwitch"
            f"?mediaId={FENXI_MEDIA_ID}&_={int(time.time() * 1000)}"
        )
        resp = await client.get(url, headers=self._fenxi_headers(referer=f"{self.fenxi_base}/analysis/", auth_headers=auth_headers))
        if resp.status_code >= 400:
            raise RuntimeError(f"fenxi getModuleSwitch failed status={resp.status_code}")

    async def _fenxi_event_query(self, client: httpx.AsyncClient, payload: dict[str, Any], auth_headers: dict[str, str]) -> dict[str, Any]:
        url = f"{self.fenxi_base}/event-analysis-server/event_analysis/query"
        resp = await client.post(
            url,
            headers=self._fenxi_headers(
                referer=f"{self.fenxi_base}/analysis/event?viewid=2688&mediaId={FENXI_MEDIA_ID}",
                auth_headers=auth_headers,
            ),
            content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"fenxi event query failed status={resp.status_code}")
        body = resp.json()
        self.debug_log.write({"event": "extra_fenxi_event_ok", "payload": {"from_datekey": payload.get("from_datekey"), "tableType": payload.get("tableType"), "compareType": (payload.get("compareParam") or {}).get("compareType")}})
        return body

    async def _fenxi_render_data(
        self,
        client: httpx.AsyncClient,
        component_id: str,
        payload: dict[str, Any],
        auth_headers: dict[str, str],
    ) -> dict[str, Any]:
        url = f"{self.fenxi_base}/event-analysis-server/bi/report/renderData?isPageInitialRender=false&componentId={component_id}"
        resp = await client.post(
            url,
            headers=self._fenxi_headers(
                referer=f"{self.fenxi_base}/analysis/BIReport?id=656&mediaId={FENXI_MEDIA_ID}",
                auth_headers=auth_headers,
            ),
            content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"fenxi renderData failed component={component_id} status={resp.status_code}")
        body = resp.json()
        self.debug_log.write({"event": "extra_fenxi_render_ok", "component": component_id, "payload_vars": payload.get("variables", [])[:2]})
        return body

    async def _bootstrap_callback(
        self,
        client: httpx.AsyncClient,
        auth: dict[str, Any],
        hosts_map: dict[str, str],
    ) -> None:
        target = str(auth.get("bootstrap_url") or "")
        if not target:
            return
        url, host_header = rewrite_url_with_hosts_map(target, hosts_map)
        headers: dict[str, str] = {"User-Agent": "Mozilla/5.0"}
        if host_header:
            headers["Host"] = host_header
        await client.get(url, headers=headers, follow_redirects=False)

    async def _manage_recharge_detail(
        self,
        client: httpx.AsyncClient,
        hosts_map: dict[str, str],
        table: str,
        query_date: date,
        auth_headers: dict[str, str],
    ) -> tuple[float, list[dict[str, Any]]]:
        date_text = query_date.isoformat()
        manage_origin = self._base_origin(self.manage_base)
        if table == "gz_web":
            endpoint = f"{self.manage_base}/pay/gz_web.php?do=gz_02_18&r={random.random()}"
            data = {"start_time": date_text, "end_time": date_text, "gameId": "1", "source": "5", "acto": ""}
        elif table == "xiamen_night":
            endpoint = f"{self.manage_base}/pay/cloudGamePay8002.php?do=cy_8002&r={random.random()}"
            data = {"start_time": date_text, "end_time": date_text, "gameId": "1", "isDetail": "1", "p_fromid": "8002", "acto": ""}
        elif table == "mobile_game":
            endpoint = f"{self.manage_base}/pay/yxhSandbox.php?do=_cgyxhs1&r={random.random()}"
            data = {"start_time": date_text, "end_time": date_text, "pay_id": "", "yc_id": "", "isDetail": "1", "p_fromid": "8005", "acto": ""}
        else:
            raise ValueError(f"unknown table: {table}")

        url, host_header = rewrite_url_with_hosts_map(endpoint, hosts_map)
        headers: dict[str, str] = {
            "Origin": manage_origin,
            "Referer": endpoint,
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0",
        }
        headers.update(auth_headers)
        headers.pop("Cookie", None)
        if host_header:
            headers["Host"] = host_header
            headers["Origin"] = manage_origin
            headers["Referer"] = endpoint

        resp = await client.post(url, data=data, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"manage table {table} failed status={resp.status_code}")
        if self._is_manage_unauthorized(resp.text, resp.headers.get_list("set-cookie")):
            raise RuntimeError("manage登录态失效，返回登录页")
        rows = self._extract_game_amount_rows(resp.text)
        amount = self._sum_game_rows(rows)
        self.debug_log.write(
            {
                "event": "extra_manage_table_ok",
                "table": table,
                "date": date_text,
                "amount": amount,
                "rows": len(rows),
            }
        )
        return amount, rows

    def _fenxi_headers(self, referer: str, auth_headers: dict[str, str]) -> dict[str, str]:
        headers = {
            "Origin": self.fenxi_base,
            "Referer": referer,
            "User-Agent": "Mozilla/5.0",
            "mediaids": FENXI_MEDIA_ID,
            "topic": FENXI_TOPIC,
            "queryid": self._query_id(),
            "Content-Type": "application/json;charset=UTF-8",
        }
        headers.update(auth_headers)
        headers.pop("Cookie", None)
        return headers

    def _build_active_payload(self, query_date: date, compare_type: str) -> dict[str, Any]:
        return self._build_event_metric_payload(
            query_date=query_date,
            compare_type=compare_type,
            event_name="play_cloudgame",
            indicator_name="玩云游戏的人数（按设备）",
            filter_list=[],
            filter_rule="",
        )

    def _build_new_payload(self, query_date: date, compare_type: str) -> dict[str, Any]:
        return self._build_event_metric_payload(
            query_date=query_date,
            compare_type=compare_type,
            event_name="app_cloudgame_start",
            indicator_name="全局_云游戏进入的人数（按设备）",
            filter_list=[
                {
                    "key": "`_is_first_day`",
                    "value": [],
                    "operator": "TRUE",
                    "time_type": "LAST",
                    "datekey": None,
                    "arrayLabelGroupByFlag": None,
                    "datatype": "boolean",
                    "showName": "是否首日访问游戏盒",
                    "value1": None,
                    "value1Name": None,
                    "valueName": None,
                }
            ],
            filter_rule="{0}",
        )

    def _build_event_metric_payload(
        self,
        query_date: date,
        compare_type: str,
        event_name: str,
        indicator_name: str,
        filter_list: list[dict[str, Any]],
        filter_rule: str,
    ) -> dict[str, Any]:
        date_key = query_date.strftime("%Y%m%d")
        compare_date = query_date - timedelta(days=1 if compare_type == "LAST_PERIOD" else 7)
        compare_key = compare_date.strftime("%Y%m%d")
        return {
            "tableType": "time",
            "from_datekey": date_key,
            "to_datekey": date_key,
            "from_datemin": 0,
            "to_datemin": 1439,
            "indicators": [
                {
                    "index": 0,
                    "filter_list": filter_list,
                    "filter_rule": filter_rule,
                    "indicator_name": indicator_name,
                    "indicator_name_editByUser": True,
                    "indicator_list": [
                        {
                            "agg": None,
                            "column": "count(distinct vid)",
                            "eventName": event_name,
                            "presetIndicator": {"expression": "{0}", "indicator_list": [{"agg": "NDV", "column": "vid", "distinct": True}]},
                            "isVirtualEvent": False,
                            "virtualEventDefine": None,
                        }
                    ],
                    "expression": "{0}",
                    "iscustom": False,
                    "showDataType": "number",
                    "labelList": [],
                    "groupList": [],
                    "diffGroup": False,
                    "dimensionTableList": [],
                }
            ],
            "timeUnit": "datekey",
            "group_fields": [],
            "group_fields_detail": [],
            "groupChineseName": {"indicator_value_0": indicator_name},
            "globalFilter": [],
            "globalFilterRule": "",
            "compareParam": {
                "from_datekey": compare_key,
                "to_datekey": compare_key,
                "from_datemin": 0,
                "to_datemin": 1439,
                "compareType": compare_type,
                "compareTime": "1",
            },
            "includeChart": True,
            "chartType": "chart",
            "needCompleteTime": False,
            "dateRange": "-1_-1",
            "timeType": "DYNAMIC",
            "order": {"column": "datekey", "monotonicity": "desc"},
            "abtestGroupBy": {"expId": None},
            "bigDataRoute": {"filterRoute": None, "groupByRoute": None},
            "pageSize": 24,
            "pageNum": 1,
        }

    def _build_top_payload(self, query_date: date) -> dict[str, Any]:
        date_key = query_date.strftime("%Y%m%d")
        return {
            "tableType": "dimension",
            "from_datekey": date_key,
            "to_datekey": date_key,
            "from_datemin": 0,
            "to_datemin": 1439,
            "indicators": [
                {
                    "index": 0,
                    "filter_list": [],
                    "filter_rule": "",
                    "indicator_name": "玩云游戏的人数（按设备）",
                    "indicator_name_editByUser": True,
                    "indicator_list": [
                        {
                            "agg": None,
                            "column": "count(distinct vid)",
                            "eventName": "play_cloudgame",
                            "presetIndicator": {"expression": "{0}", "indicator_list": [{"agg": "NDV", "column": "vid", "distinct": True}]},
                            "isVirtualEvent": False,
                            "virtualEventDefine": None,
                        }
                    ],
                    "expression": "{0}",
                    "iscustom": False,
                    "showDataType": "number",
                    "labelList": [],
                    "groupList": [],
                    "diffGroup": False,
                    "dimensionTableList": [],
                    "orderBy": {"column": "indicator_value_0", "monotonicity": "desc"},
                }
            ],
            "timeUnit": "datekey",
            "group_fields": ["`game_id`"],
            "group_fields_detail": [
                {
                    "key": "`game_id`",
                    "time_type": "LAST",
                    "showName": "游戏ID",
                    "bucketType": "DISCRETE",
                    "field_name": "`game_id`",
                }
            ],
            "groupChineseName": {"indicator_value_0": "玩云游戏的人数（按设备）", "`game_id`": "游戏ID"},
            "globalFilter": [],
            "globalFilterRule": "",
            "compareParam": None,
            "includeChart": True,
            "chartType": "chart",
            "needCompleteTime": False,
            "dateRange": "-1_-1",
            "timeType": "DYNAMIC",
            "order": {"column": "indicator_value_0", "monotonicity": "desc"},
            "abtestGroupBy": {"expId": None},
            "bigDataRoute": {"filterRoute": None, "groupByRoute": None},
            "pageSize": 24,
            "pageNum": 1,
        }

    def _payload_pay_rate(self, offset: int) -> dict[str, Any]:
        payload = deepcopy(BI_PAYLOAD_PAY_RATE)
        payload["variables"][0]["filterList"][0] = {
            "key": None,
            "value": [],
            "operator": "DURING",
            "dataType": None,
            "range": [offset, offset],
            "period": None,
            "timeType": "DYNAMIC",
            "role": None,
            "decimalDigits": None,
            "dateLimitTip": None,
            "error": "",
        }
        return payload

    def _payload_member_recharge(self, offset: int) -> dict[str, Any]:
        payload = deepcopy(BI_PAYLOAD_MEMBER_RECHARGE)
        payload["variables"][0]["filterList"][0]["range"] = [offset, offset]
        payload["variables"][1]["filterList"][0]["range"] = [offset - 7, offset - 7]
        return payload

    def _payload_member_daily(self, offset: int) -> dict[str, Any]:
        payload = deepcopy(BI_PAYLOAD_MEMBER_DAILY)
        payload["variables"][0]["filterList"][0]["range"] = [offset, offset]
        return payload

    def _extract_compare_metric(self, day_payload: dict[str, Any], week_payload: dict[str, Any]) -> dict[str, Any]:
        day_row = self._first_event_row(day_payload)
        week_row = self._first_event_row(week_payload)
        value = self._to_int(day_row.get("indicator_value_0"))
        day_ratio = str((day_row.get("compareKey") or {}).get("indicator_value_0_ratio") or "")
        week_ratio = str((week_row.get("compareKey") or {}).get("indicator_value_0_ratio") or "")
        return {"value": value, "day_ratio": day_ratio, "week_ratio": week_ratio}

    def _extract_top_games(self, payload: dict[str, Any], top_n: int = 10) -> list[dict[str, Any]]:
        rows = (((payload.get("data") or {}).get("table") or {}).get("records") or [])
        merged: dict[str, int] = {}
        for row in rows:
            label = str(row.get("`game_id`_label") or "")
            raw_name = self._parse_game_name(label) if label else str(row.get("`game_id`") or "")
            name = self._normalize_game_name(raw_name)
            if not name:
                continue
            merged[name] = merged.get(name, 0) + self._to_int(row.get("indicator_value_0"))
        sorted_items = sorted(merged.items(), key=lambda item: (-int(item[1]), str(item[0])))
        out: list[dict[str, Any]] = []
        for name, active_users in sorted_items[:top_n]:
            out.append({"name": name, "active_users": int(active_users)})
        return out

    def _first_event_row(self, payload: dict[str, Any]) -> dict[str, Any]:
        rows = (((payload.get("data") or {}).get("table") or {}).get("records") or [])
        return rows[0] if rows else {}

    def _first_row(self, payload: dict[str, Any]) -> dict[str, Any]:
        rows = (((payload.get("data") or {}).get("data") or []))
        if rows and isinstance(rows[0], dict):
            return rows[0]
        return {}

    def _find_date_row(self, payload: dict[str, Any], query_date: date) -> dict[str, Any]:
        rows = (((payload.get("data") or {}).get("data") or []))
        key = query_date.strftime("%Y%m%d")
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("yewst5mvg2xk") or "") == key:
                return row
        return self._first_row(payload)

    def _extract_game_amount_rows(self, html: str) -> list[dict[str, Any]]:
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.IGNORECASE | re.DOTALL)
        game_col: int | None = None
        amount_col: int | None = None
        merged: dict[str, float] = {}
        order: list[str] = []
        for row_html in rows:
            headers = self._extract_cells(row_html, "th")
            if headers:
                if game_col is None:
                    for idx, header in enumerate(headers):
                        if "游戏名" in header:
                            game_col = idx
                            break
                if amount_col is None:
                    for idx, header in enumerate(headers):
                        if "充值金额" in header:
                            amount_col = idx
                            break
                continue

            cells = self._extract_cells(row_html, "td")
            if not cells:
                continue
            if any("合计" in c for c in cells):
                continue
            if game_col is None or amount_col is None:
                continue
            if game_col >= len(cells) or amount_col >= len(cells):
                continue
            game_name = str(cells[game_col] or "").strip()
            if not game_name:
                continue
            amount = self._to_float(cells[amount_col])
            if amount is None:
                continue
            if game_name not in merged:
                merged[game_name] = 0.0
                order.append(game_name)
            merged[game_name] += amount

        out: list[dict[str, Any]] = []
        for game_name in order:
            out.append({"game": game_name, "amount": float(merged.get(game_name, 0.0))})
        return out

    def _sum_game_rows(self, rows: list[dict[str, Any]]) -> float:
        total = 0.0
        for row in rows:
            total += float(row.get("amount") or 0.0)
        return total

    def _merge_game_rows(self, first: list[dict[str, Any]], second: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: dict[str, float] = {}
        order: list[str] = []
        for source in (first, second):
            for row in source:
                game = str(row.get("game") or "").strip()
                if not game:
                    continue
                amount = float(row.get("amount") or 0.0)
                if game not in merged:
                    merged[game] = 0.0
                    order.append(game)
                merged[game] += amount
        return [{"game": game, "amount": float(merged.get(game, 0.0))} for game in order]

    def _build_compare_rows(self, today_rows: list[dict[str, Any]], week_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        today_map = {str(row.get("game")): int(round(float(row.get("amount") or 0.0))) for row in today_rows}
        week_map = {str(row.get("game")): int(round(float(row.get("amount") or 0.0))) for row in week_rows}
        order: list[str] = []
        for row in today_rows:
            game = str(row.get("game") or "")
            if game and game not in order:
                order.append(game)
        for row in week_rows:
            game = str(row.get("game") or "")
            if game and game not in order:
                order.append(game)
        out: list[dict[str, Any]] = []
        for game in order:
            today_val = int(today_map.get(game, 0))
            week_val = int(week_map.get(game, 0))
            out.append({"game": game, "today": today_val, "week": week_val, "delta": today_val - week_val})
        return out

    def _sort_game_rows(self, rows: list[dict[str, Any]], drop_zero: bool = False) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for row in rows:
            game = str(row.get("game") or "").strip()
            if not game:
                continue
            amount = int(round(float(row.get("amount") or 0.0)))
            if drop_zero and amount <= 0:
                continue
            items.append({"game": game, "amount": amount})
        items.sort(key=lambda x: (-int(x["amount"]), str(x["game"])))
        return items

    def _is_manage_unauthorized(self, html: str, set_cookies: list[str]) -> bool:
        cookie_text = ";".join(set_cookies).lower()
        if "__manage_uid=deleted" in cookie_text or "__manage_user=deleted" in cookie_text:
            return True
        lowered = html.lower()
        if "oauth/index?clientid=manage505" in lowered:
            return True
        if "/oauth.php" in lowered and "redirecturl=" in lowered:
            return True
        return False

    def _base_origin(self, base_url: str) -> str:
        parsed = urlsplit(base_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        return base_url

    def _extract_cells(self, row_html: str, tag: str) -> list[str]:
        matches = re.findall(fr"<{tag}[^>]*>(.*?)</{tag}>", row_html, flags=re.IGNORECASE | re.DOTALL)
        out: list[str] = []
        for raw in matches:
            text = re.sub(r"<[^>]+>", "", raw)
            text = re.sub(r"&nbsp;?", " ", text)
            text = text.strip()
            if text:
                out.append(text)
        return out

    def _parse_game_name(self, label: str) -> str:
        if "(" in label and ")" in label:
            return label.split("(", 1)[1].rsplit(")", 1)[0]
        return label

    def _normalize_game_name(self, name: str) -> str:
        text = str(name or "").strip()
        if not text:
            return ""
        # Remove bracketed channel/platform info, e.g. "(官服)", "(电脑页游)".
        text = re.sub(r"[（(][^）)]*[）)]", "", text)
        # Remove common suffixes, e.g. "-4.0版本", "－2.6版本", "-云游戏".
        text = re.sub(r"[-－]\s*\d+(?:\.\d+)*\s*版本.*$", "", text)
        text = re.sub(r"[-－]\s*云游戏.*$", "", text)
        # For campaign/tag suffixes, keep the base game name before '-' (e.g. 第五人格-xxx -> 第五人格).
        text = re.split(r"[-－]", text, maxsplit=1)[0]
        # Collapse extra separators/spaces.
        text = re.sub(r"\s+", "", text)
        text = re.sub(r"[-－]+$", "", text)
        return text.strip()

    def _apply_auth(self, client: httpx.AsyncClient, auth: dict[str, Any]) -> None:
        cookies = dict(auth.get("cookies", {}))
        if cookies:
            client.cookies.update(cookies)

    def _date_offset(self, query_date: date) -> int:
        now_local = datetime.now(get_tzinfo(self.settings.timezone)).date()
        return (query_date - now_local).days

    def _query_id(self) -> str:
        return f"{int(time.time() * 1000):x}{random.randint(10, 99)}"

    def _auth_headers(self, auth: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        raw = auth.get("headers")
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(k, str) and isinstance(v, str):
                    out[k] = v
        return out

    def _normalize_ratio_text(self, raw_value: Any) -> str:
        raw = str(raw_value or "").strip().replace("％", "%")
        if not raw:
            return ""
        if raw.upper() == "N/A":
            return "N/A"
        if raw.endswith("%"):
            numeric = raw[:-1].replace(",", "").strip()
            try:
                value = float(numeric)
            except ValueError:
                return raw
            if abs(value) < 0.005:
                value = 0.0
            prefix = "+" if value > 0 else ""
            return f"{prefix}{value:.2f}%"
        return raw

    def _to_int(self, value: Any) -> int:
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            raw = value.replace(",", "").replace("%", "").strip()
            if not raw:
                return 0
            try:
                return int(float(raw))
            except ValueError:
                return 0
        return 0

    def _to_float(self, value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            raw = value.replace(",", "").replace("%", "").strip()
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                return None
        return None
