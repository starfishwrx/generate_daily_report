from __future__ import annotations

import copy
import json
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from network_hosts import load_hosts_map, rewrite_url_with_hosts_map

FENXI_MEDIA_ID = "media-eb40cb50d15a49a9"
FENXI_TOPIC = "gamebox_event"

PC_MEMBER_COMPONENT_TOTAL = "7ff9d130_a300_11ee_ba66_ddad220ce416"
PC_MEMBER_COMPONENT_FIRST = "08cc7dc0_a304_11ee_ba66_ddad220ce416"
PC_MEMBER_COMPONENT_TREND = "85fa2360_a309_11ee_ba66_ddad220ce416"
PC_MEMBER_COMPONENT_TOTAL_V2 = "1f4be1f0_4a06_11ee_91f8_bbf1e2ea3cec"
PC_MEMBER_COMPONENT_COUNT_V2 = "97479700_4a04_11ee_91f8_bbf1e2ea3cec"

PC_MEMBER_PAYLOAD_TOTAL: dict[str, Any] = {
    "reportId": 793,
    "componentId": PC_MEMBER_COMPONENT_TOTAL,
    "modelId": 666,
    "fieldList": [
        {"showname": "datekey", "nullDisplay": "-", "describe": "", "fieldId": "g0dxf_e30fj1", "uniqueId": "g0dxf_e30fj1", "dateCompare": None, "rawFieldId": "g0dxf_e30fj1", "role": "dimension"},
        {"showname": "总金额", "nullDisplay": "null", "describe": "", "fieldId": "d0kojttvygqw", "uniqueId": "d0kojttvygqw", "dateCompare": None, "rawFieldId": "d0kojttvygqw", "role": "measure"},
        {"showname": "总订单数", "nullDisplay": "null", "describe": "", "fieldId": "4r8uh91ns_sx", "uniqueId": "4r8uh91ns_sx", "dateCompare": None, "rawFieldId": "4r8uh91ns_sx", "role": "measure"},
    ],
    "orderBy": [],
    "aggFunction": [],
    "showDataFormat": [
        {"fieldId": "d0kojttvygqw", "uniqueId": "d0kojttvygqw", "isThousand": True, "isPercent": False, "decimalDigits": 0},
        {"fieldId": "4r8uh91ns_sx", "uniqueId": "4r8uh91ns_sx", "isThousand": True, "isPercent": False, "decimalDigits": 0},
    ],
    "showDateFormat": [],
    "componentType": "normal-chart-line",
    "pageSize": 1000,
    "page": 1,
    "isCheckBigTable": False,
    "componentTitle": "每日各类型订单总金额、订单数",
    "variables": [{"sourceType": "component"}],
    "filter": {"logicalOperator": "AND", "conditions": []},
    "fieldFilter": [
        {
            "fieldId": "g0dxf_e30fj1",
            "dataType": "DATE",
            "dateFormat": "yyyyMMdd",
            "filterList": [
                {
                    "key": None,
                    "value": [],
                    "operator": "DURING",
                    "dataType": None,
                    "range": [-13, 0],
                    "period": None,
                    "timeType": "DYNAMIC",
                    "role": None,
                    "decimalDigits": None,
                }
            ],
            "filterRule": "({0})",
            "__for": "outter",
        }
    ],
    "legendFieldIds": [],
}

PC_MEMBER_PAYLOAD_FIRST: dict[str, Any] = {
    "reportId": 793,
    "componentId": PC_MEMBER_COMPONENT_FIRST,
    "modelId": 667,
    "fieldList": [
        {"showname": "datekey", "nullDisplay": "-", "describe": "", "fieldId": "chciprojlqw0", "uniqueId": "chciprojlqw0", "dateCompare": None, "rawFieldId": "chciprojlqw0", "role": "dimension"},
        {"showname": "首开会员总金额", "nullDisplay": "null", "describe": "", "fieldId": "v7qf1aulb414", "uniqueId": "v7qf1aulb414", "dateCompare": None, "rawFieldId": "v7qf1aulb414", "role": "measure"},
        {"showname": "首开会员数", "nullDisplay": "null", "describe": "", "fieldId": "g8vcu9mh4r56", "uniqueId": "g8vcu9mh4r56", "dateCompare": None, "rawFieldId": "g8vcu9mh4r56", "role": "measure"},
    ],
    "orderBy": [],
    "aggFunction": [],
    "showDataFormat": [
        {"fieldId": "v7qf1aulb414", "uniqueId": "v7qf1aulb414", "isThousand": True, "isPercent": False, "decimalDigits": 0},
        {"fieldId": "g8vcu9mh4r56", "uniqueId": "g8vcu9mh4r56", "isThousand": True, "isPercent": False, "decimalDigits": 0},
    ],
    "showDateFormat": [],
    "componentType": "normal-chart-line",
    "pageSize": 1000,
    "page": 1,
    "isCheckBigTable": False,
    "componentTitle": "首开会员数",
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
                    "range": [-13, 0],
                    "period": None,
                    "timeType": "DYNAMIC",
                    "role": None,
                    "decimalDigits": None,
                }
            ],
            "filterRule": "({0})",
            "__for": "outter",
            "sourceType": "component",
        }
    ],
    "filter": {"logicalOperator": "AND", "conditions": []},
    "fieldFilter": [],
    "legendFieldIds": [],
}

