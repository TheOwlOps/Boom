"""Gửi thông báo đa kênh (telegram/discord/slack/webhook) qua webhook URL trong env.

Mỗi kênh fail graceful: thiếu cấu hình hoặc lỗi mạng đều trả về dict lỗi
chứ không raise, để các kênh khác vẫn được gửi.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Callable

# Ánh xạ kênh -> biến môi trường chứa webhook URL
CHANNEL_ENV = {
    "telegram": "TELEGRAM_WEBHOOK",
    "discord": "DISCORD_WEBHOOK",
    "slack": "SLACK_WEBHOOK",
    "webhook": "GENERIC_WEBHOOK",
}

# Payload mặc định cho từng kênh (đều chấp nhận khoá "text"/"content")
def _telegram_payload(msg: str) -> dict:
    return {"text": msg}


def _discord_payload(msg: str) -> dict:
    return {"content": msg}


def _slack_payload(msg: str) -> dict:
    return {"text": msg}


def _webhook_payload(msg: str) -> dict:
    return {"text": msg}


PAYLOAD_BUILDERS: dict[str, Callable[[str], dict]] = {
    "telegram": _telegram_payload,
    "discord": _discord_payload,
    "slack": _slack_payload,
    "webhook": _webhook_payload,
}


def _post_json(url: str, payload: dict, timeout: float = 10.0) -> bool:
    """POST JSON tới URL, trả True nếu HTTP 2xx."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return 200 <= resp.status < 300


def send(channel: str, message: str) -> dict:
    """Gửi message tới một kênh.

    Returns:
        dict {ok, channel, error} — ok=False khi thiếu env hoặc lỗi gửi.
    """
    channel = channel.lower()
    if channel not in CHANNEL_ENV:
        return {
            "ok": False,
            "channel": channel,
            "error": f"Kênh không hỗ trợ: {channel}. Chọn trong {list(CHANNEL_ENV)}",
        }
    url = os.environ.get(CHANNEL_ENV[channel])
    if not url:
        return {
            "ok": False,
            "channel": channel,
            "error": f"Thiếu biến môi trường {CHANNEL_ENV[channel]}",
        }
    try:
        payload = PAYLOAD_BUILDERS[channel](message)
        ok = _post_json(url, payload)
        return {"ok": ok, "channel": channel, "error": None if ok else "HTTP không 2xx"}
    except Exception as exc:  # fail graceful: mạng lỗi không raise
        return {"ok": False, "channel": channel, "error": f"{type(exc).__name__}: {exc}"}


def broadcast(message: str, channels: list[str] | None = None) -> list[dict]:
    """Gửi message tới nhiều kênh, trả kết quả từng kênh (kênh lỗi không chặn kênh khác)."""
    channels = channels or list(CHANNEL_ENV)
    return [send(ch, message) for ch in channels]
