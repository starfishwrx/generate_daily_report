from __future__ import annotations

import base64
import asyncio
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

from generate_daily_report import build_wecom_link_payload
from wecom_longbot import (
    WeComBotSettings,
    WeComLongBotClient,
    build_wecom_image_body,
    build_wecom_markdown_messages,
    publish_reports_to_wecom_async,
)


class WeComLongBotTests(unittest.TestCase):
    def test_build_wecom_markdown_messages_removes_image_placeholders(self) -> None:
        messages = build_wecom_markdown_messages(
            title="日报",
            report_text="第一段\n\n[pc云游戏图片]\n\n页游付费表图片：/tmp/a.png\n\n第二段",
            max_length=500,
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("第一段", messages[0])
        self.assertIn("第二段", messages[0])
        self.assertNotIn("图片", messages[0])

    def test_build_wecom_image_body_uses_base64_and_md5(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "x.png"
            path.write_bytes(b"abc")
            body = build_wecom_image_body(path)
        self.assertEqual(body["msgtype"], "image")
        self.assertEqual(body["image"]["base64"], base64.b64encode(b"abc").decode("ascii"))
        self.assertEqual(body["image"]["md5"], "900150983cd24fb0d6963f7d28e17f72")

    def test_build_wecom_link_payload_collects_main_and_pc_urls(self) -> None:
        payloads = build_wecom_link_payload(
            date(2026, 3, 9),
            main_url="https://www.feishu.cn/docx/AAA",
            pc_url="https://www.feishu.cn/docx/BBB",
        )
        self.assertEqual(len(payloads), 1)
        self.assertIn("主日报飞书：https://www.feishu.cn/docx/AAA", payloads[0]["content"])
        self.assertIn("PC日报飞书：https://www.feishu.cn/docx/BBB", payloads[0]["content"])

    def test_async_publish_awaits_client_without_nested_event_loop(self) -> None:
        async def scenario():
            with patch.object(
                WeComLongBotClient,
                "send_messages",
                new=AsyncMock(return_value=[{"errcode": 0}]),
            ) as send:
                result = await publish_reports_to_wecom_async(
                    settings=WeComBotSettings(bot_id="bot", secret="secret"),
                    chatid="group",
                    reports=[{"title": "日报", "content": "https://example.test/report"}],
                )
            return result, send

        result, send = asyncio.run(scenario())
        self.assertTrue(result["ok"])
        self.assertEqual(result["message_count"], 1)
        send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
