from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
import mimetypes
import random
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from autodatareport.events import current_metrics

_TABLE_FONT_CONFIGURED = False


class FeishuDocError(RuntimeError):
    """Raised when Feishu doc API returns an error."""


@dataclass
class FeishuDocSettings:
    app_id: str
    app_secret: str
    folder_token: str = ""
    doc_url_prefix: str = "https://www.feishu.cn/docx/"
    timeout: int = 60
    api_base: str = "https://open.feishu.cn"
    request_retries: int = 3
    retry_backoff_seconds: float = 2.0
    image_width: int = 960
    narrow_image_width: int = 760
    tall_ratio_threshold: float = 1.9
    prevent_upscale: bool = True
    enable_auto_trim: bool = True
    trim_background_threshold: float = 0.985
    trim_padding: int = 4
    verify_content_after_publish: bool = False
    verify_content_lang: str = "zh"


def _safe_json(response: requests.Response) -> Dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:  # pragma: no cover - depends on remote API response
        raise FeishuDocError(f"飞书接口返回非JSON响应: HTTP {response.status_code}") from exc
    if not isinstance(payload, dict):
        raise FeishuDocError(f"飞书接口响应格式异常: HTTP {response.status_code}")
    return payload


def _rewind_files(files: Any) -> None:
    if not isinstance(files, dict):
        return
    for value in files.values():
        if not isinstance(value, tuple) or len(value) < 2:
            continue
        handle = value[1]
        if hasattr(handle, "seek"):
            try:
                handle.seek(0)
            except Exception:
                continue


def _request_with_retry(
    *,
    method: str,
    url: str,
    timeout: int,
    request_retries: int,
    retry_backoff_seconds: float,
    safe_to_retry: bool = False,
    **kwargs: Any,
) -> requests.Response:
    last_exc: Optional[requests.RequestException] = None
    attempts = max(1, int(request_retries)) if safe_to_retry or method.upper() in {"GET", "HEAD"} else 1
    backoff = max(0.0, float(retry_backoff_seconds))
    for attempt in range(1, attempts + 1):
        _rewind_files(kwargs.get("files"))
        metrics = current_metrics()
        if metrics is not None:
            metrics.increment("requests")
        try:
            response = requests.request(
                method=method.upper(),
                url=url,
                timeout=timeout,
                **kwargs,
            )
            status_code = response.status_code
            retryable_status = isinstance(status_code, int) and (status_code == 429 or 500 <= status_code <= 599)
            if retryable_status and attempt < attempts:
                if metrics is not None:
                    metrics.increment("retries")
                retry_after = str(response.headers.get("Retry-After", "") or "").strip()
                response.close()
                if backoff > 0:
                    try:
                        delay = max(0.0, float(retry_after)) if retry_after else backoff * attempt
                    except (TypeError, ValueError):
                        delay = backoff * attempt
                    time.sleep(delay + random.uniform(0.0, min(0.25, delay * 0.1)))
                continue
            return response
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            if metrics is not None:
                metrics.increment("retries")
            if backoff > 0:
                delay = backoff * attempt
                time.sleep(delay + random.uniform(0.0, min(0.25, delay * 0.1)))
    raise FeishuDocError(
        f"HTTP request failed after {attempts} attempts: {last_exc}"
    )


