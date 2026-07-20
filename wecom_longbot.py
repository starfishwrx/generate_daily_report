from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence
from urllib.parse import urlparse

import websockets


DEFAULT_WECOM_WS_URL = "wss://openws.work.weixin.qq.com"


class WeComBotError(RuntimeError):
    """Raised when WeCom long-bot send flow fails."""


class WeComBotSendError(WeComBotError):
    def __init__(self, errcode: int, errmsg: str) -> None:
        super().__init__(f"aibot_send_msg failed errcode={errcode} errmsg={errmsg}")
        self.errcode = int(errcode)
        self.errmsg = errmsg


@dataclass
class WeComBotSettings:
    bot_id: str
    secret: str
    ws_url: str = DEFAULT_WECOM_WS_URL
    open_timeout: float = 20.0
    ack_timeout: float = 20.0
    max_message_length: int = 3200


def _make_req_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _json_dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _describe_ws_connect_error(ws_url: str, exc: BaseException) -> str:
    parsed = urlparse(str(ws_url or ""))
    host = parsed.hostname or str(ws_url or "").strip() or "-"
    if isinstance(exc, socket.gaierror):
        return f"企业微信 WebSocket 连接失败：无法解析域名 {host}，请检查 DNS、网络或代理设置。底层错误：{exc}"
    if isinstance(exc, TimeoutError):
        return f"企业微信 WebSocket 连接超时：{host}。请检查网络是否可访问企业微信开放平台。底层错误：{exc}"
    if isinstance(exc, OSError):
        return f"企业微信 WebSocket 连接失败：{host}。请检查网络、防火墙或代理设置。底层错误：{exc}"
    return f"企业微信 WebSocket 连接失败：{host}。{type(exc).__name__}: {exc}"


def _normalize_report_text(report_text: str) -> str:
    lines: List[str] = []
    for raw in report_text.replace("\r", "").split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if re.fullmatch(r"\[[^\]]*图片\]", stripped):
            continue
        if "图片：" in stripped:
            continue
        if stripped.startswith("页游付费表图片：") or stripped.startswith("手游付费表图片："):
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _split_message(content: str, max_length: int) -> List[str]:
    max_length = max(500, int(max_length))
    text = content.strip()
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    chunks: List[str] = []
    blocks = re.split(r"\n{2,}", text)
    current = ""
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(block) > max_length:
            cut = block.rfind("\n", 0, max_length)
            if cut < max_length // 2:
                cut = max_length
            chunks.append(block[:cut].strip())
            block = block[cut:].strip()
        current = block
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk]


def build_wecom_markdown_messages(title: str, report_text: str, max_length: int = 3200) -> List[str]:
    normalized = _normalize_report_text(report_text)
    prefix = f"# {title}".strip()
    if not normalized:
        return [prefix] if prefix else []
    body_budget = max(500, int(max_length) - len(prefix) - 2)
    body_chunks = _split_message(normalized, body_budget)
    messages: List[str] = []
    for idx, chunk in enumerate(body_chunks, start=1):
        header = prefix
        if len(body_chunks) > 1:
            header = f"{prefix} ({idx}/{len(body_chunks)})"
        messages.append(f"{header}\n\n{chunk}".strip())
    return messages


def build_wecom_image_body(image_path: Path) -> Dict[str, Any]:
    data = image_path.read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    # The WeCom image protocol requires MD5 as a transport checksum, not for security.
    md5_hex = hashlib.md5(data, usedforsecurity=False).hexdigest()
    return {
        "msgtype": "image",
        "image": {
            "base64": encoded,
            "md5": md5_hex,
        },
    }


