from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import websockets


DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe WeCom long-connection bot and capture callback targets.")
    parser.add_argument("--bot-id", required=True, help="WeCom long bot BotID")
    parser.add_argument("--secret", required=True, help="WeCom long bot Secret")
    parser.add_argument("--ws-url", default=DEFAULT_WS_URL, help="WeCom long bot websocket URL")
    parser.add_argument("--heartbeat-interval", type=float, default=30.0, help="Heartbeat interval in seconds")
    parser.add_argument("--output-dir", type=Path, default=Path("output"), help="Directory for logs and captured targets")
    return parser.parse_args()


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def make_req_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def json_dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


class WeComLongBotProbe:
    def __init__(self, bot_id: str, secret: str, ws_url: str, heartbeat_interval: float, output_dir: Path) -> None:
        self.bot_id = bot_id
        self.secret = secret
        self.ws_url = ws_url
        self.heartbeat_interval = max(5.0, float(heartbeat_interval))
        self.output_dir = output_dir
        self.targets_log_path = self.output_dir / "wecom_bot_targets.jsonl"
        self.last_target_path = self.output_dir / "wecom_bot_last_target.json"
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.stop_event = asyncio.Event()
        self.auth_req_id: Optional[str] = None
        self.authenticated = False

    async def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                logging.info("Connecting to WeCom websocket: %s", self.ws_url)
                connect_kwargs = {
                    "open_timeout": 20,
                    "ping_interval": None,
                    "close_timeout": 10,
                    "max_size": 8 * 1024 * 1024,
                    "proxy": None,
                }
                async with websockets.connect(
                    self.ws_url,
                    **connect_kwargs,
                ) as ws:
                    self.ws = ws
                    logging.info("WebSocket connected")
                    await self._subscribe()
                    heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                    try:
                        await self._recv_loop()
                    finally:
                        heartbeat_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await heartbeat_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logging.exception("WeCom websocket probe error: %s", exc)
            finally:
                self.ws = None
                self.authenticated = False
                if not self.stop_event.is_set():
                    logging.info("Disconnected; reconnecting in 3s")
                    await asyncio.sleep(3)

    async def _subscribe(self) -> None:
        self.auth_req_id = make_req_id("aibot_subscribe")
        frame = {
            "cmd": "aibot_subscribe",
            "headers": {"req_id": self.auth_req_id},
            "body": {
                "bot_id": self.bot_id,
                "secret": self.secret,
            },
        }
        assert self.ws is not None
        await self.ws.send(json_dumps(frame))
        logging.info("Subscribe frame sent")

    async def _heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(self.heartbeat_interval)
            if self.ws is None:
                return
            req_id = make_req_id("ping")
            frame = {"cmd": "ping", "headers": {"req_id": req_id}}
            await self.ws.send(json_dumps(frame))
            logging.info("heartbeat ping req_id=%s", req_id)

    async def _recv_loop(self) -> None:
        assert self.ws is not None
        async for message in self.ws:
            await self._handle_message(message)

    async def _handle_message(self, message: Any) -> None:
        if isinstance(message, bytes):
            text = message.decode("utf-8", "replace")
        else:
            text = str(message)

        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            logging.info("Received non-JSON frame: %s", text[:500])
            return

        headers = frame.get("headers") if isinstance(frame, dict) else {}
        req_id = str((headers or {}).get("req_id") or "")
        cmd = str(frame.get("cmd") or "")

        if not cmd:
            errcode = int(frame.get("errcode") or 0)
            errmsg = str(frame.get("errmsg") or "")
            if self.auth_req_id and req_id == self.auth_req_id:
                if errcode == 0:
                    self.authenticated = True
                    logging.info("aibot_subscribe success")
                else:
                    logging.error("aibot_subscribe failed errcode=%s errmsg=%s", errcode, errmsg)
                return
            if req_id.startswith("ping-"):
                logging.info("heartbeat pong req_id=%s errcode=%s errmsg=%s", req_id, errcode, errmsg or "ok")
                return
            logging.info("Received ack req_id=%s errcode=%s errmsg=%s", req_id, errcode, errmsg)
            return

        logging.info("Received cmd=%s", cmd)
        if cmd in {"aibot_msg_callback", "aibot_event_callback"}:
            self._record_target(frame)
            logging.info("Callback body: %s", json.dumps(frame.get("body") or {}, ensure_ascii=False))
        else:
            logging.info("Frame: %s", json.dumps(frame, ensure_ascii=False))

    def _record_target(self, frame: dict[str, Any]) -> None:
        body = frame.get("body")
        if not isinstance(body, dict):
            return
        target = {
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "cmd": str(frame.get("cmd") or ""),
            "req_id": str(((frame.get("headers") or {}) if isinstance(frame.get("headers"), dict) else {}).get("req_id") or ""),
            "chatid": str(body.get("chatid") or "").strip(),
            "chattype": str(body.get("chattype") or "").strip(),
            "from_userid": str(((body.get("from") or {}) if isinstance(body.get("from"), dict) else {}).get("userid") or "").strip(),
            "msgtype": str(body.get("msgtype") or "").strip(),
        }
        self.targets_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.targets_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(target, ensure_ascii=False) + "\n")
        self.last_target_path.write_text(json.dumps(target, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("Captured target: %s", json.dumps(target, ensure_ascii=False))


async def async_main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    setup_logging(output_dir / "wecom_bot.log")
    for env_name in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "WS_PROXY", "ws_proxy", "WSS_PROXY", "wss_proxy"):
        os.environ.pop(env_name, None)
    probe = WeComLongBotProbe(
        bot_id=args.bot_id,
        secret=args.secret,
        ws_url=args.ws_url,
        heartbeat_interval=args.heartbeat_interval,
        output_dir=output_dir,
    )

    loop = asyncio.get_running_loop()

    def _stop() -> None:
        logging.info("Stop signal received")
        probe.stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _stop)

    await probe.run()


if __name__ == "__main__":
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass
