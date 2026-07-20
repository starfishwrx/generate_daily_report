from __future__ import annotations

import asyncio
import io
import json
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import httpx

from async_utils import RetryingAsyncClient
from autodatareport.cache import ArtifactCache, hash_payload
from autodatareport.events import JsonlEventSink, RunMetricsRecorder
from autodatareport.gui_runtime import parse_event_line
from autodatareport.models import StageEvent
from autodatareport.pipeline import SourceTask, run_source_tasks
from feishu_doc import FeishuDocSettings
from generate_daily_report import (
    FeishuPublishJob,
    MetricSummary,
    TargetResult,
    TimePoint,
    _target_chart_input,
    generate_chart,
    publish_feishu_jobs,
)


class EventAndMetricsTests(unittest.TestCase):
    def test_jsonl_event_round_trips_into_gui_event(self) -> None:
        stream = io.StringIO()
        JsonlEventSink(stream).emit(
            StageEvent(
                kind="publish_finished",
                stage="publish_feishu_main",
                message="主日报已发送",
                progress=96,
                details={"target": "main", "url": "https://example.test/doc"},
            )
        )
        parsed = parse_event_line(stream.getvalue())
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.progress, 96)
        self.assertEqual(parsed.target, "main")
        self.assertEqual(parsed.url, "https://example.test/doc")

    def test_metrics_are_written_under_run_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = RunMetricsRecorder(Path(temp_dir), "2026-07-19")
            recorder.record_stage("collect", 1.25, status="ok")
            recorder.increment("requests", 3)
            path = recorder.finalize(status="ok")
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(path.parent.name, "run_metrics")
            self.assertEqual(payload["schema"], "autodatareport.run_metrics.v1")
            self.assertEqual(payload["counters"]["requests"], 3)


class CacheAndPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_chart_cache_input_uses_metric_series(self) -> None:
        target = TargetResult(
            key="total",
            label="总",
            concurrency=MetricSummary(series=[TimePoint(None, "10:00", 10, 123.0)]),
            queue=MetricSummary(series=[TimePoint(None, "10:00", 10, 4.0)]),
        )
        payload = _target_chart_input(target)
        self.assertEqual(payload["concurrency"], [("10:00", 123.0, 10)])
        self.assertEqual(payload["queue"], [("10:00", 4.0, 10)])

    async def test_background_chart_render_uses_headless_backend(self) -> None:
        import matplotlib

        target = TargetResult(
            key="total",
            label="总",
            concurrency=MetricSummary(series=[TimePoint(None, "10:00", 10, 123.0)]),
            queue=MetricSummary(series=[TimePoint(None, "10:00", 10, 4.0)]),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "chart.png"
            self.assertEqual(generate_chart(target, output), output)
            self.assertTrue(output.exists())
        self.assertEqual(str(matplotlib.get_backend()).lower(), "agg")

    async def test_async_client_retries_temporary_server_failure(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503 if calls == 1 else 200, request=request)

        async with RetryingAsyncClient(
            transport=httpx.MockTransport(handler),
            request_retries=2,
            retry_backoff_seconds=0,
        ) as client:
            response = await client.get("https://example.test/data")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, 2)

    async def test_source_tasks_run_concurrently_and_preserve_errors(self) -> None:
        active = 0
        peak = 0
        both_started = asyncio.Event()
        release = asyncio.Event()

        async def slow(value: str) -> str:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active >= 2:
                both_started.set()
            await release.wait()
            active -= 1
            return value

        async def fail() -> str:
            await asyncio.sleep(0.01)
            raise RuntimeError("boom")

        runner = asyncio.create_task(
            run_source_tasks(
                [
                    SourceTask("a", lambda: slow("a")),
                    SourceTask("b", lambda: slow("b")),
                    SourceTask("bad", fail, strict=False),
                ],
                max_active_sources=3,
            )
        )
        await asyncio.wait_for(both_started.wait(), timeout=2)
        release.set()
        result = await runner
        self.assertEqual(peak, 2)
        self.assertEqual(result.values, {"a": "a", "b": "b"})
        self.assertIn("bad", result.errors)

    async def test_artifact_cache_requires_matching_hash_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            artifact = output / "chart.png"
            artifact.write_bytes(b"png")
            digest = hash_payload([{"value": 1}])
            cache = ArtifactCache(output)
            cache.update("chart", digest, [artifact])
            cache.save()
            loaded = ArtifactCache(output)
            self.assertTrue(loaded.is_fresh("chart", digest, [artifact]))
            self.assertFalse(loaded.is_fresh("chart", hash_payload([{"value": 2}]), [artifact]))


class PublishConcurrencyTests(unittest.TestCase):
    def test_two_documents_share_one_token_and_publish_concurrently(self) -> None:
        settings = FeishuDocSettings(app_id="id", app_secret="secret")
        barrier = threading.Barrier(2)
        job = lambda target: FeishuPublishJob(  # noqa: E731
            target=target,
            report_text=target,
            report_date=date(2026, 7, 19),
            title_override="",
            title_prefix=target,
            report_base_dir=Path("."),
            chart_image_paths={},
        )

        def fake_publish(**kwargs):
            barrier.wait(timeout=2)
            return {"url": f"https://example.test/{kwargs['report_text']}"}

        with mock.patch("generate_daily_report.fetch_tenant_access_token", return_value="shared") as token_mock:
            with mock.patch("generate_daily_report.publish_report_to_feishu_doc", side_effect=fake_publish) as publish_mock:
                results = publish_feishu_jobs(settings, [job("main"), job("pc")])
        token_mock.assert_called_once_with(settings)
        self.assertEqual(publish_mock.call_count, 2)
        self.assertEqual(results["main"][0]["url"], "https://example.test/main")
        self.assertTrue(all(call.kwargs["tenant_access_token"] == "shared" for call in publish_mock.call_args_list))


if __name__ == "__main__":
    unittest.main()