class WeComLongBotClient:
    def __init__(self, settings: WeComBotSettings) -> None:
        self.settings = settings

    async def send_messages(self, chatid: str, bodies: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not chatid.strip():
            raise WeComBotError("企业微信目标 chatid/userid 为空")
        clean_bodies = [body for body in bodies if isinstance(body, dict) and body.get("msgtype")]
        if not clean_bodies:
            raise WeComBotError("企业微信待发送消息为空")

        for env_name in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "WS_PROXY", "ws_proxy", "WSS_PROXY", "wss_proxy"):
            os.environ.pop(env_name, None)

        connect_kwargs = {
            "open_timeout": self.settings.open_timeout,
            "ping_interval": None,
            "close_timeout": 10,
            "max_size": 8 * 1024 * 1024,
            "proxy": None,
        }
        try:
            async with websockets.connect(self.settings.ws_url, **connect_kwargs) as ws:
                await self._subscribe(ws)
                responses: List[Dict[str, Any]] = []
                image_send_supported = True
                for body in clean_bodies:
                    msgtype = str(body.get("msgtype") or "").strip().lower()
                    if msgtype == "image" and not image_send_supported:
                        continue
                    try:
                        responses.append(await self._send_body(ws, chatid, body))
                    except WeComBotSendError as exc:
                        if msgtype == "image" and exc.errcode == 40008:
                            image_send_supported = False
                            logging.warning("WeCom long-bot active push does not support image messages; skip remaining images.")
                            continue
                        raise
                return responses
        except WeComBotError:
            raise
        except (OSError, TimeoutError) as exc:
            raise WeComBotError(_describe_ws_connect_error(self.settings.ws_url, exc)) from exc

    async def _subscribe(self, ws: websockets.WebSocketClientProtocol) -> None:
        req_id = _make_req_id("aibot_subscribe")
        payload = {
            "cmd": "aibot_subscribe",
            "headers": {"req_id": req_id},
            "body": {
                "bot_id": self.settings.bot_id,
                "secret": self.settings.secret,
            },
        }
        await ws.send(_json_dumps(payload))
        ack = await self._wait_for_ack(ws, req_id)
        errcode = int(ack.get("errcode") or 0)
        if errcode != 0:
            raise WeComBotError(f"aibot_subscribe failed errcode={errcode} errmsg={ack.get('errmsg')}")
        logging.info("WeCom aibot_subscribe success")

    async def _send_body(self, ws: websockets.WebSocketClientProtocol, chatid: str, body: Dict[str, Any]) -> Dict[str, Any]:
        req_id = _make_req_id("aibot_send_msg")
        payload = {
            "cmd": "aibot_send_msg",
            "headers": {"req_id": req_id},
            "body": {
                "chatid": chatid,
                **body,
            },
        }
        await ws.send(_json_dumps(payload))
        ack = await self._wait_for_ack(ws, req_id)
        errcode = int(ack.get("errcode") or 0)
        if errcode != 0:
            raise WeComBotSendError(errcode=errcode, errmsg=str(ack.get("errmsg") or ""))
        return ack

    async def _wait_for_ack(self, ws: websockets.WebSocketClientProtocol, req_id: str) -> Dict[str, Any]:
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self.settings.ack_timeout)
            except asyncio.TimeoutError as exc:
                raise WeComBotError(
                    f"企业微信等待回执超时（{self.settings.ack_timeout:g}s）req_id={req_id}。"
                    "请检查 bot_id/secret、chatid/userid 是否正确，或适当调大 wecom_bot.ack_timeout。"
                ) from exc

            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            try:
                frame = json.loads(text)
            except json.JSONDecodeError:
                logging.info("WeCom ignored non-JSON frame: %s", text[:300])
                continue

            headers = frame.get("headers") if isinstance(frame, dict) else {}
            incoming_req_id = str((headers or {}).get("req_id") or "")
            cmd = str(frame.get("cmd") or "")

            if cmd == "ping":
                pong = {"headers": {"req_id": incoming_req_id}, "errcode": 0, "errmsg": "ok"}
                with contextlib.suppress(Exception):
                    await ws.send(_json_dumps(pong))
                continue

            if cmd:
                logging.info("WeCom ignored callback cmd=%s req_id=%s", cmd, incoming_req_id)
                continue

            if incoming_req_id == req_id:
                return frame


def publish_reports_to_wecom(
    *,
    settings: WeComBotSettings,
    chatid: str,
    reports: Sequence[Dict[str, str]],
) -> Dict[str, Any]:
    all_bodies: List[Dict[str, Any]] = []
    report_titles: List[str] = []
    for report in reports:
        title = str(report.get("title") or "").strip()
        content = str(report.get("content") or "")
        image_paths = report.get("image_paths") or []
        if not title or not content.strip():
            continue
        report_titles.append(title)
        for message in build_wecom_markdown_messages(
            title=title,
            report_text=content,
            max_length=settings.max_message_length,
        ):
            all_bodies.append(
                {
                    "msgtype": "markdown",
                    "markdown": {"content": message},
                }
            )
        for raw_path in image_paths:
            path = Path(str(raw_path))
            if not path.exists() or not path.is_file():
                continue
            all_bodies.append(build_wecom_image_body(path))
    if not all_bodies:
        raise WeComBotError("没有可推送到企业微信的日报内容")
    try:
        responses = asyncio.run(WeComLongBotClient(settings).send_messages(chatid=chatid, bodies=all_bodies))
    except WeComBotError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise WeComBotError(f"企业微信推送异常：{type(exc).__name__}: {exc}") from exc
    return {
        "ok": True,
        "chatid": chatid,
        "report_titles": report_titles,
        "message_count": len(all_bodies),
        "responses": responses,
    }