PC_MEMBER_PAYLOAD_TREND: dict[str, Any] = {
    "reportId": 793,
    "componentId": PC_MEMBER_COMPONENT_TREND,
    "modelId": 668,
    "fieldList": [
        {"showname": "当前充值金额", "nullDisplay": "-", "describe": "", "fieldId": "2a9u57i279ba", "uniqueId": "2a9u57i279ba", "dateCompare": None, "rawFieldId": "2a9u57i279ba", "role": "measure"},
        {"showname": "对比充值金额", "nullDisplay": "-", "describe": "", "fieldId": "6zlb6zfwezv0", "uniqueId": "6zlb6zfwezv0", "dateCompare": None, "rawFieldId": "6zlb6zfwezv0", "role": "measure"},
        {"showname": "涨幅", "nullDisplay": "-", "describe": "", "fieldId": "avq_ehkl4hqy", "uniqueId": "avq_ehkl4hqy", "dateCompare": None, "rawFieldId": "avq_ehkl4hqy", "role": "measure"},
    ],
    "orderBy": [],
    "page": 1,
    "pageSize": 100,
    "aggFunction": [
        {"agg": "SUM", "fieldId": "2a9u57i279ba", "uniqueId": "2a9u57i279ba"},
        {"agg": "SUM", "fieldId": "6zlb6zfwezv0", "uniqueId": "6zlb6zfwezv0"},
    ],
    "showDataFormat": [
        {"fieldId": "2a9u57i279ba", "uniqueId": "2a9u57i279ba", "isThousand": True, "isPercent": False, "decimalDigits": 0},
        {"fieldId": "6zlb6zfwezv0", "uniqueId": "6zlb6zfwezv0", "isThousand": True, "isPercent": False, "decimalDigits": 0},
        {"fieldId": "avq_ehkl4hqy", "uniqueId": "avq_ehkl4hqy", "isThousand": True, "isPercent": True, "decimalDigits": 2},
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
                {
                    "key": None,
                    "value": [],
                    "operator": "DURING",
                    "dataType": None,
                    "range": [-1, -1],
                    "period": None,
                    "timeType": "DYNAMIC",
                    "role": None,
                    "decimalDigits": None,
                }
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
                {
                    "key": None,
                    "value": [],
                    "operator": "DURING",
                    "dataType": None,
                    "range": [-8, -8],
                    "period": None,
                    "timeType": "DYNAMIC",
                    "role": None,
                    "decimalDigits": None,
                }
            ],
            "filterRule": "({0})",
            "__for": "inner",
            "sourceType": "component",
        },
    ],
    "filter": {"logicalOperator": "AND", "conditions": []},
    "fieldFilter": [],
}

PC_MEMBER_PAYLOAD_TOTAL_V2: dict[str, Any] = {
    "reportId": 656,
    "componentId": PC_MEMBER_COMPONENT_TOTAL_V2,
    "modelId": 316,
    "fieldList": [
        {"showname": "日期", "nullDisplay": "-", "describe": "", "fieldId": "zjpfdm75018i", "uniqueId": "zjpfdm75018i", "dateCompare": None, "rawFieldId": "zjpfdm75018i", "role": "dimension"},
        {"showname": "总金额", "nullDisplay": "null", "describe": "", "fieldId": "bq2uvv3owhlk", "uniqueId": "bq2uvv3owhlk", "dateCompare": None, "rawFieldId": "bq2uvv3owhlk", "role": "measure"},
        {"showname": "总订单数", "nullDisplay": "null", "describe": "", "fieldId": "g41xqw2bbrw9", "uniqueId": "g41xqw2bbrw9", "dateCompare": None, "rawFieldId": "g41xqw2bbrw9", "role": "measure"},
    ],
    "orderBy": [],
    "aggFunction": [
        {"agg": "SUM", "fieldId": "bq2uvv3owhlk", "uniqueId": "bq2uvv3owhlk"},
        {"agg": "SUM", "fieldId": "g41xqw2bbrw9", "uniqueId": "g41xqw2bbrw9"},
    ],
    "showDataFormat": [
        {"fieldId": "bq2uvv3owhlk", "uniqueId": "bq2uvv3owhlk", "isThousand": True, "isPercent": False, "decimalDigits": 2},
        {"fieldId": "g41xqw2bbrw9", "uniqueId": "g41xqw2bbrw9", "isThousand": True, "isPercent": False, "decimalDigits": 0},
    ],
    "showDateFormat": [{"dateFormat": "yyyyMMdd", "fieldId": "zjpfdm75018i", "fillDate": True, "uniqueId": "zjpfdm75018i"}],
    "componentType": "normal-chart-line",
    "pageSize": 1000,
    "page": 1,
    "isCheckBigTable": False,
    "componentTitle": "49-每日各类型云游戏会员订单金额、订单数",
    "variables": [{"sourceType": "component"}, {"sourceType": "component"}],
    "filter": {"logicalOperator": "AND", "conditions": []},
    "fieldFilter": [
        {"fieldId": "lfczy9e3rzpd", "dataType": "STRING", "dateFormat": None, "filterList": [{"operator": "NO_EQUAL", "value": ["总"]}], "filterRule": "{0}", "legal": True, "__for": "dataFilter"},
        {
            "fieldId": "zjpfdm75018i",
            "dataType": "DATE",
            "dateFormat": "yyyyMMdd",
            "filterList": [{"key": None, "value": [], "operator": "DURING", "dataType": None, "range": [-14, -1], "period": None, "timeType": "DYNAMIC", "role": None, "decimalDigits": None}],
            "filterRule": "({0})",
            "__for": "outter",
        },
        {
            "fieldId": "lfczy9e3rzpd",
            "dataType": "STRING",
            "filterList": [{"key": None, "value": ["月卡"], "operator": "EQUALS", "dataType": None, "range": None, "period": None, "timeType": None, "role": None, "decimalDigits": None}],
            "filterRule": "({0})",
            "__for": "inner",
        },
    ],
    "legendFieldIds": [],
}

