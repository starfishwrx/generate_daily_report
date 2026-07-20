from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from generate_daily_report import MetricSummary, TargetResult, main


class NoPublishSmokeTests(unittest.TestCase):
    def test_cli_generates_report_without_external_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "output"
            templates = root / "templates"
            templates.mkdir()
            (templates / "smoke.j2").write_text("{{ report_date_cn }} {{ targets.total.label }}\n", encoding="utf-8")
            config = root / "config.yaml"
            config.write_text(
                "\n".join(
                    [
                        "base_url: https://example.invalid/api",
                        "session_cookie: PHPSESSID=test",
                        "generate_charts: false",
                        "feishu_doc:",
                        "  enabled: true",
                        "wecom_bot:",
                        "  enabled: true",
                        "targets:",
                        "  total:",
                        "    label: 总",
                        "    queries:",
                        "      - params: {game_type: 0}",
                        "report_section_order: [total]",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            target = TargetResult(
                key="total",
                label="总",
                concurrency=MetricSummary(formatted_peak_value="1", peak_time_label="10点"),
                queue=MetricSummary(formatted_peak_value="0", peak_time_label="无"),
            )
            with mock.patch("generate_daily_report.run_full_auth_preflight_with_repair"):
                with mock.patch("generate_daily_report.build_target_result", return_value=target):
                    with mock.patch("generate_daily_report.publish_report_to_feishu_doc") as feishu_publish:
                        with mock.patch("generate_daily_report.push_reports_to_wecom_target") as wecom_publish:
                            main(
                                [
                                    "--data-dir",
                                    str(root),
                                    "--config",
                                    str(config),
                                    "--output",
                                    str(output),
                                    "--template-dir",
                                    str(templates),
                                    "--template-name",
                                    "smoke.j2",
                                    "--date",
                                    "2026-07-16",
                                    "--no-runtime-gui",
                                    "--no-charts",
                                    "--no-publish",
                                ]
                            )
            self.assertTrue((output / "2026716_report.txt").exists())
            feishu_publish.assert_not_called()
            wecom_publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