def _api_request(
    settings: FeishuDocSettings,
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    response = _request_with_retry(
        method=method,
        url=url,
        timeout=settings.timeout,
        request_retries=settings.request_retries,
        retry_backoff_seconds=settings.retry_backoff_seconds,
        headers=headers,
        params=params,
        json=json_body,
    )
    payload = _safe_json(response)
    code = payload.get("code")
    if code not in (0, "0"):
        msg = payload.get("msg") or payload.get("message") or "unknown error"
        raise FeishuDocError(f"飞书接口失败(code={code}): {msg}")
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    return data


def _fetch_tenant_access_token(settings: FeishuDocSettings) -> str:
    response = _request_with_retry(
        method="POST",
        url=f"{settings.api_base.rstrip('/')}/open-apis/auth/v3/tenant_access_token/internal",
        timeout=settings.timeout,
        request_retries=settings.request_retries,
        retry_backoff_seconds=settings.retry_backoff_seconds,
        safe_to_retry=True,
        json={"app_id": settings.app_id, "app_secret": settings.app_secret},
    )
    payload = _safe_json(response)
    code = payload.get("code")
    if code not in (0, "0"):
        msg = payload.get("msg") or payload.get("message") or "unknown error"
        raise FeishuDocError(f"飞书鉴权失败(code={code}): {msg}")
    token = str(payload.get("tenant_access_token") or "").strip()
    if not token:
        raise FeishuDocError("飞书鉴权失败：未返回 tenant_access_token")
    return token


def fetch_tenant_access_token(settings: FeishuDocSettings) -> str:
    """Fetch one token that can be shared by all Feishu documents in a run."""

    return _fetch_tenant_access_token(settings)


def _create_document(settings: FeishuDocSettings, token: str, title: str) -> str:
    body: Dict[str, Any] = {"title": title}
    folder_token = settings.folder_token.strip()
    if folder_token:
        body["folder_token"] = folder_token
    data = _api_request(
        settings=settings,
        method="POST",
        url=f"{settings.api_base.rstrip('/')}/open-apis/docx/v1/documents",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        json_body=body,
    )
    document = data.get("document")
    if not isinstance(document, dict):
        raise FeishuDocError("飞书文档创建失败：响应缺少 document 字段")
    document_id = str(document.get("document_id") or "").strip()
    if not document_id:
        raise FeishuDocError("飞书文档创建失败：响应缺少 document_id")
    return document_id


def _list_blocks(settings: FeishuDocSettings, token: str, document_id: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    page_token = ""
    while True:
        params: Dict[str, Any] = {"page_size": 200}
        if page_token:
            params["page_token"] = page_token
        data = _api_request(
            settings=settings,
            method="GET",
            url=f"{settings.api_base.rstrip('/')}/open-apis/docx/v1/documents/{document_id}/blocks",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        batch = data.get("items")
        if isinstance(batch, list):
            items.extend([item for item in batch if isinstance(item, dict)])
        has_more = bool(data.get("has_more"))
        if not has_more:
            break
        page_token = str(data.get("page_token") or "").strip()
        if not page_token:
            break
    return items


def _resolve_root_block_id(document_id: str, blocks: List[Dict[str, Any]]) -> str:
    for block in blocks:
        if int(block.get("block_type") or 0) != 1:
            continue
        parent_id = str(block.get("parent_id") or "").strip()
        block_id = str(block.get("block_id") or "").strip()
        if block_id and not parent_id:
            return block_id
    for block in blocks:
        block_id = str(block.get("block_id") or "").strip()
        if block_id == document_id:
            return block_id
    return document_id


def _split_for_docx(text: str, max_len: int = 1200) -> List[str]:
    line = text.rstrip("\n")
    if not line:
        return []
    parts: List[str] = []
    idx = 0
    while idx < len(line):
        parts.append(line[idx : idx + max_len])
        idx += max_len
    return parts


def _to_text_children(report_text: str) -> List[Dict[str, Any]]:
    children: List[Dict[str, Any]] = []
    for line in report_text.replace("\r", "").split("\n"):
        for chunk in _split_for_docx(line):
            children.append(
                {
                    "block_type": 2,
                    "text": {
                        "elements": [
                            {
                                "text_run": {
                                    "content": chunk,
                                }
                            }
                        ]
                    },
                }
            )
    if not children:
        children.append(
            {
                "block_type": 2,
                "text": {
                    "elements": [
                        {
                            "text_run": {
                                "content": "日报内容为空",
                            }
                        }
                    ]
                },
            }
        )
    return children


def _insert_children(
    settings: FeishuDocSettings,
    token: str,
    document_id: str,
    root_block_id: str,
    children: List[Dict[str, Any]],
) -> None:
    # Split into batches to avoid field validation failures on large payloads.
    batch_size = 50
    insert_index = 0
    for offset in range(0, len(children), batch_size):
        batch = children[offset : offset + batch_size]
        _api_request(
            settings=settings,
            method="POST",
            url=f"{settings.api_base.rstrip('/')}/open-apis/docx/v1/documents/{document_id}/blocks/{root_block_id}/children",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            params={"document_revision_id": -1},
            json_body={"index": insert_index, "children": batch},
        )
        insert_index += len(batch)


def _guess_mime(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _append_text_blocks(
    settings: FeishuDocSettings,
    token: str,
    document_id: str,
    root_block_id: str,
    index: int,
    lines: List[str],
) -> int:
    if not lines:
        return 0
    children: List[Dict[str, Any]] = []
    for line in lines:
        content = str(line).rstrip("\n")
        if not content:
            continue
        children.append(
            {
                "block_type": 2,
                "text": {
                    "elements": [
                        {
                            "text_run": {
                                "content": content,
                            }
                        }
                    ]
                },
            }
        )
    if not children:
        return 0
    _api_request(
        settings=settings,
        method="POST",
        url=f"{settings.api_base.rstrip('/')}/open-apis/docx/v1/documents/{document_id}/blocks/{root_block_id}/children",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        params={"document_revision_id": -1},
        json_body={"index": index, "children": children},
    )
    return len(children)


def _create_image_placeholder(
    settings: FeishuDocSettings,
    token: str,
    document_id: str,
    root_block_id: str,
    index: int,
) -> str:
    temp_id = f"tmp_img_{uuid.uuid4().hex}"
    data = _api_request(
        settings=settings,
        method="POST",
        url=f"{settings.api_base.rstrip('/')}/open-apis/docx/v1/documents/{document_id}/blocks/{root_block_id}/descendant",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        params={"document_revision_id": -1},
        json_body={
            "children_id": [temp_id],
            "index": index,
            "descendants": [{"block_id": temp_id, "block_type": 27, "image": {}}],
        },
    )
    relations = data.get("block_id_relations")
    if not isinstance(relations, list):
        raise FeishuDocError("飞书图片占位块创建失败：缺少 block_id_relations")
    for item in relations:
        if not isinstance(item, dict):
            continue
        if str(item.get("temporary_block_id") or "") == temp_id:
            real_id = str(item.get("block_id") or "").strip()
            if real_id:
                return real_id
    raise FeishuDocError("飞书图片占位块创建失败：无法解析 block_id")


def _upload_image_token_for_block(
    settings: FeishuDocSettings,
    token: str,
    image_path: Path,
    image_block_id: str,
    document_id: str,
) -> str:
    if not image_path.exists():
        raise FeishuDocError(f"图片文件不存在：{image_path}")
    with image_path.open("rb") as fh:
        response = _request_with_retry(
            method="POST",
            url=f"{settings.api_base.rstrip('/')}/open-apis/drive/v1/medias/upload_all",
            timeout=settings.timeout,
            request_retries=settings.request_retries,
            retry_backoff_seconds=settings.retry_backoff_seconds,
            headers={"Authorization": f"Bearer {token}"},
            data={
                "file_name": image_path.name,
                "parent_type": "docx_image",
                "parent_node": image_block_id,
                "size": str(image_path.stat().st_size),
                "extra": f'{{"drive_route_token":"{document_id}"}}',
            },
            files={"file": (image_path.name, fh, _guess_mime(image_path))},
        )
    payload = _safe_json(response)
    code = payload.get("code")
    if code not in (0, "0"):
        msg = payload.get("msg") or payload.get("message") or "unknown error"
        raise FeishuDocError(f"飞书图片上传失败(code={code}): {msg}")
    token_value = str(((payload.get("data") or {}).get("file_token") or "")).strip()
    if not token_value:
        raise FeishuDocError("飞书图片上传失败：未返回 file_token")
    return token_value


def _replace_image_for_block(
    settings: FeishuDocSettings,
    token: str,
    document_id: str,
    image_block_id: str,
    image_token: str,
    image_width: int,
    image_height: int,
) -> None:
    _api_request(
        settings=settings,
        method="PATCH",
        url=f"{settings.api_base.rstrip('/')}/open-apis/docx/v1/documents/{document_id}/blocks/{image_block_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        params={"document_revision_id": -1},
        json_body={"replace_image": {"token": image_token, "width": int(image_width), "height": int(image_height)}},
    )


def _read_image_size(image_path: Path) -> Optional[Tuple[int, int]]:
    try:
        import matplotlib.image as mpimg
    except ImportError:
        return None
    try:
        arr = mpimg.imread(image_path)
    except Exception:  # noqa: BLE001
        return None
    if getattr(arr, "ndim", 0) < 2:
        return None
    height = int(arr.shape[0])
    width = int(arr.shape[1])
    if width <= 0 or height <= 0:
        return None
    return (width, height)


def _trim_image_whitespace(settings: FeishuDocSettings, image_path: Path, temp_root: Path) -> Tuple[Path, Optional[Tuple[int, int]]]:
    if not settings.enable_auto_trim:
        return image_path, _read_image_size(image_path)
    try:
        import matplotlib

        matplotlib.use("Agg", force=True)
        import matplotlib.image as mpimg
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        return image_path, _read_image_size(image_path)

    try:
        arr = mpimg.imread(image_path)
    except Exception:  # noqa: BLE001
        return image_path, _read_image_size(image_path)
    if getattr(arr, "ndim", 0) < 2:
        return image_path, None

    height = int(arr.shape[0])
    width = int(arr.shape[1])
    if width <= 0 or height <= 0:
        return image_path, None

    threshold = float(settings.trim_background_threshold)
    if arr.ndim == 2:
        mask = arr < threshold
    else:
        rgb = arr[:, :, :3]
        mask = np.any(rgb < threshold, axis=2)
        if arr.shape[2] >= 4:
            alpha = arr[:, :, 3]
            mask = mask | (alpha < 0.995)

    ys, xs = np.where(mask)
    if ys.size == 0 or xs.size == 0:
        return image_path, (width, height)

    pad = max(0, int(settings.trim_padding))
    y_min = max(0, int(ys.min()) - pad)
    y_max = min(height - 1, int(ys.max()) + pad)
    x_min = max(0, int(xs.min()) - pad)
    x_max = min(width - 1, int(xs.max()) + pad)

    if x_min == 0 and y_min == 0 and x_max == width - 1 and y_max == height - 1:
        return image_path, (width, height)

    cropped = arr[y_min : y_max + 1, x_min : x_max + 1]
    crop_h = int(cropped.shape[0])
    crop_w = int(cropped.shape[1])
    if crop_w <= 0 or crop_h <= 0:
        return image_path, (width, height)
    if crop_w >= int(width * 0.98) and crop_h >= int(height * 0.98):
        return image_path, (width, height)

    out_path = temp_root / f"trim_{uuid.uuid4().hex}.png"
    plt.imsave(out_path, cropped)
    return out_path, (crop_w, crop_h)


def _suggest_image_width(settings: FeishuDocSettings, image_path: Path, image_size: Optional[Tuple[int, int]]) -> int:
    target = int(settings.image_width)
    narrow = int(settings.narrow_image_width)
    name = image_path.name.lower()

    if "505_page_payment_table" in name or "505_mobile_payment_table" in name:
        target = min(target, narrow)

    if image_size:
        width, height = image_size
        if width > 0:
            ratio = float(height) / float(width)
            if ratio >= float(settings.tall_ratio_threshold):
                target = min(target, narrow)
            elif ratio >= 1.5:
                target = min(target, max(narrow + 60, 820))

    return max(560, target)


def _compute_target_dimensions(
    settings: FeishuDocSettings,
    image_path: Path,
    image_size: Optional[Tuple[int, int]],
) -> Tuple[int, int]:
    if not image_size:
        fallback_width = max(560, int(settings.image_width))
        # Use a conservative 4:3 fallback when actual size is unknown.
        return fallback_width, max(420, int(round(fallback_width * 0.75)))
    src_w, src_h = image_size
    if src_w <= 0 or src_h <= 0:
        fallback_width = max(560, int(settings.image_width))
        return fallback_width, max(420, int(round(fallback_width * 0.75)))

    target_w = _suggest_image_width(settings=settings, image_path=image_path, image_size=image_size)
    if settings.prevent_upscale:
        target_w = min(target_w, int(src_w))
        target_w = max(320, target_w)
    target_h = max(320, int(round(target_w * float(src_h) / float(src_w))))
    return target_w, target_h


def _split_pipe_row(line: str) -> List[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def _is_markdown_table_line(line: str) -> bool:
    text = line.strip()
    return text.startswith("|") and text.count("|") >= 2


def _render_markdown_table_image(table_lines: List[str], output_path: Path) -> bool:
    rows = [_split_pipe_row(line) for line in table_lines if line.strip()]
    if not rows:
        return False
    has_align = len(rows) > 1 and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in rows[1])
    header = rows[0]
    body = rows[2:] if has_align else rows[1:]
    if not header:
        return False
    try:
        import matplotlib

        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except ImportError:
        return False

    global _TABLE_FONT_CONFIGURED  # pylint: disable=global-statement
    if not _TABLE_FONT_CONFIGURED:
        preferred_fonts = [
            "Microsoft YaHei",
            "Microsoft YaHei UI",
            "Microsoft JhengHei",
            "PingFang SC",
            "Hiragino Sans GB",
            "Noto Sans CJK SC",
            "SimHei",
            "SimSun",
        ]
        for font_name in preferred_fonts:
            try:
                font_manager.findfont(font_name, fallback_to_default=False)
            except (ValueError, RuntimeError):
                continue
            matplotlib.rcParams["font.sans-serif"] = [font_name]
            matplotlib.rcParams["font.family"] = "sans-serif"
            matplotlib.rcParams["axes.unicode_minus"] = False
            break
        _TABLE_FONT_CONFIGURED = True

    cell_text = [header] + body
    row_count = max(1, len(cell_text))
    col_count = max(1, len(header))
    fig_width = max(8.0, min(24.0, col_count * 3.8))
    fig_height = max(1.8, min(24.0, row_count * 0.5 + 0.5))

    fig, ax = plt.subplots(figsize=(fig_width, fig_height), dpi=220)
    ax.axis("off")
    table = ax.table(cellText=cell_text, loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(11)
    table.scale(1.0, 1.28)
    for (row, _col), cell in table.get_celld().items():
        cell.set_edgecolor("#4f4f4f")
        cell.set_linewidth(0.6)
        if row == 0:
            cell.set_facecolor("#E8F4DC")
            cell.get_text().set_weight("bold")
        else:
            cell.set_facecolor("#F6F6F6")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.01)
    fig.tight_layout()
    fig.savefig(output_path, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)
    return True


def _resolve_path(path_text: str, report_base_dir: Path) -> Optional[Path]:
    raw = str(path_text or "").strip()
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute():
        return p
    return (report_base_dir / p).resolve()


def _default_chart_markers(report_base_dir: Path) -> Dict[str, Path]:
    charts_dir = (report_base_dir / "charts").resolve()
    return {
        "[总路线图片]": charts_dir / "total.png",
        "[页游图片]": charts_dir / "page.png",
        "[主机图片]": charts_dir / "console.png",
        "[手游图片]": charts_dir / "mobile.png",
        "[原神图片]": charts_dir / "genshin.png",
        "[星铁图片]": charts_dir / "starrail.png",
        "[绝区零图片]": charts_dir / "zzz.png",
        "[高画质图片]": charts_dir / "high_quality.png",
        "[pc云游戏图片]": charts_dir / "pc_cloud.png",
    }


def _build_report_segments(
    report_text: str,
    report_base_dir: Path,
    temp_root: Path,
    chart_image_paths: Optional[Dict[str, str]],
    payment_images: Optional[Dict[str, str]],
) -> List[Tuple[str, str]]:
    chart_image_paths = chart_image_paths or {}
    payment_images = payment_images or {}

    marker_to_path = _default_chart_markers(report_base_dir)
    custom_marker_map = {
        "[总路线图片]": chart_image_paths.get("total", ""),
        "[页游图片]": chart_image_paths.get("page", ""),
        "[主机图片]": chart_image_paths.get("console", ""),
        "[手游图片]": chart_image_paths.get("mobile", ""),
        "[原神图片]": chart_image_paths.get("genshin", ""),
        "[星铁图片]": chart_image_paths.get("starrail", ""),
        "[绝区零图片]": chart_image_paths.get("zzz", ""),
        "[高画质图片]": chart_image_paths.get("high_quality", ""),
        "[pc云游戏图片]": chart_image_paths.get("pc_cloud", ""),
    }
    for marker, rel_path in custom_marker_map.items():
        resolved = _resolve_path(str(rel_path or ""), report_base_dir)
        if resolved is not None:
            marker_to_path[marker] = resolved

    page_payment = _resolve_path(str(payment_images.get("page", "") or ""), report_base_dir)
    mobile_payment = _resolve_path(str(payment_images.get("mobile", "") or ""), report_base_dir)

    segments: List[Tuple[str, str]] = []
    lines = report_text.replace("\r", "").split("\n")
    idx = 0
    table_idx = 0
    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()

        if stripped in marker_to_path:
            image_path = marker_to_path[stripped]
            if image_path.exists():
                segments.append(("image", str(image_path)))
            idx += 1
            continue

        if stripped.startswith("页游付费表图片："):
            path_text = stripped.split("：", 1)[1].strip()
            image_path = _resolve_path(path_text, report_base_dir) or page_payment
            if image_path is not None and image_path.exists():
                segments.append(("image", str(image_path)))
            idx += 1
            continue

        if stripped.startswith("手游付费表图片："):
            path_text = stripped.split("：", 1)[1].strip()
            image_path = _resolve_path(path_text, report_base_dir) or mobile_payment
            if image_path is not None and image_path.exists():
                segments.append(("image", str(image_path)))
            idx += 1
            continue

        if _is_markdown_table_line(line):
            table_lines = [line]
            idx += 1
            while idx < len(lines) and _is_markdown_table_line(lines[idx]):
                table_lines.append(lines[idx])
                idx += 1
            table_idx += 1
            table_image = temp_root / f"table_{table_idx}.png"
            if _render_markdown_table_image(table_lines, table_image):
                segments.append(("image", str(table_image)))
            else:
                for raw in table_lines:
                    segments.append(("text", raw.strip()))
            continue

        segments.append(("text", stripped))
        idx += 1

    return segments


def _build_document_title(report_date: date, title_override: str, title_prefix: str) -> str:
    override = title_override.strip()
    if override:
        return override
    prefix = title_prefix.strip() or "云游戏日报"
    return f"{prefix}_{report_date.strftime('%Y%m%d')}"


def _build_document_url(prefix: str, document_id: str) -> str:
    base = prefix.strip() or "https://www.feishu.cn/docx/"
    if not base.endswith("/"):
        base = base + "/"
    return f"{base}{document_id}"


def _fetch_doc_markdown_content(
    settings: FeishuDocSettings,
    token: str,
    document_id: str,
) -> str:
    response = _request_with_retry(
        method="GET",
        url=f"{settings.api_base.rstrip('/')}/open-apis/docs/v1/content",
        timeout=settings.timeout,
        request_retries=settings.request_retries,
        retry_backoff_seconds=settings.retry_backoff_seconds,
        headers={"Authorization": f"Bearer {token}"},
        params={
            "doc_token": document_id,
            "doc_type": "docx",
            "content_type": "markdown",
            "lang": settings.verify_content_lang or "zh",
        },
    )
    payload = _safe_json(response)
    code = payload.get("code")
    if code not in (0, "0"):
        msg = payload.get("msg") or payload.get("message") or "unknown error"
        raise FeishuDocError(f"飞书文档内容校验失败(code={code}): {msg}")
    data = payload.get("data")
    if not isinstance(data, dict):
        return ""
    content = data.get("content")
    return str(content or "")


def publish_report_to_feishu_doc(
    settings: FeishuDocSettings,
    report_text: str,
    report_date: date,
    title_override: str = "",
    title_prefix: str = "云游戏日报",
    report_base_dir: Optional[Path] = None,
    chart_image_paths: Optional[Dict[str, str]] = None,
    payment_images: Optional[Dict[str, str]] = None,
    tenant_access_token: str = "",
    image_upload_concurrency: int = 3,
    on_publish_started: Optional[Callable[[], None]] = None,
    on_document_created: Optional[Callable[[Dict[str, str]], None]] = None,
) -> Dict[str, str]:
    token = tenant_access_token.strip() or _fetch_tenant_access_token(settings)
    title = _build_document_title(report_date=report_date, title_override=title_override, title_prefix=title_prefix)
    if on_publish_started is not None:
        on_publish_started()
    document_id = _create_document(settings=settings, token=token, title=title)
    initial_result = {
        "document_id": document_id,
        "title": title,
        "url": _build_document_url(settings.doc_url_prefix, document_id),
    }
    if on_document_created is not None:
        on_document_created(initial_result)
    blocks = _list_blocks(settings=settings, token=token, document_id=document_id)
    root_block_id = _resolve_root_block_id(document_id=document_id, blocks=blocks)
    base_dir = (report_base_dir or Path.cwd()).resolve()
    with tempfile.TemporaryDirectory(prefix="feishu_doc_") as temp_dir:
        temp_root = Path(temp_dir)
        segments = _build_report_segments(
            report_text=report_text,
            report_base_dir=base_dir,
            temp_root=temp_root,
            chart_image_paths=chart_image_paths,
            payment_images=payment_images,
        )
        insert_index = 0
        text_buffer: List[str] = []
        image_jobs: List[Tuple[Path, str, int, int]] = []
        for kind, payload in segments:
            if kind == "text":
                text_buffer.append(payload)
                continue
            if text_buffer:
                inserted = _append_text_blocks(
                    settings=settings,
                    token=token,
                    document_id=document_id,
                    root_block_id=root_block_id,
                    index=insert_index,
                    lines=text_buffer,
                )
                insert_index += inserted
                text_buffer = []
            image_path = Path(payload)
            if not image_path.exists():
                continue
            prepared_image_path, prepared_size = _trim_image_whitespace(
                settings=settings,
                image_path=image_path,
                temp_root=temp_root,
            )
            target_width, target_height = _compute_target_dimensions(
                settings=settings,
                image_path=image_path,
                image_size=prepared_size,
            )
            image_block_id = _create_image_placeholder(
                settings=settings,
                token=token,
                document_id=document_id,
                root_block_id=root_block_id,
                index=insert_index,
            )
            image_jobs.append((prepared_image_path, image_block_id, target_width, target_height))
            insert_index += 1
        if text_buffer:
            _append_text_blocks(
                settings=settings,
                token=token,
                document_id=document_id,
                root_block_id=root_block_id,
                index=insert_index,
                lines=text_buffer,
            )

        def publish_image(job: Tuple[Path, str, int, int]) -> None:
            prepared_image_path, image_block_id, target_width, target_height = job
            image_token = _upload_image_token_for_block(
                settings=settings,
                token=token,
                image_path=prepared_image_path,
                image_block_id=image_block_id,
                document_id=document_id,
            )
            _replace_image_for_block(
                settings=settings,
                token=token,
                document_id=document_id,
                image_block_id=image_block_id,
                image_token=image_token,
                image_width=target_width,
                image_height=target_height,
            )

        if image_jobs:
            worker_count = min(max(1, int(image_upload_concurrency)), 3, len(image_jobs))
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="feishu-image") as executor:
                for future in [executor.submit(publish_image, job) for job in image_jobs]:
                    future.result()
    out = dict(initial_result)
    if settings.verify_content_after_publish:
        content = _fetch_doc_markdown_content(settings=settings, token=token, document_id=document_id)
        out["markdown_length"] = str(len(content))
    return out