PC_MEMBER_PAYLOAD_COUNT_V2: dict[str, Any] = {
    "reportId": 656,
    "componentId": PC_MEMBER_COMPONENT_COUNT_V2,
    "modelId": 315,
    "fieldList": [
        {"showname": "日期", "nullDisplay": "-", "describe": "", "fieldId": "fxlo87pe_bxk", "uniqueId": "fxlo87pe_bxk", "dateCompare": None, "rawFieldId": "fxlo87pe_bxk", "role": "dimension"},
        {"showname": "49云游戏会员数", "nullDisplay": "null", "describe": "", "fieldId": "_kcr7vzxji88", "uniqueId": "_kcr7vzxji88{1}", "dateCompare": None, "rawFieldId": "_kcr7vzxji88", "role": "measure"},
        {"showname": "首开49云游戏会员数", "nullDisplay": "null", "describe": "", "fieldId": "z_ftqnrrb2si", "uniqueId": "z_ftqnrrb2si{1}", "dateCompare": None, "rawFieldId": "z_ftqnrrb2si", "role": "measure"},
    ],
    "orderBy": [],
    "aggFunction": [
        {"agg": "SUM", "fieldId": "_kcr7vzxji88", "uniqueId": "_kcr7vzxji88{1}"},
        {"agg": "SUM", "fieldId": "z_ftqnrrb2si", "uniqueId": "z_ftqnrrb2si{1}"},
    ],
    "showDataFormat": [
        {"fieldId": "_kcr7vzxji88", "uniqueId": "_kcr7vzxji88{1}", "isThousand": True, "isPercent": False, "decimalDigits": 0},
        {"fieldId": "z_ftqnrrb2si", "uniqueId": "z_ftqnrrb2si{1}", "isThousand": True, "isPercent": False, "decimalDigits": 0},
    ],
    "showDateFormat": [{"dateFormat": "yyyyMMdd", "fieldId": "fxlo87pe_bxk", "fillDate": True, "uniqueId": "fxlo87pe_bxk"}],
    "componentType": "normal-chart-line",
    "pageSize": 1000,
    "page": 1,
    "isCheckBigTable": False,
    "componentTitle": "有效期内49云游戏会员数、首开49云游戏会员数",
    "variables": [
        {
            "showName": "datekeyPh",
            "dateFormat": "yyyyMMdd",
            "dataType": "DATE",
            "type": "placeholder",
            "key": "datekeyPh",
            "impactPage": "ALL_PAGE",
            "filterList": [{"key": None, "value": [], "operator": "DURING", "dataType": None, "range": [-14, -1], "period": None, "timeType": "DYNAMIC", "role": None, "decimalDigits": None}],
            "filterRule": "({0})",
            "__for": "outter",
            "sourceType": "component",
        }
    ],
    "filter": {"logicalOperator": "AND", "conditions": []},
    "fieldFilter": [],
    "legendFieldIds": [],
}


@dataclass(frozen=True)
class PCWebSettings:
    base_url: str
    web_origin: str
    request_timeout: int
    query_proxy_url: str
    hosts_yaml_path: str
    fenxi_base: str = "https://fenxi.4399dev.com"
    timezone: str = "Asia/Shanghai"


