"""Slack adapter: chat.postMessage + reactions.add + chat.update + chat.delete.

Slack answers HTTP 200 with {"ok": false} for a refused call, so every action
checks the body, not just the status code. Reactions take an emoji *name*
("heart"), not the unicode character — a small map covers the common ones and
anything else is passed through for Slack to judge honestly.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any, Dict, Optional


from gateway.channel import ChannelAdapter

DEFAULT_API_BASE = "https://slack.com/api"
SLACK_WINDOW = 300  # seconds, Slack's own documented replay window

# unicode -> Slack emoji name; unknown input is passed through untouched.
EMOJI_NAMES = {
    "❤️": "heart", "❤": "heart", "👍": "+1", "👎": "-1",
    "😂": "joy", "😮": "open_mouth", "😢": "cry", "🙏": "pray",
    "🎉": "tada", "🔥": "fire", "👏": "clap", "✅": "white_check_mark",
    "👀": "eyes", "🚀": "rocket", "💯": "100",
}


def slack_emoji_name(emoji: str) -> str:
    if emoji in EMOJI_NAMES:
        return EMOJI_NAMES[emoji]
    if emoji.startswith(":") and emoji.endswith(":") and len(emoji) > 2:
        return emoji[1:-1]
    return emoji


class SlackAdapter(ChannelAdapter):
    name = "slack"

    def __init__(self, bot_token: Optional[str] = None,
                 signing_secret: Optional[str] = None,
                 api_base: Optional[str] = None) -> None:
        # None means "read the environment at call time", like the old daemon did.
        self.bot_token = bot_token
        self.signing_secret = signing_secret
        self.api_base = api_base

    def _token(self) -> str:
        if self.bot_token is not None:
            return self.bot_token
        return os.getenv("BOOM_SLACK_BOT_TOKEN", "")

    def _base(self) -> str:
        if self.api_base is not None:
            return self.api_base.rstrip("/")
        return os.getenv("BOOM_SLACK_API_BASE", DEFAULT_API_BASE).rstrip("/")

    def _secret(self) -> str:
        if self.signing_secret is not None:
            return self.signing_secret
        return os.getenv("BOOM_SLACK_SIGNING_SECRET", "")

    async def _post(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        client = self._http()
        resp = await client.post(
            f"{self._base()}/{method}",
            headers={"Authorization": f"Bearer {self._token()}"},
            json=payload,
        )
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        return {"status": resp.status_code, "body": body}

    # -- reply path -------------------------------------------------------

    def can_deliver(self) -> bool:
        return bool(self._token())

    async def send_text(self, target: str, text: str) -> bool:
        res = await self._post("chat.postMessage", {"channel": target, "text": text})
        ok = bool(res["body"].get("ok"))
        print(f"[slack] -> {res['status']} ok={ok} {len(text)} chars", flush=True)
        return res["status"] < 300 and ok

    # -- actions -----------------------------------------------------------

    async def react(self, target: str, message_id: str, emoji: str) -> Dict[str, Any]:
        # Slack addresses a message by its ts, which is what message_id carries here.
        res = await self._post("reactions.add", {
            "channel": target, "timestamp": str(message_id),
            "name": slack_emoji_name(emoji),
        })
        if res["status"] < 300 and res["body"].get("ok"):
            return {"ok": True}
        return {"error": f"slack reactions.add refused: {res['body'].get('error') or res['status']}"}

    async def edit_message(self, target: str, message_id: str, text: str) -> Dict[str, Any]:
        res = await self._post("chat.update", {
            "channel": target, "ts": str(message_id), "text": text,
        })
        if res["status"] < 300 and res["body"].get("ok"):
            return {"ok": True}
        return {"error": f"slack chat.update refused: {res['body'].get('error') or res['status']}"}

    async def delete_message(self, target: str, message_id: str) -> Dict[str, Any]:
        res = await self._post("chat.delete", {
            "channel": target, "ts": str(message_id),
        })
        if res["status"] < 300 and res["body"].get("ok"):
            return {"ok": True}
        return {"error": f"slack chat.delete refused: {res['body'].get('error') or res['status']}"}

    async def kick_member(self, target: str, user_id: str) -> Dict[str, Any]:
        """conversations.kick: remove a user from a channel. `target` is the
        channel id. Needs an admin/owner token scope; a plain bot token gets
        "not_admin". Admin-only: call only on the user's explicit request.
        (Slack has no bot ban API, so ban_member keeps the base honest error.)"""
        res = await self._post("conversations.kick", {
            "channel": target, "user": user_id,
        })
        if res["status"] < 300 and res["body"].get("ok"):
            return {"ok": True}
        err = str(res["body"].get("error") or "")
        # Slack reports missing rights as ok:false + an error name, not HTTP 403.
        if err in ("not_admin", "missing_scope"):
            return {"error": "bot lacks admin permission"}
        return {"error": f"slack conversations.kick refused: {err or res['status']}"}

    async def get_group_info(self, target: str) -> Dict[str, Any]:
        """conversations.info -> {id, name, member_count, type}.

        Slack answers HTTP 200 with {"ok": false} on refusal, so the body —
        not the status code — is checked, like every other Slack action here.
        """
        res = await self._post("conversations.info", {"channel": target})
        body = res["body"]
        if not body.get("ok"):
            return {"error": f"slack conversations.info refused: "
                             f"{body.get('error') or res['status']}"}
        ch = body.get("channel") or {}
        if ch.get("is_im"):
            ctype = "dm"
        elif ch.get("is_mpim"):
            ctype = "mpim"
        elif ch.get("is_group"):
            ctype = "private_channel"
        else:
            ctype = "channel"
        return {
            "id": str(ch.get("id", target)),
            "name": ch.get("name", ""),
            "member_count": ch.get("num_members"),
            "type": ctype,
        }

    async def get_member_info(self, target: str, user_id: str) -> Dict[str, Any]:
        """users.info -> {id, name, role, is_bot, timezone}.

        HONESTY: a bot can only look up users in workspaces it belongs to.
        An unknown user answers {"ok": false, "error": "user_not_found"} —
        reported honestly, never fabricated.
        """
        res = await self._post("users.info", {"user": user_id})
        body = res["body"]
        if not body.get("ok"):
            return {"error": f"slack users.info refused (user not visible to "
                             f"bot?): {body.get('error') or res['status']}"}
        user = body.get("user") or {}
        profile = user.get("profile") or {}
        if user.get("is_owner"):
            role = "owner"
        elif user.get("is_admin"):
            role = "admin"
        else:
            role = "member"
        return {
            "id": str(user.get("id", user_id)),
            "name": profile.get("display_name") or profile.get("real_name") or user.get("name") or "",
            "role": role,
            "is_bot": bool(user.get("is_bot")),
            "timezone": user.get("tz"),
        }

    # -- ingress path -------------------------------------------------------

    def has_verify_secret(self) -> bool:
        return bool(self._secret())

    def verify_request(self, headers: Dict[str, str], body: bytes) -> Optional[str]:
        """Slack: v0 = HMAC-SHA256(secret, "v0:<timestamp>:<raw body>")."""
        secret = self._secret()
        if not secret:
            return None
        signature = headers.get("x-slack-signature", "")
        stamp = headers.get("x-slack-request-timestamp", "")
        if not signature or not stamp.isdigit():
            return "missing X-Slack-Signature or X-Slack-Request-Timestamp"
        if abs(time.time() - int(stamp)) > SLACK_WINDOW:
            return f"stale Slack timestamp {stamp}"
        expected = "v0=" + hmac.new(
            secret.encode(), b"v0:" + stamp.encode() + b":" + body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return "bad Slack signature"
        return None

    def handshake(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if event.get("type") == "url_verification":
            return {"challenge": event.get("challenge")}
        return None

    def normalize(self, event: Dict[str, Any]) -> Dict[str, Any]:
        if "sender_id" in event and "text" in event:
            return event  # a bridge's canonical event passes through untouched
        if "type" not in event:
            return event
        if event.get("type") != "event_callback":
            return {"ignore": f"Slack payload {event.get('type')!r}"}
        inner = event.get("event") or {}
        if inner.get("type") != "message":
            return {"ignore": f"Slack event {inner.get('type')!r}"}
        # Slack sends our own replies back as message events; acting on one would loop forever.
        if inner.get("bot_id") or inner.get("subtype"):
            return {"ignore": f"Slack message with subtype {inner.get('subtype') or 'bot'}"}
        user = str(inner.get("user") or "")
        return {"sender_id": user, "sender_name": user, "text": inner.get("text") or "",
                # ts is unique per message and survives Slack's retries, so it is the key.
                "message_id": str(inner.get("client_msg_id") or inner.get("ts") or ""),
                "target": str(inner.get("channel") or "")}
