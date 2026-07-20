from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from generate_daily_report import MetricSummary, TargetResult, render_pc_report


class PCReportRenderTests(unittest.TestCase):
    def test_render_pc_report_includes_notes_member_and_top_games(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            target = TargetResult(
                key="pc_cloud",
                label="PC云游戏",
                concurrency=MetricSummary(formatted_peak_value="5137", peak_time_label="15点"),
                queue=MetricSummary(formatted_peak_value="146", peak_time_label="16点"),
                queue_summary="于10点-23点有排队。",
            )
            out_path = render_pc_report(
                template_dir=Path(__file__).resolve().parents[1] / "templates",
                template_name="pc_report_template.j2",
                output_dir=output_dir,
                date_cn="2026年2月23日",
                target=target,
                pc_notes={"new_users_text": "7,601", "active_users_text": "47,177"},
                pc_member_summary={
                    "recharge_count_text": "262",
                    "first_count_text": "76",
                    "recharge_amount_text": "2,992",
                    "week_trend_text": "上升31.98%",
                },
                pc_top_games=[
                    {"name": "蛋仔派对", "active_users_text": "10,083"},
                    {"name": "崩坏：星穹铁道", "active_users_text": "5,640"},
                ],
                pc_warnings=[],
            )
            text = out_path.read_text(encoding="utf-8")
            self.assertIn("2026年2月23日游戏盒PC云游戏数据", text)
            self.assertIn("一、游戏盒PC云游戏相关数据日报", text)
            self.assertIn("PC云游戏总并发峰值：5137，时间：15点。", text)
            self.assertIn("PC云游戏总排队峰值：146，时间：16点。", text)
            self.assertIn("1、游戏的新增用户数为：7,601，游戏的活跃用户数为：47,177。", text)
            self.assertIn("2、会员充值人数：262，PC首开会员人数：76，充值金额：2,992元，环比上周同期上升31.98%。", text)
            self.assertIn("| 蛋仔派对 | 10,083 |", text)


if __name__ == "__main__":
    unittest.main()
