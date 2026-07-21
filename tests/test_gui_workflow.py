from __future__ import annotations

import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from report_launcher_gui import ReportLauncherApp


class FakeVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value) -> None:
        self.value = value


class GuiWorkflowTests(unittest.TestCase):
    def make_app(self) -> ReportLauncherApp:
        app = ReportLauncherApp.__new__(ReportLauncherApp)
        app.app_paths = SimpleNamespace(extra_auth=Path("C:/data/extra_auth.json"))
        app.config_path = Path("C:/data/config.yaml")
        app.project_root = Path("C:/data")
        app.date_value = FakeVar((date.today() - timedelta(days=1)).isoformat())
        app.with_extra = FakeVar(True)
        app.verify_feishu = FakeVar(False)
        app.disable_feishu = FakeVar(False)
        app.auto_auth_recover = FakeVar(True)
        app.option_summary_text = FakeVar("")
        app.run_button_text = FakeVar("")
        app.status_text = FakeVar("")
        app._build_cli_command = lambda *args: ["cli", *args]
        app._append_auth_repair_args = lambda cmd, target="auto": cmd.append("--repair")
        return app

    def test_default_summary_describes_one_click_flow(self) -> None:
        app = self.make_app()
        app._update_option_summary()
        self.assertEqual(app.run_button_text.get(), "生成并发送昨天日报")
        self.assertIn("完整数据", app.option_summary_text.get())
        self.assertIn("自动发送飞书 / 企微", app.option_summary_text.get())
        self.assertIn("登录失效自动修复", app.option_summary_text.get())

    def test_generate_only_mode_disables_all_publish_channels(self) -> None:
        app = self.make_app()
        app.disable_feishu.set(True)
        command = app._build_command()
        self.assertIn("--no-publish", command)
        self.assertNotIn("--no-push-feishu-doc", command)
        app._update_option_summary()
        self.assertEqual(app.run_button_text.get(), "仅生成昨天日报")

    def test_gui_enables_v14_event_stream_and_global_limit(self) -> None:
        command = self.make_app()._build_command()
        self.assertEqual(ReportLauncherApp.APP_VERSION, "1.5")
        self.assertIn("--event-stream", command)
        self.assertIn("jsonl", command)
        self.assertIn("--max-total-concurrency", command)
        self.assertIn("8", command)

    def test_first_setup_covers_all_platforms(self) -> None:
        app = self.make_app()
        captured = {}
        app._extra_auth_path = lambda: Path("C:/data/extra_auth.json")
        app._run_aux_command = lambda label, cmd: captured.update(label=label, cmd=cmd)
        app.start_first_time_setup()
        self.assertEqual(captured["label"], "首次设置")
        self.assertIn("all", captured["cmd"])
        self.assertIn("--repair-auth-only", captured["cmd"])
        self.assertIn("--no-publish", captured["cmd"])
        date_index = captured["cmd"].index("--date") + 1
        self.assertEqual(captured["cmd"][date_index], date.today().isoformat())


if __name__ == "__main__":
    unittest.main()
