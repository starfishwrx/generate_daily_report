from __future__ import annotations

import argparse
import unittest
from datetime import date
from unittest import mock

from generate_daily_report import preflight_870_auth, select_870_preflight_query


class AuthPreflightTests(unittest.TestCase):
    def test_select_870_preflight_query_respects_order(self) -> None:
        config = {
            "targets": {
                "total": {"label": "总", "queries": [{"params": {"game_type": 0}}]},
                "mobile": {"label": "手游", "queries": [{"params": {"game_type": 1}}]},
            },
            "report_section_order": ["mobile", "total"],
        }
        key, target_cfg, query = select_870_preflight_query(config)
        self.assertEqual(key, "mobile")
        self.assertEqual(target_cfg["label"], "手游")
        self.assertEqual(query["params"]["game_type"], 1)

    def test_preflight_870_auth_reports_missing_cookie(self) -> None:
        args = argparse.Namespace(cookie=None, proxy_mode=None, http_proxy=None, https_proxy=None, network_hosts_yaml=None)
        config = {
            "base_url": "http://admin.example.com/api",
            "network": {"proxy_mode": "direct"},
            "targets": {"total": {"label": "总", "queries": [{"params": {"game_type": 0}}]}},
        }
        result = preflight_870_auth(config, args, date(2026, 3, 7))
        self.assertFalse(result["ok"])
        self.assertIn("Session cookie missing", result["message"])

    def test_preflight_870_auth_success(self) -> None:
        args = argparse.Namespace(cookie=None, proxy_mode=None, http_proxy=None, https_proxy=None, network_hosts_yaml=None)
        config = {
            "base_url": "http://admin.example.com/api",
            "session_cookie": "PHPSESSID=test",
            "timeout": 30,
            "default_http_method": "post",
            "auto_query_params": {"add_date_begin": {"format": "%Y-%m-%d", "offset_days": 0}},
            "network": {"proxy_mode": "direct"},
            "targets": {"total": {"label": "总", "queries": [{"params": {"game_type": 0}}]}},
        }
        with mock.patch("generate_daily_report.fetch_json", return_value={"ok": True}) as mocked_fetch:
            result = preflight_870_auth(config, args, date(2026, 3, 7))
        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "870登录态可用: 总")
        _, base_url, params, timeout, method = mocked_fetch.call_args.args
        self.assertEqual(base_url, "http://admin.example.com/api")
        self.assertEqual(params["game_type"], 0)
        self.assertEqual(params["add_date_begin"], "2026-03-07")
        self.assertEqual(timeout, 30)
        self.assertEqual(method, "post")


if __name__ == "__main__":
    unittest.main()
