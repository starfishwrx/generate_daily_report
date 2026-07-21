from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from datetime import date
from pathlib import Path
from unittest import mock

import httpx
import pytest
import requests

from async_utils import RetryingAsyncClient
from autodatareport.atomic_io import atomic_write_text
from autodatareport.cache import hash_payload
from autodatareport.gui_task_controller import GuiTaskController
from autodatareport.gui_runtime import parse_event_line
from autodatareport.models import AppConfig, RunContext, RunOptions
from autodatareport.orchestrator import PipelineStages, RunPipeline
from autodatareport.pipeline import SourceTask, run_source_tasks
from autodatareport.redaction import redact_sensitive_text
from extra_metrics_service import ExtraMetricsService, ExtraSettings
from feishu_doc import FeishuDocError, FeishuDocSettings, _request_with_retry, publish_report_to_feishu_doc
from generate_daily_report import CHART_RENDERER_SCHEMA, auth_repair_enabled, collect_series_for_queries
from pc_web_metrics_service import PCWebMetricsService, PCWebSettings
from publish_state import PUBLISH_STATE_SCHEMA, PublishStateStore, PublishStatus, UncertainPublishError
from scripts.write_release_manifest import contains_sensitive_value, contains_unapproved_internal_secret


def test_atomic_write_preserves_original_when_replace_fails(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    target.write_text("old", encoding="utf-8")
    with mock.patch("autodatareport.atomic_io.os.replace", side_effect=OSError("replace failed")):
        with pytest.raises(OSError, match="replace failed"):
            atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".*.tmp-*"))


def test_redaction_covers_desktop_auth_formats() -> None:
    raw = "PHPSESSID=abc; Authorization: Bearer secret-token\nAdmin-Token=x e_token=y token=z webhook=https://secret"
    clean = redact_sensitive_text(raw)
    for secret in ("abc", "secret-token", "Admin-Token=x", "e_token=y", "token=z", "https://secret"):
        assert secret not in clean
    assert clean.count("<redacted>") >= 3


def test_publish_state_v1_completed_is_compatible_and_upgrades_on_write(tmp_path: Path) -> None:
    store = PublishStateStore(tmp_path, date(2026, 7, 20))
    store.state_dir.mkdir(parents=True)
    store.path.write_text(
        json.dumps({"date": "2026-07-20", "targets": {"feishu_main": {"status": "completed", "content_hash": "h", "result": {"url": "u"}}}}),
        encoding="utf-8",
    )
    assert store.completed_result("feishu_main", "h") == {"url": "u"}
    store.mark_completed("feishu_pc", "p", {"url": "pc"})
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["schema"] == PUBLISH_STATE_SCHEMA
    assert set(payload["targets"]) == {"feishu_main", "feishu_pc"}


def test_uncertain_publish_blocks_until_user_resolution(tmp_path: Path) -> None:
    store = PublishStateStore(tmp_path, date(2026, 7, 20))
    store.mark_publishing("feishu_main", "h")
    with pytest.raises(UncertainPublishError):
        store.assert_publish_allowed("feishu_main", "h")
    assert store.entry("feishu_main", "h").status is PublishStatus.UNCERTAIN
    store.resolve_uncertain("feishu_main", "retry")
    store.assert_publish_allowed("feishu_main", "h")
    assert store.entry("feishu_main", "h").status is PublishStatus.FAILED


def test_parallel_publish_targets_do_not_overwrite_each_other(tmp_path: Path) -> None:
    store = PublishStateStore(tmp_path, date(2026, 7, 20))
    targets = [f"target_{index}" for index in range(20)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda target: store.mark_completed(target, target, {"url": target}), targets))
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert set(payload["targets"]) == set(targets)


class _FakeProcess:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.returncode = 0

    def poll(self):
        return self.returncode if self.done.is_set() else None

    def wait(self, timeout=None):
        if not self.done.wait(timeout):
            raise TimeoutError
        return self.returncode

    def kill(self) -> None:
        self.done.set()


class _FakeRunner:
    def __init__(self) -> None:
        self.process = _FakeProcess()

    def open(self, *_args, **_kwargs):
        return self.process

    def stream(self, process, _on_line):
        process.done.wait(2)
        return process.returncode

    def terminate(self, process):
        process.done.set()