class PCWebMetricsService:
    def __init__(self, settings: PCWebSettings) -> None:
        self.settings = settings
        self.base_url = str(settings.base_url or "").rstrip("/")
        if not self.base_url:
            self.base_url = "http://yapiadmin.4399.com"
        self.web_origin = str(settings.web_origin or "").rstrip("/")
        if not self.web_origin:
            self.web_origin = "http://yadmin.4399.com"
        self.fenxi_base = str(settings.fenxi_base or "").rstrip("/")
        if not self.fenxi_base:
            self.fenxi_base = "https://fenxi.4399dev.com"

    async def preflight(self, query_date: date, auth: dict[str, Any] | None) -> dict[str, Any]:
        if not auth:
            return {"ok": False, "message": "PC网页端认证信息缺失"}
        try:
            payload = await self._query_game_start_data(
                query_date=query_date,
                start_date=query_date,
                auth=auth,
            )
            self._extract_metrics_from_payload(payload, query_date, top_n=1)
            return {"ok": True, "message": "PC网页端登录态可用"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"PC网页端登录态不可用: {exc}"}

    async def fetch(self, query_date: date, auth: dict[str, Any] | None, top_n: int = 10) -> dict[str, Any]:
        if not auth:
            raise RuntimeError("PC网页端认证信息缺失")
        start_date = query_date - timedelta(days=7)
        payload = await self._query_game_start_data(query_date=query_date, start_date=start_date, auth=auth)
        return self._extract_metrics_from_payload(payload, query_date, top_n=top_n)

    async def preflight_member(self, query_date: date, fenxi_auth: dict[str, Any] | None) -> dict[str, Any]:
        if not fenxi_auth:
            return {"ok": False, "message": "PC会员认证信息缺失（fenxi）"}
        try:
            _ = await self.fetch_member_metrics(query_date=query_date, fenxi_auth=fenxi_auth)
            return {"ok": True, "message": "PC会员登录态可用"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"PC会员登录态不可用: {exc}"}

    async def fetch_member_metrics(self, query_date: date, fenxi_auth: dict[str, Any] | None) -> dict[str, Any]:
        if not fenxi_auth:
            raise RuntimeError("PC会员认证信息缺失（fenxi）")
        offset = self._date_offset(query_date)
        payload_total = self._payload_pc_member_total(offset)
        payload_first = self._payload_pc_member_first(offset)
        payload_trend = self._payload_pc_member_trend(offset)
        payload_total_today_single = self._payload_pc_member_total_single(offset)
        payload_total_week_single = self._payload_pc_member_total_single(offset - 7)
        auth_headers = self._auth_headers(fenxi_auth)

        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout,
            follow_redirects=True,
            proxy=self.settings.query_proxy_url or None,
            trust_env=False,
        ) as client:
            self._apply_auth(client, fenxi_auth)
            await self._fenxi_module_switch(client, auth_headers)
            total_data = await self._fenxi_render_data(client, PC_MEMBER_COMPONENT_TOTAL, payload_total, auth_headers)
            first_data = await self._fenxi_render_data(client, PC_MEMBER_COMPONENT_FIRST, payload_first, auth_headers)
            trend_data = await self._fenxi_render_data(client, PC_MEMBER_COMPONENT_TREND, payload_trend, auth_headers)
            total_today_single = await self._fenxi_render_data(client, PC_MEMBER_COMPONENT_TOTAL, payload_total_today_single, auth_headers)
            total_week_single = await self._fenxi_render_data(client, PC_MEMBER_COMPONENT_TOTAL, payload_total_week_single, auth_headers)

        notes = self._extract_member_notes(
            total_data=total_data,
            first_data=first_data,
            query_date=query_date,
            trend_summary_override=self._extract_trend_summary(trend_data),
            total_amount_today_override=self._extract_single_value(
                total_today_single,
                "d0kojttvygqw",
                date_field="g0dxf_e30fj1",
                target_day_key=query_date.strftime("%Y%m%d"),
            ),
            total_amount_prev_week_override=self._extract_single_value(
                total_week_single,
                "d0kojttvygqw",
                date_field="g0dxf_e30fj1",
                target_day_key=(query_date - timedelta(days=7)).strftime("%Y%m%d"),
            ),
        )
        return {"notes": notes}

    async def _query_game_start_data(
        self,
        query_date: date,
        start_date: date,
        auth: dict[str, Any],
    ) -> dict[str, Any]:
        hosts_map = load_hosts_map(self.settings.hosts_yaml_path)
        endpoint = f"{self.base_url}/?m=gameData&ac=gameStartData"
        url, host_header = rewrite_url_with_hosts_map(endpoint, hosts_map)
        headers = self._build_headers(endpoint, auth, host_header)
        data = {
            "time_start": start_date.isoformat(),
            "time_end": query_date.isoformat(),
            "gameids": "",
        }

        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout,
            follow_redirects=True,
            proxy=self.settings.query_proxy_url or None,
            trust_env=False,
        ) as client:
            self._apply_auth(client, auth)
            resp = await client.post(url, data=data, headers=headers)

        if resp.status_code >= 400:
            raise RuntimeError(f"PC网页端接口请求失败 status={resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            snippet = resp.text[:160].replace("\n", " ").strip()
            raise RuntimeError(f"PC网页端返回非JSON，可能未登录: {snippet}") from exc
        if not isinstance(body, dict):
            raise RuntimeError("PC网页端接口返回结构异常")
        success = bool(body.get("success"))
        status_code = int(body.get("status") or 0)
        if (not success) or status_code != 100:
            msg = str(body.get("msg") or "请求失败")
            raise RuntimeError(self._format_pc_web_failure_message(status_code, msg, auth))
        data_block = body.get("data")
        if not isinstance(data_block, dict):
            raise RuntimeError("PC网页端接口缺少data字段")
        return body

    def _extract_metrics_from_payload(self, payload: dict[str, Any], query_date: date, top_n: int = 10) -> dict[str, Any]:
        data_block = payload.get("data")
        if not isinstance(data_block, dict):
            raise RuntimeError("PC网页端接口data字段无效")

        day_map = data_block.get("list")
        if not isinstance(day_map, dict) or not day_map:
            raise RuntimeError("PC网页端接口缺少按日明细")

        target_key = query_date.isoformat()
        today_bucket = self._select_day_bucket(day_map, target_key)
        if today_bucket is None:
            raise RuntimeError(f"PC网页端缺少目标日期数据: {target_key}")

        d1_bucket = day_map.get((query_date - timedelta(days=1)).isoformat())
        d7_bucket = day_map.get((query_date - timedelta(days=7)).isoformat())

        today_new = self._extract_total(today_bucket, "new_num")
        d1_new = self._extract_total(d1_bucket, "new_num")
        d7_new = self._extract_total(d7_bucket, "new_num")

        today_active = self._extract_total(today_bucket, "people_num")
        d1_active = self._extract_total(d1_bucket, "people_num")
        d7_active = self._extract_total(d7_bucket, "people_num")

        top_games = self._extract_top_games(today_bucket, top_n=top_n)

        return {
            "notes": {
                "new_users": {"value": today_new, "day_ratio": self._format_ratio(today_new, d1_new), "week_ratio": self._format_ratio(today_new, d7_new)},
                "active_users": {"value": today_active, "day_ratio": self._format_ratio(today_active, d1_active), "week_ratio": self._format_ratio(today_active, d7_active)},
            },
            "top_games": top_games,
            "source_range": {
                "start_date": min(day_map.keys()),
                "end_date": max(day_map.keys()),
            },
        }

    def _extract_top_games(self, day_bucket: Any, top_n: int = 10) -> list[dict[str, Any]]:
        if not isinstance(day_bucket, dict):
            return []
        people = day_bucket.get("people_num")
        if not isinstance(people, dict):
            return []
        rows = people.get("list")
        if not isinstance(rows, list):
            return []

        merged: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_name = str(row.get("title") or "").strip()
            name = self._normalize_game_name(raw_name)
            if not name:
                continue
            merged[name] = merged.get(name, 0) + self._to_int(row.get("num"))
        sorted_rows = sorted(merged.items(), key=lambda x: (-int(x[1]), str(x[0])))
        out: list[dict[str, Any]] = []
        for name, users in sorted_rows[: max(1, int(top_n))]:
            out.append({"name": name, "active_users": int(users)})
        return out

    def _extract_total(self, day_bucket: Any, key: str) -> int:
        if not isinstance(day_bucket, dict):
            return 0
        sub = day_bucket.get(key)
        if not isinstance(sub, dict):
            return 0
        return self._to_int(sub.get("total"))

    def _select_day_bucket(self, day_map: dict[str, Any], target_key: str) -> Any:
        direct = day_map.get(target_key)
        if isinstance(direct, dict):
            return direct
        valid_keys = sorted([k for k, v in day_map.items() if isinstance(k, str) and isinstance(v, dict)])
        if not valid_keys:
            return None
        fallback = valid_keys[-1]
        return day_map.get(fallback)

    def _build_headers(self, endpoint: str, auth: dict[str, Any], host_header: str | None) -> dict[str, str]:
        parsed = urlsplit(self.base_url)
        origin = self.web_origin
        if parsed.scheme and parsed.netloc and not origin:
            origin = f"{parsed.scheme}://{parsed.netloc}"

        headers: dict[str, str] = {
            "Origin": origin,
            "Referer": f"{origin}/",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0",
            "X-Requested-With": "XMLHttpRequest",
        }
        headers.update(self._auth_headers(auth))
        headers.pop("Cookie", None)
        headers.pop("cookie", None)
        if host_header:
            headers["Host"] = host_header
            headers["Referer"] = f"{self.web_origin}/"
            headers["Origin"] = self.web_origin
        if endpoint:
            headers.setdefault("Referer", f"{self.web_origin}/")
        return headers

    async def _fenxi_module_switch(self, client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
        url = (
            f"{self.fenxi_base}/event-analysis-server/app_auth/getModuleSwitch"
            f"?mediaId={FENXI_MEDIA_ID}&_={int(time.time() * 1000)}"
        )
        resp = await client.get(url, headers=self._fenxi_headers(referer=f"{self.fenxi_base}/analysis/", auth_headers=auth_headers))
        if resp.status_code >= 400:
            raise RuntimeError(f"fenxi getModuleSwitch failed status={resp.status_code}")

    async def _fenxi_render_data(
        self,
        client: httpx.AsyncClient,
        component_id: str,
        payload: dict[str, Any],
        auth_headers: dict[str, str],
    ) -> dict[str, Any]:
        url = f"{self.fenxi_base}/event-analysis-server/bi/report/renderData?isPageInitialRender=false&componentId={component_id}"
        report_id = int(payload.get("reportId") or 656)
        resp = await client.post(
            url,
            headers=self._fenxi_headers(
                referer=f"{self.fenxi_base}/analysis/BIReport?id={report_id}&mediaId={FENXI_MEDIA_ID}",
                auth_headers=auth_headers,
            ),
            content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"fenxi renderData failed component={component_id} status={resp.status_code}")
        body = resp.json()
        code = int(body.get("code") or 0)
        if code not in (0, 1):
            msg = str(body.get("message") or "")
            raise RuntimeError(f"fenxi renderData failed component={component_id} code={code} msg={msg}")
        return body

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

    def _auth_headers(self, auth: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        raw = auth.get("headers")
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(k, str) and isinstance(v, str):
                    out[k] = v
        return out

    def _has_bearer_header(self, auth: dict[str, Any]) -> bool:
        raw = auth.get("headers")
        if not isinstance(raw, dict):
            return False
        for key, value in raw.items():
            k = str(key or "").strip().lower()
            v = str(value or "").strip()
            if not v:
                continue
            if k == "bearer":
                return True
            if k == "authorization" and v.lower().startswith("bearer "):
                return True
        return False

    def _format_pc_web_failure_message(self, status_code: int, msg: str, auth: dict[str, Any]) -> str:
        if status_code == -100 and "请先登录" in str(msg or ""):
            if not self._has_bearer_header(auth):
                return (
                    "PC网页端接口失败 status=-100, msg=请先登录（pc_web HAR 未提取到 Bearer 请求头，"
                    "请重新抓取 yadmin HAR 并刷新 extra_auth.json）"
                )
        return f"PC网页端接口失败 status={status_code}, msg={msg}"

    def _apply_auth(self, client: httpx.AsyncClient, auth: dict[str, Any]) -> None:
        cookies = auth.get("cookies")
        if isinstance(cookies, dict):
            clean = {str(k): str(v) for k, v in cookies.items() if str(k).strip() and str(v).strip()}
            if clean:
                client.cookies.update(clean)

    def _payload_pc_member_total(self, offset: int) -> dict[str, Any]:
        payload = copy.deepcopy(PC_MEMBER_PAYLOAD_TOTAL)
        self._shift_dynamic_ranges(payload, offset)
        return payload

    def _payload_pc_member_first(self, offset: int) -> dict[str, Any]:
        payload = copy.deepcopy(PC_MEMBER_PAYLOAD_FIRST)
        self._shift_dynamic_ranges(payload, offset)
        return payload

    def _payload_pc_member_total_single(self, offset: int) -> dict[str, Any]:
        payload = copy.deepcopy(PC_MEMBER_PAYLOAD_TOTAL)
        self._shift_dynamic_ranges(payload, offset)
        field_filters = payload.get("fieldFilter")
        if isinstance(field_filters, list):
            for ff in field_filters:
                if not isinstance(ff, dict):
                    continue
                filters = ff.get("filterList")
                if isinstance(filters, list):
                    for flt in filters:
                        if isinstance(flt, dict):
                            flt["range"] = [offset, offset]
        return payload

    def _payload_pc_member_trend(self, offset: int) -> dict[str, Any]:
        payload = copy.deepcopy(PC_MEMBER_PAYLOAD_TREND)
        variables = payload.get("variables")
        if not isinstance(variables, list):
            return payload
        for item in variables:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "")
            filters = item.get("filterList")
            if not isinstance(filters, list):
                continue
            for flt in filters:
                if not isinstance(flt, dict):
                    continue
                if key == "datekeyPh1":
                    flt["range"] = [offset, offset]
                elif key == "datekeyPh2":
                    flt["range"] = [offset - 7, offset - 7]
        return payload

    def _payload_pc_member_total_v2(self, offset: int) -> dict[str, Any]:
        payload = copy.deepcopy(PC_MEMBER_PAYLOAD_TOTAL_V2)
        field_filters = payload.get("fieldFilter")
        if isinstance(field_filters, list):
            for ff in field_filters:
                if not isinstance(ff, dict):
                    continue
                if str(ff.get("fieldId") or "") != "zjpfdm75018i":
                    continue
                filters = ff.get("filterList")
                if isinstance(filters, list) and filters:
                    first = filters[0]
                    if isinstance(first, dict):
                        first["range"] = [int(offset) - 13, int(offset)]
        return payload

    def _payload_pc_member_count_v2(self, offset: int) -> dict[str, Any]:
        payload = copy.deepcopy(PC_MEMBER_PAYLOAD_COUNT_V2)
        variables = payload.get("variables")
        if isinstance(variables, list) and variables:
            first_var = variables[0]
            if isinstance(first_var, dict):
                filters = first_var.get("filterList")
                if isinstance(filters, list) and filters:
                    first = filters[0]
                    if isinstance(first, dict):
                        first["range"] = [int(offset) - 13, int(offset)]
        return payload

    def _shift_dynamic_ranges(self, payload: dict[str, Any], offset: int) -> None:
        variables = payload.get("variables")
        if isinstance(variables, list):
            for item in variables:
                if not isinstance(item, dict):
                    continue
                filters = item.get("filterList")
                if isinstance(filters, list):
                    for flt in filters:
                        if isinstance(flt, dict):
                            flt["range"] = self._offset_range(flt.get("range"), offset)

        field_filters = payload.get("fieldFilter")
        if isinstance(field_filters, list):
            for ff in field_filters:
                if not isinstance(ff, dict):
                    continue
                filters = ff.get("filterList")
                if isinstance(filters, list):
                    for flt in filters:
                        if isinstance(flt, dict):
                            flt["range"] = self._offset_range(flt.get("range"), offset)

    def _offset_range(self, raw_range: Any, offset: int) -> list[int]:
        if isinstance(raw_range, list) and len(raw_range) == 2:
            try:
                start = int(raw_range[0]) + int(offset)
                end = int(raw_range[1]) + int(offset)
                return [start, end]
            except (TypeError, ValueError):
                return [-13 + int(offset), 0 + int(offset)]
        return [-13 + int(offset), 0 + int(offset)]

    def _extract_member_notes(
        self,
        total_data: dict[str, Any],
        first_data: dict[str, Any],
        query_date: date,
        trend_summary_override: dict[str, Any] | None = None,
        total_amount_today_override: int | None = None,
        total_amount_prev_week_override: int | None = None,
    ) -> dict[str, Any]:
        date_key = query_date.strftime("%Y%m%d")
        prev_day_key = (query_date - timedelta(days=1)).strftime("%Y%m%d")
        prev_week_key = (query_date - timedelta(days=7)).strftime("%Y%m%d")

        total_series = self._extract_series_by_date(
            payload=total_data,
            date_field="g0dxf_e30fj1",
            value_fields=("d0kojttvygqw", "4r8uh91ns_sx"),
        )
        first_series = self._extract_series_by_date(
            payload=first_data,
            date_field="chciprojlqw0",
            value_fields=("v7qf1aulb414", "g8vcu9mh4r56"),
        )

        total_amount_today = self._series_value(total_series, date_key, "d0kojttvygqw")
        total_order_today = self._series_value(total_series, date_key, "4r8uh91ns_sx")
        first_amount_today = self._series_value(first_series, date_key, "v7qf1aulb414")
        first_count_today = self._series_value(first_series, date_key, "g8vcu9mh4r56")
        if total_amount_today_override is not None:
            total_amount_today = int(total_amount_today_override)
        prev_week_total_amount = self._series_value(total_series, prev_week_key, "d0kojttvygqw")
        if total_amount_prev_week_override is not None:
            prev_week_total_amount = int(total_amount_prev_week_override)
        week_ratio = self._format_ratio(total_amount_today, prev_week_total_amount)
        if isinstance(trend_summary_override, dict) and trend_summary_override:
            trend_current_amount = trend_summary_override.get("current_amount")
            trend_compare_amount = trend_summary_override.get("compare_amount")
            trend_ratio = self._normalize_ratio_text(trend_summary_override.get("ratio_text"))
            if isinstance(trend_current_amount, int):
                total_amount_today = trend_current_amount
            if isinstance(trend_compare_amount, int):
                prev_week_total_amount = trend_compare_amount
            week_ratio = trend_ratio or self._format_ratio(total_amount_today, prev_week_total_amount)

        return {
            "member_total_amount": {
                "value": total_amount_today,
                "day_ratio": self._format_ratio(total_amount_today, self._series_value(total_series, prev_day_key, "d0kojttvygqw")),
                "week_ratio": week_ratio,
            },
            "member_total_orders": {
                "value": total_order_today,
                "day_ratio": self._format_ratio(total_order_today, self._series_value(total_series, prev_day_key, "4r8uh91ns_sx")),
                "week_ratio": self._format_ratio(total_order_today, self._series_value(total_series, prev_week_key, "4r8uh91ns_sx")),
            },
            "member_first_amount": {
                "value": first_amount_today,
                "day_ratio": self._format_ratio(first_amount_today, self._series_value(first_series, prev_day_key, "v7qf1aulb414")),
                "week_ratio": self._format_ratio(first_amount_today, self._series_value(first_series, prev_week_key, "v7qf1aulb414")),
            },
            "member_first_count": {
                "value": first_count_today,
                "day_ratio": self._format_ratio(first_count_today, self._series_value(first_series, prev_day_key, "g8vcu9mh4r56")),
                "week_ratio": self._format_ratio(first_count_today, self._series_value(first_series, prev_week_key, "g8vcu9mh4r56")),
            },
            "member_summary": {
                "recharge_count": total_order_today,
                "first_count": first_count_today,
                "recharge_amount": total_amount_today,
                "recharge_amount_formatted": f"{int(total_amount_today):,}",
                "week_trend_text": self._ratio_to_trend_text(
                    week_ratio
                ),
            },
        }

    def _extract_member_notes_v2(
        self,
        total_data_v2: dict[str, Any],
        count_data_v2: dict[str, Any],
        query_date: date,
    ) -> dict[str, Any]:
        day_key = query_date.strftime("%Y%m%d")
        prev_day_key = (query_date - timedelta(days=1)).strftime("%Y%m%d")
        prev_week_key = (query_date - timedelta(days=7)).strftime("%Y%m%d")

        total_series = self._extract_series_by_date(
            payload=total_data_v2,
            date_field="zjpfdm75018i",
            value_fields=("bq2uvv3owhlk", "g41xqw2bbrw9"),
        )
        count_series = self._extract_series_by_date(
            payload=count_data_v2,
            date_field="fxlo87pe_bxk",
            value_fields=("_kcr7vzxji88", "z_ftqnrrb2si"),
        )

        total_amount_today = self._series_value(total_series, day_key, "bq2uvv3owhlk")
        total_order_today = self._series_value(total_series, day_key, "g41xqw2bbrw9")
        first_count_today = self._series_value(count_series, day_key, "z_ftqnrrb2si")
        week_ratio = self._format_ratio(total_amount_today, self._series_value(total_series, prev_week_key, "bq2uvv3owhlk"))

        return {
            "member_total_amount": {
                "value": total_amount_today,
                "day_ratio": self._format_ratio(total_amount_today, self._series_value(total_series, prev_day_key, "bq2uvv3owhlk")),
                "week_ratio": week_ratio,
            },
            "member_total_orders": {
                "value": total_order_today,
                "day_ratio": self._format_ratio(total_order_today, self._series_value(total_series, prev_day_key, "g41xqw2bbrw9")),
                "week_ratio": self._format_ratio(total_order_today, self._series_value(total_series, prev_week_key, "g41xqw2bbrw9")),
            },
            "member_first_amount": {
                "value": 0,
                "day_ratio": self._format_ratio(0, 0),
                "week_ratio": self._format_ratio(0, 0),
            },
            "member_first_count": {
                "value": first_count_today,
                "day_ratio": self._format_ratio(first_count_today, self._series_value(count_series, prev_day_key, "z_ftqnrrb2si")),
                "week_ratio": self._format_ratio(first_count_today, self._series_value(count_series, prev_week_key, "z_ftqnrrb2si")),
            },
            "member_summary": {
                "recharge_count": total_order_today,
                "first_count": first_count_today,
                "recharge_amount": total_amount_today,
                "recharge_amount_formatted": f"{int(total_amount_today):,}",
                "week_trend_text": self._ratio_to_trend_text(week_ratio),
            },
        }

    def _extract_trend_summary(self, payload: dict[str, Any]) -> dict[str, Any]:
        rows = (((payload.get("data") or {}).get("data") or []))
        if not isinstance(rows, list):
            return {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            return {
                "current_amount": self._to_int(row.get("2a9u57i279ba")),
                "compare_amount": self._to_int(row.get("6zlb6zfwezv0")),
                "ratio_text": str(row.get("avq_ehkl4hqy") or "").strip(),
            }
        return {}

    def _extract_series_by_date(
        self,
        payload: dict[str, Any],
        date_field: str,
        value_fields: tuple[str, ...],
    ) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        rows = (((payload.get("data") or {}).get("data") or []))
        if not isinstance(rows, list):
            return out
        for row in rows:
            if not isinstance(row, dict):
                continue
            day = self._normalize_day_key(row.get(date_field))
            if not day:
                continue
            values: dict[str, int] = {}
            for field in value_fields:
                values[field] = self._to_int(row.get(field))
            out[day] = values
        return out

    def _series_value(self, series: dict[str, dict[str, int]], day_key: str, field: str) -> int:
        normalized_key = self._normalize_day_key(day_key)
        row = series.get(normalized_key) or {}
        return int(row.get(field) or 0)

    def _extract_single_value(
        self,
        payload: dict[str, Any],
        field: str,
        date_field: str | None = None,
        target_day_key: str | None = None,
    ) -> int | None:
        rows = (((payload.get("data") or {}).get("data") or []))
        if not isinstance(rows, list):
            return None
        target_day = self._normalize_day_key(target_day_key) if target_day_key else ""
        latest_by_day: tuple[str, int] | None = None
        fallback_value: int | None = None

        for row in rows:
            if not isinstance(row, dict):
                continue
            value = self._to_int(row.get(field))
            if fallback_value is None:
                fallback_value = value
            if date_field:
                day = self._normalize_day_key(row.get(date_field))
                if day:
                    if latest_by_day is None or day > latest_by_day[0]:
                        latest_by_day = (day, value)
                    if target_day and day == target_day:
                        return value

        if latest_by_day is not None:
            return int(latest_by_day[1])
        return fallback_value

    def _normalize_day_key(self, raw: Any) -> str:
        if raw is None:
            return ""
        text = str(raw).strip()
        if not text:
            return ""
        if text.isdigit():
            if len(text) == 8:
                return text
            try:
                epoch = int(text)
                if epoch > 10_000_000_000:
                    dt = datetime.fromtimestamp(epoch / 1000.0, tz=ZoneInfo(self.settings.timezone))
                    return dt.strftime("%Y%m%d")
                if epoch > 1_000_000_000:
                    dt = datetime.fromtimestamp(epoch, tz=ZoneInfo(self.settings.timezone))
                    return dt.strftime("%Y%m%d")
            except (TypeError, ValueError, OSError):
                return ""
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) < 8:
            return ""
        yyyy = digits[:4]
        mm = digits[4:6]
        dd = digits[6:8]
        try:
            datetime(int(yyyy), int(mm), int(dd))
        except ValueError:
            return ""
        return f"{yyyy}{mm}{dd}"

    def _normalize_game_name(self, name: str) -> str:
        text = str(name or "").strip()
        if not text:
            return ""
        text = re.sub(r"[（(][^）)]*[）)]", "", text)
        text = re.sub(r"[-－]\s*\d+(?:\.\d+)*\s*版本.*$", "", text)
        text = re.sub(r"[-－]\s*云游戏.*$", "", text)
        text = re.split(r"[-－]", text, maxsplit=1)[0]
        text = re.sub(r"\s+", "", text)
        text = re.sub(r"[-－]+$", "", text)
        return text.strip()

    def _format_ratio(self, current: int, baseline: int) -> str:
        if baseline <= 0:
            if current <= 0:
                return "0.00%"
            return "N/A"
        ratio = ((current - baseline) / baseline) * 100.0
        prefix = "+" if ratio > 0 else ""
        return f"{prefix}{ratio:.2f}%"

    def _ratio_to_trend_text(self, ratio_text: str) -> str:
        raw = str(ratio_text or "").strip()
        if not raw or raw.upper() == "N/A":
            return "暂无同比数据"
        if raw == "0.00%" or raw == "+0.00%" or raw == "-0.00%":
            return "持平"
        if raw.startswith("+"):
            return f"上升{raw[1:]}"
        if raw.startswith("-"):
            return f"下降{raw[1:]}"
        return f"上升{raw}"

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
                return ""
            prefix = "+" if value > 0 else ""
            return f"{prefix}{value:.2f}%"
        return ""

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

    def _date_offset(self, query_date: date) -> int:
        now_local = datetime.now(ZoneInfo(self.settings.timezone)).date()
        return (query_date - now_local).days

    def _query_id(self) -> str:
        return f"{int(time.time() * 1000):x}{random.randint(10, 99)}"