def test_gui_controller_owns_aux_process_and_rejects_stale_finish(tmp_path: Path) -> None:
    runner = _FakeRunner()
    controller = GuiTaskController(runner)  # type: ignore[arg-type]
    done = threading.Event()
    task_id = controller.start(
        ["fake"], cwd=tmp_path, env={}, kind="aux", label="首次设置", on_line=lambda *_: None, on_done=lambda *_: done.set()
    )
    assert controller.busy
    assert controller.finish(task_id + 1) is False
    controller.set_stage(task_id, "publish_feishu_main")
    assert controller.active and controller.active.publishing
    assert controller.stop()
    assert done.wait(2)
    assert controller.finish(task_id)
    assert not controller.busy


def test_strict_source_failure_cancels_sibling() -> None:
    async def scenario() -> tuple[object, asyncio.Event]:
        cancelled = asyncio.Event()

        async def fail():
            raise RuntimeError("boom")

        async def slow():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        result = await run_source_tasks([SourceTask("strict", fail, strict=True), SourceTask("slow", slow)], max_active_sources=2)
        return result, cancelled

    result, cancelled = asyncio.run(scenario())
    assert "strict" in result.errors
    assert cancelled.is_set()


def test_retry_after_header_is_honored() -> None:
    async def scenario() -> tuple[httpx.Response, mock.AsyncMock]:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(429, headers={"Retry-After": "2"}, request=request) if calls == 1 else httpx.Response(200, request=request)

        with mock.patch("async_utils.asyncio.sleep", new=mock.AsyncMock()) as sleep:
            async with RetryingAsyncClient(transport=httpx.MockTransport(handler), request_retries=2) as client:
                response = await client.get("https://example.test")
        return response, sleep

    response, sleep = asyncio.run(scenario())
    assert response.status_code == 200
    assert sleep.await_args.args[0] >= 2


def test_preflight_response_cache_avoids_duplicate_870_request() -> None:
    cache = {"fingerprint": {"data": []}}
    with mock.patch("autodatareport.application._870_request_fingerprint", return_value="fingerprint"):
        with mock.patch("autodatareport.application.fetch_json") as fetch:
            with mock.patch("autodatareport.application.extract_series", return_value={}):
                collect_series_for_queries(
                    queries=[{"params": {"game_type": 0}}],
                    auto_params={"add_date_begin": "2026-07-20"},
                    concurrency_patterns=[],
                    queue_patterns=[],
                    session=mock.Mock(),
                    base_url="https://example.test",
                    base_date=date(2026, 7, 20),
                    timeout=30,
                    default_http_method="post",
                    time_field="time",
                    response_cache=cache,
                )
    fetch.assert_not_called()


def test_feishu_write_is_not_blindly_retried() -> None:
    with mock.patch("feishu_doc.requests.request", side_effect=[requests.ReadTimeout("unknown"), mock.Mock()] ) as request:
        with pytest.raises(FeishuDocError):
            _request_with_retry(
                method="POST",
                url="https://example.test/write",
                timeout=1,
                request_retries=3,
                retry_backoff_seconds=0,
            )
    assert request.call_count == 1


def test_feishu_document_id_is_reported_before_content_append() -> None:
    seen: list[dict[str, str]] = []
    settings = FeishuDocSettings(app_id="id", app_secret="secret")
    with mock.patch("feishu_doc._create_document", return_value="doc-id"):
        with mock.patch("feishu_doc._list_blocks", return_value=[]):
            with mock.patch("feishu_doc._resolve_root_block_id", return_value="root"):
                with mock.patch("feishu_doc._build_report_segments", return_value=[]):
                    result = publish_report_to_feishu_doc(
                        settings,
                        "report",
                        date(2026, 7, 20),
                        tenant_access_token="token",
                        on_document_created=seen.append,
                    )
    assert seen[0]["document_id"] == "doc-id"
    assert result["url"].endswith("doc-id")


class _ReusableClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("service_kind", ["extra", "pc"])
def test_run_scoped_http_client_is_reused_and_closed(tmp_path: Path, service_kind: str) -> None:
    if service_kind == "extra":
        service = ExtraMetricsService(
            ExtraSettings("Asia/Shanghai", 30, "", "", tmp_path / "debug.jsonl", "https://fenxi.test", "https://manage.test")
        )
        key = "fenxi"
    else:
        service = PCWebMetricsService(PCWebSettings("https://pc.test", "https://web.test", 30, "", ""))
        key = "pc"
    created: list[_ReusableClient] = []

    def make_client() -> _ReusableClient:
        client = _ReusableClient()
        created.append(client)
        return client

    async def scenario() -> tuple[object, object]:
        service.enable_client_reuse()
        with mock.patch.object(service, "_client", side_effect=make_client):
            async with service._client_scope(key) as first:  # noqa: SLF001
                pass
            async with service._client_scope(key) as second:  # noqa: SLF001
                pass
            await service.aclose()
        return first, second

    first, second = asyncio.run(scenario())
    assert first is second
    assert len(created) == 1
    assert created[0].closed


def test_five_stage_pipeline_order_and_outcome_paths(tmp_path: Path) -> None:
    order: list[str] = []

    def stage(name: str, result):
        async def run(_context):
            order.append(name)
            return result

        return run

    context = RunContext(
        options=RunOptions(tmp_path / "config.yaml", tmp_path, None, tmp_path / "auth.json", False, True, False),
        config=AppConfig({}),
        report_date=date(2026, 7, 20),
        output_dir=tmp_path,
        charts_dir=tmp_path / "charts",
    )
    main_report = tmp_path / "main.txt"
    pc_report = tmp_path / "pc.txt"
    pipeline = RunPipeline(
        PipelineStages(
            stage("authenticate", {"ok": True}),
            stage("collect", {"rows": 1}),
            stage("calculate", {"metric": 1}),
            stage("render", {"output_path": main_report, "pc_report_path": pc_report}),
            stage("publish", {"main": "https://docs.test/main"}),
        )
    )

    outcome = asyncio.run(pipeline.run(context))

    assert order == ["authenticate", "collect", "calculate", "render", "publish"]
    assert outcome.artifacts == {"main": main_report, "pc": pc_report}
    assert outcome.publish_urls == {"main": "https://docs.test/main"}


def test_gui_event_exposes_typed_publish_failure() -> None:
    event = parse_event_line(
        json.dumps(
            {
                "schema": "autodatareport.event.v1",
                "kind": "run_finished",
                "stage": "pipeline",
                "details": {
                    "status": "error",
                    "failure_kind": "publish_uncertain",
                    "failure_source": "feishu_main",
                    "error_type": "PublishUncertainError",
                },
            }
        )
    )
    assert event is not None
    assert event.failure_kind == "publish_uncertain"
    assert event.failure_source == "feishu_main"
    assert event.error_type == "PublishUncertainError"


def test_renderer_schema_version_changes_cache_fingerprint() -> None:
    payload = {"series": [1, 2, 3], "theme": "default"}
    assert hash_payload(["chart-renderer-v1", "1.4.0", payload]) != hash_payload(
        [CHART_RENDERER_SCHEMA, "1.5.0", payload]
    )


def test_release_sensitive_scan_distinguishes_empty_and_real_credentials(tmp_path: Path) -> None:
    clean = tmp_path / "clean.yaml"
    example = tmp_path / "example.json"
    secret = tmp_path / "secret.yaml"
    clean.write_text("session_cookie: ''\nfeishu_doc:\n  app_secret: ''\n", encoding="utf-8")
    example.write_text('{"e_token": "<REDACTED>", "PHPSESSID": "<YOUR_SESSION_ID>"}', encoding="utf-8")
    secret.write_text("session_cookie: PHPSESSID=real-secret\n", encoding="utf-8")
    assert not contains_sensitive_value(clean)
    assert not contains_sensitive_value(example)
    assert contains_sensitive_value(secret)


def test_internal_publish_manifest_allows_only_designated_publish_secrets(tmp_path: Path) -> None:
    internal = tmp_path / "company-defaults.yaml"
    internal.write_text(
        "feishu_doc:\n  app_secret: app-secret\n"
        "wecom_bot:\n  secret: bot-secret\n"
        "session_cookie: ''\n",
        encoding="utf-8",
    )
    assert contains_sensitive_value(internal)
    assert not contains_unapproved_internal_secret(internal)

    internal.write_text(
        "feishu_doc:\n  app_secret: app-secret\n"
        "wecom_bot:\n  secret: bot-secret\n"
        "session_cookie: PHPSESSID=must-not-ship\n",
        encoding="utf-8",
    )
    assert contains_unapproved_internal_secret(internal)


def test_explicit_false_environment_disables_configured_auto_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_REPAIR_ENABLED", "false")
    args = SimpleNamespace(repair_auth_on_failure=False)
    assert not auth_repair_enabled({"auth_repair": {"enabled": True}}, args)
