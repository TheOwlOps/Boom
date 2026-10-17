# -*- coding: utf-8 -*-
"""Zalo Personal bridge for Boom — full actions edition.

Exposes a tiny local HTTP API (localhost only) so the Boom core can
drive a personal Zalo account:

    GET  /status            -> {"status": "qr_pending"|"connected"|"error", ...}
    GET  /qr                -> PNG QR code to scan with the Zalo mobile app
    GET  /messages          -> drain inbound message queue
    GET  /events            -> SSE stream (messages + group events)
    POST /send              -> {"to", "text", "thread_type", "mentions"?[{uid,name}]}
    POST /react             -> {"msg_id","cli_msg_id","to","thread_type","emoji"}
    POST /recall            -> {"msg_id","cli_msg_id","to","thread_type"}
    POST /send-image        -> {"to","thread_type","file_path","caption"?}
    POST /send-file         -> {"to","thread_type","file_path","caption"?}
    POST /send-voice        -> {"to","thread_type","file_path"}  (.m4a AAC)
    POST /send-sticker      -> {"to","thread_type","keyword"}    (search + send)
    POST /group/rename      -> {"group_id","name"}
    POST /group/avatar      -> {"group_id","file_path"}
    POST /group/add-deputy  -> {"group_id","uid"}
    POST /group/kick        -> {"group_id","uid"}
    POST /group/block       -> {"group_id","uid"}
    GET  /group/members?group_id=... -> [{uid, name, avatar}]
    POST /find-user         -> {"phone"}
    GET  /user-info?uid=... -> profile (works for non-friends via UID)
    POST /lookup-phone      -> {"phone"} phone -> UID/profile, no friending.
                               Works even for hidden profiles when the number
                               is searchable; reports `visibility` per field.
    POST /lookup-qr         -> {"qr_data"} decode Zalo QR -> UID -> profile,
                               no friending, no phone. (Alias: /qr-lookup.)
    POST /qr-lookup-image   -> {"file_path"} decode QR image -> UID -> profile

Welcome automation: when a new member joins a group (GroupEventType.JOIN),
the bridge sends WELCOME_TEMPLATE (env BOOM_ZALO_WELCOME, {name} placeholder).

Security notes:
  * Binds to 127.0.0.1 only. Never expose this port to a network.
  * Session cookies stored with mode 0600 under $BOOM_HOME/zalo_personal/.
  * File paths must resolve inside $BOOM_HOME (path traversal rejected).
  * All request bodies validated; inputs length-capped.
  * Message content never logged (only ids/types).

Unofficial `zalo-api` PyPI package (reverse-engineered Zalo web client).
Use a SECONDARY account; Zalo may rate-limit or lock automation.
"""

import base64
import hashlib
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("BOOM_ZALO_BRIDGE_PORT", "17890"))
POLL_INTERVAL = int(os.environ.get("BOOM_ZALO_POLL_INTERVAL", "5"))
QR_TTL = 120
MSG_QUEUE_CAP = 500
EVENT_QUEUE_CAP = 200
MAX_TEXT_LEN = 4000
MAX_ID_LEN = 64
MAX_NAME_LEN = 100
MAX_PHONE_LEN = 20

# Lookup rate limit: max lookups per window (avoid Zalo throttling).
# Applies to /lookup-phone, /lookup-qr (/qr-lookup), /qr-lookup-image.
LOOKUP_MAX = int(os.environ.get("BOOM_ZALO_LOOKUP_MAX",
                               os.environ.get("BOOM_ZALO_QR_LOOKUP_MAX", "10")))
LOOKUP_WINDOW = int(os.environ.get("BOOM_ZALO_LOOKUP_WINDOW",
                                   os.environ.get("BOOM_ZALO_QR_LOOKUP_WINDOW", "60")))

# Image extensions accepted for POST /qr-lookup-image.
ALLOWED_QR_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}

BOOM_HOME = os.environ.get(
    "BOOM_HOME", os.path.join(os.path.expanduser("~"), ".boom")
)
STATE_DIR = os.path.join(BOOM_HOME, "zalo_personal")
SESSION_PATH = os.path.join(STATE_DIR, "session.json")
WELCOME_TEMPLATE = os.environ.get(
    "BOOM_ZALO_WELCOME", "Chào mừng {name} đã tham gia nhóm! 🎉"
)

# The 14 supported reaction emojis.
REACTIONS = ["❤️", "👍", "😂", "😆", "😮", "😭", "😡", "💩",
             "🌹", "💔", "👎", "😍", "🤔", "😉"]

ALLOWED_IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
ALLOWED_FILE_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                    ".txt", ".csv", ".zip", ".rar"}
ALLOWED_VOICE_EXT = {".m4a"}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [zalo-bridge] %(levelname)s %(message)s"
)
log = logging.getLogger("zalo-bridge")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class BridgeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "starting"
        self.error = None
        self.qr_png = None
        self.qr_issued_at = 0.0
        self.client = None
        self.self_id = None
        self.messages = queue.Queue()   # inbound text messages
        self.events = queue.Queue()     # group events (join/leave/...)
        self.msg_seq = 0
        self.seen_ids = set()

    def set_status(self, status, error=None):
        with self.lock:
            self.status = status
            self.error = error

    def push_message(self, msg):
        mid = msg.get("id")
        with self.lock:
            if mid and mid in self.seen_ids:
                return
            if mid:
                self.seen_ids.add(mid)
                if len(self.seen_ids) > 2000:
                    self.seen_ids = set(list(self.seen_ids)[-1000:])
            self.msg_seq += 1
            msg["seq"] = self.msg_seq
        _bounded_put(self.messages, msg, MSG_QUEUE_CAP)

    def push_event(self, ev):
        _bounded_put(self.events, ev, EVENT_QUEUE_CAP)

    def drain_messages(self):
        return _drain(self.messages)

    def drain_events(self):
        return _drain(self.events)


def _bounded_put(q, item, cap):
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        q.put_nowait(item)


def _drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            break
    return out


STATE = BridgeState()


# ---------------------------------------------------------------------------
# Session persistence (0600)
# ---------------------------------------------------------------------------

def _ensure_state_dir():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)


def _write_private_json(path, obj):
    data = json.dumps(obj).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def load_session():
    try:
        with open(SESSION_PATH, "r", encoding="utf-8") as f:
            obj = json.load(f)
        cookies = obj.get("session_cookies") or {}
        imei = obj.get("imei")
        if isinstance(cookies, dict) and cookies:
            return cookies, imei
    except (OSError, ValueError) as e:
        log.warning("Could not read saved session: %s", e)
    return None, None


def save_session(cookies, imei):
    _ensure_state_dir()
    _write_private_json(SESSION_PATH, {"session_cookies": cookies, "imei": imei})
    log.info("Session saved (%d cookies).", len(cookies))


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _valid_id(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_ID_LEN
        and all(c.isalnum() or c in "-_" for c in value)
    )


def _valid_text(value, max_len=MAX_TEXT_LEN):
    return isinstance(value, str) and 0 < len(value.strip()) <= max_len


def _valid_phone(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_PHONE_LEN
        and all(c.isdigit() or c == "+" for c in value)
    )


def _safe_path(value, allowed_exts):
    """Resolve a user-supplied path; must stay inside $BOOM_HOME."""
    if not isinstance(value, str) or not value or len(value) > 512:
        return None
    # Reject obvious traversal attempts early.
    if ".." in value.replace("\\", "/").split("/"):
        return None
    abs_path = os.path.abspath(os.path.join(BOOM_HOME, value))
    if not abs_path.startswith(os.path.abspath(BOOM_HOME) + os.sep):
        return None
    if not os.path.isfile(abs_path):
        return None
    _, ext = os.path.splitext(abs_path)
    if ext.lower() not in allowed_exts:
        return None
    return abs_path


def _thread_type(value):
    v = str(value or "user").lower()
    return v if v in ("user", "group") else None


# ---------------------------------------------------------------------------
# QR lookup (no friending, no phone number needed)
# ---------------------------------------------------------------------------

_lookup_lock = threading.Lock()
_lookup_times = []  # timestamps of recent lookups (sliding window)


def _lookup_allowed():
    """Sliding-window rate limiter for profile lookups (anti Zalo throttle)."""
    now = time.time()
    with _lookup_lock:
        while _lookup_times and _lookup_times[0] <= now - LOOKUP_WINDOW:
            _lookup_times.pop(0)
        if len(_lookup_times) >= LOOKUP_MAX:
            return False
        _lookup_times.append(now)
        return True


def _reset_lookup_limiter():
    """Test helper: clear the rate-limit window."""
    with _lookup_lock:
        _lookup_times.clear()


# Back-compat aliases (used by earlier tests/scripts).
_qr_lookup_lock = _lookup_lock
_qr_lookup_times = _lookup_times
_qr_lookup_allowed = _lookup_allowed
_reset_qr_lookup_limiter = _reset_lookup_limiter
QR_LOOKUP_MAX = LOOKUP_MAX
QR_LOOKUP_WINDOW = LOOKUP_WINDOW


_UID_RE = re.compile(r"^(\d{1,20})")


def _uid_from_qr_data(qr_data):
    """Extract a Zalo UID from QR payload text.

    Supported payloads:
      * raw numeric UID, e.g. "123456789"
      * Zalo share links: https://zalo.me/<uid>, https://zalo.me/s/<uid>,
        https://chat.zalo.me/...?uid=<uid>
    Returns the UID string, or None when nothing usable is found.
    """
    if not isinstance(qr_data, str):
        return None
    s = qr_data.strip()
    if not s or len(s) > 2048:
        return None
    # Raw numeric UID.
    if s.isdigit() and len(s) <= 20:
        return s
    # URL forms.
    try:
        p = urlparse(s if "://" in s else "https://" + s)
    except Exception:
        return None
    host = (p.hostname or "").lower()
    if "zalo.me" not in host:
        return None
    # Query params first (?uid= / ?id= / ?u=).
    try:
        qs = parse_qs(p.query)
        for key in ("uid", "id", "u"):
            vals = qs.get(key) or []
            for v in vals:
                m = _UID_RE.match(v.strip())
                if m:
                    return m.group(1)
    except Exception:
        pass
    # Last path segment: https://zalo.me/<uid> or /s/<uid>.
    segs = [seg for seg in p.path.split("/") if seg]
    for seg in reversed(segs):
        m = _UID_RE.match(seg)
        if m:
            return m.group(1)
    return None


def _decode_qr_image(fpath):
    """Decode QR payloads from an image file.

    Returns a list of payload strings, [] when no QR found, or None when
    the optional decode dependency (pyzbar + system libzbar) is missing.
    """
    try:
        from pyzbar.pyzbar import decode
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(fpath) as img:
            results = decode(img)
        out = []
        for r in results:
            try:
                out.append(r.data.decode("utf-8", errors="replace"))
            except Exception:
                pass
        return out
    except Exception:
        return []


def _require_client():
    client = STATE.client
    if client is None or STATE.status != "connected":
        return None
    return client


# ---------------------------------------------------------------------------
# Zalo client
# ---------------------------------------------------------------------------

def _new_client(session_cookies=None, imei=None):
    from zlapi import ZaloAPI

    self_id = {"uid": None}

    class BoomZaloClient(ZaloAPI):
        def onMessage(self, mid=None, author_id=None, message=None,
                      message_object=None, thread_id=None, thread_type=None):
            try:
                own = self_id["uid"]
                if own and str(author_id) == str(own):
                    return
                text = message if isinstance(message, str) else ""
                if not text:
                    return
                ttype = "group"
                try:
                    ttype = (thread_type.name or "USER").lower()
                except Exception:
                    pass
                STATE.push_message({
                    "id": str(mid) if mid is not None else None,
                    "thread_id": str(thread_id),
                    "thread_type": ttype,
                    "author_id": str(author_id),
                    "text": text[:MAX_TEXT_LEN],
                    "ts": int(time.time()),
                })
                # Also mirror to the SSE event stream.
                STATE.push_event({
                    "type": "message",
                    "thread_id": str(thread_id),
                    "thread_type": ttype,
                    "author_id": str(author_id),
                    "ts": int(time.time()),
                })
            except Exception as e:
                log.warning("onMessage handler failed: %s", e)

        def onEvent(self, event_data, event_type):
            try:
                from zlapi.models import GroupEventType
                etype = getattr(event_type, "name", str(event_type))
                data = {}
                try:
                    data = dict(event_data) if hasattr(event_data, "items") else {}
                except Exception:
                    data = {"raw": str(event_data)[:200]}
                ev = {"type": "group_event", "event": etype,
                      "data": data, "ts": int(time.time())}
                STATE.push_event(ev)
                # Auto-welcome on member join.
                if str(etype) == "JOIN" and WELCOME_TEMPLATE:
                    _handle_member_join(data)
            except Exception as e:
                log.warning("onEvent handler failed: %s", e)

    kwargs = dict(phone=None, password=None, auto_login=bool(session_cookies))
    if session_cookies:
        kwargs["session_cookies"] = session_cookies
    if imei:
        kwargs["imei"] = imei
    client = BoomZaloClient(**kwargs)
    return client, self_id


def _handle_member_join(data):
    """Send a welcome message when a new member joins a group."""
    try:
        group_id = str(data.get("group_id") or data.get("grid") or "")
        new_uid = str(data.get("uid") or data.get("user_id") or "")
        name = str(data.get("display_name") or data.get("name") or "bạn mới")
        if not group_id or not new_uid:
            return
        client = STATE.client
        if client is None:
            return
        from zlapi import ZaloAPI  # noqa
        from zlapi.models import Message, ThreadType, Mention
        text = WELCOME_TEMPLATE.replace("{name}", "@" + name)
        # Mention the newcomer: "@name" at offset 0.
        mention = Mention(uid=new_uid, length=len("@" + name), offset=0)
        client.sendMentionMessage(Message(text=text, mention=mention),
                                  group_id)
        log.info("Welcome sent to group %s for new member.", group_id)
    except Exception as e:
        log.warning("Welcome message failed: %s", type(e).__name__)


def _resolve_self_id(client, self_id):
    try:
        info = client.fetchAccountInfo()
        uid = getattr(info, "uid", None) or getattr(info, "id", None)
        if uid:
            self_id["uid"] = str(uid)
            log.info("Own Zalo uid resolved.")
    except Exception as e:
        log.warning("Could not resolve own uid (echo filtering off): %s",
                    type(e).__name__)


def _on_qr_generated(qr_path):
    try:
        with open(qr_path, "rb") as f:
            png = f.read()
        with STATE.lock:
            STATE.qr_png = png
            STATE.qr_issued_at = time.time()
        log.info("QR code ready (%d bytes).", len(png))
    except OSError as e:
        log.error("Failed to read generated QR: %s", e)


def login_worker():
    from zlapi import ZaloAPI  # noqa: F401

    STATE.set_status("qr_pending")
    cookies, imei = load_session()
    if cookies:
        try:
            client, self_id = _new_client(session_cookies=cookies, imei=imei)
            STATE.client = client
            _resolve_self_id(client, self_id)
            STATE.set_status("connected")
            log.info("Logged in with saved session.")
            _start_listening(client)
            return
        except Exception as e:
            log.warning("Saved session invalid, QR login: %s", e)

    try:
        client, self_id = _new_client()
        STATE.client = client
        log.info("Waiting for QR scan...")
        client.loginWithQR(
            qr_path=os.path.join(STATE_DIR, "qr.png"),
            on_qr_generated=_on_qr_generated,
            scan_timeout=QR_TTL,
            confirm_timeout=QR_TTL,
        )
        _resolve_self_id(client, self_id)
        try:
            new_cookies = client.getSession()
            new_imei = getattr(client, "_imei", None)
            if isinstance(new_cookies, dict) and new_cookies:
                save_session(new_cookies, new_imei)
        except Exception as e:
            log.warning("Could not persist session: %s", e)
        STATE.set_status("connected")
        log.info("QR login successful.")
        _start_listening(client)
    except Exception as e:
        STATE.set_status("error", str(e)[:200])
        log.error("Login failed: %s", e)


def _start_listening(client):
    def _run():
        try:
            client.listen(delay=POLL_INTERVAL, thread=False, type="requests",
                         run_forever=True, reconnect=10)
        except Exception as e:
            log.error("Listener stopped: %s", e)
            STATE.set_status("error", "listener stopped: " + str(e)[:200])

    t = threading.Thread(target=_run, name="zalo-listen", daemon=True)
    t.start()
    log.info("Listening for messages (poll every %ds).", POLL_INTERVAL)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _json(handler, obj, code=200):
    body = json.dumps(obj).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _err(handler, msg, code=400):
    _json(handler, {"error": msg}, code)


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "BoomZaloBridge/2.0"

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def _read_json(self, max_bytes=65536):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > max_bytes:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _client_or_503(self):
        client = _require_client()
        if client is None:
            _err(self, "not connected to Zalo", 503)
            return None
        return client

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/status":
            with STATE.lock:
                s, e, has_qr = STATE.status, STATE.error, STATE.qr_png is not None
            _json(self, {"status": s, "error": e, "qr_available": has_qr,
                         "reactions": REACTIONS})
        elif path == "/qr":
            with STATE.lock:
                png, issued = STATE.qr_png, STATE.qr_issued_at
            if not png or (time.time() - issued) > QR_TTL:
                _err(self, "no valid QR code", 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(png)
        elif path == "/messages":
            _json(self, {"messages": STATE.drain_messages()})
        elif path == "/events":
            # SSE stream: flush queued events, then heartbeat every 15s.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                for ev in STATE.drain_events():
                    self.wfile.write(
                        ("data: " + json.dumps(ev) + "\n\n").encode("utf-8"))
                # Keep-alive heartbeat loop (client should reconnect).
                for _ in range(4):  # ~60s total
                    time.sleep(15)
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif path == "/group/members":
            group_id = (qs.get("group_id") or [""])[0]
            if not _valid_id(group_id):
                _err(self, "group_id invalid", 400)
                return
            client = self._client_or_503()
            if client is None:
                return
            try:
                info = client.fetchGroupInfo(group_id)
                members = _extract_members(info)
                _json(self, {"members": members})
            except Exception:
                log.error("fetchGroupInfo failed: %s", "group/members")
                _err(self, "lookup failed", 502)
        elif path == "/user-info":
            uid = (qs.get("uid") or [""])[0]
            if not _valid_id(uid):
                _err(self, "uid invalid", 400)
                return
            client = self._client_or_503()
            if client is None:
                return
            try:
                info = client.fetchUserInfo(uid)
                _json(self, {"profile": _extract_profile(info)})
            except Exception:
                _err(self, "lookup failed", 502)
        else:
            _err(self, "not found", 404)

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        path = urlparse(self.path).path
        handlers = {
            "/send": self._h_send,
            "/react": self._h_react,
            "/recall": self._h_recall,
            "/send-image": self._h_send_image,
            "/send-file": self._h_send_file,
            "/send-voice": self._h_send_voice,
            "/send-sticker": self._h_send_sticker,
            "/group/rename": self._h_group_rename,
            "/group/avatar": self._h_group_avatar,
            "/group/add-deputy": self._h_group_add_deputy,
            "/group/kick": self._h_group_kick,
            "/group/block": self._h_group_block,
            "/find-user": self._h_find_user,
            "/lookup-phone": self._h_lookup_phone,
            "/lookup-qr": self._h_qr_lookup,   # canonical name
            "/qr-lookup": self._h_qr_lookup,   # alias (back-compat)
            "/qr-lookup-image": self._h_qr_lookup_image,
        }
        fn = handlers.get(path)
        if fn is None:
            _err(self, "not found", 404)
            return
        body = self._read_json()
        if not isinstance(body, dict):
            _err(self, "invalid JSON body", 400)
            return
        try:
            fn(body)
        except Exception:
            log.error("handler %s failed", path)
            _err(self, "operation failed", 502)

    # -- handlers ----------------------------------------------------------
    def _h_send(self, body):
        to = body.get("to")
        text = body.get("text")
        tt = _thread_type(body.get("thread_type"))
        mentions = body.get("mentions") or []
        if not _valid_id(to):
            _err(self, "field 'to' invalid", 400); return
        if not _valid_text(text):
            _err(self, "field 'text' invalid", 400); return
        if tt is None:
            _err(self, "thread_type must be 'user' or 'group'", 400); return
        client = self._client_or_503()
        if client is None:
            return
        from zlapi.models import Message, ThreadType, Mention
        thread = ThreadType.USER if tt == "user" else ThreadType.GROUP
        mention_payload = None
        if mentions:
            if not isinstance(mentions, list) or len(mentions) > 20:
                _err(self, "mentions: max 20", 400); return
            parts = []
            for m in mentions:
                if not isinstance(m, dict):
                    continue
                uid = m.get("uid"); name = m.get("name", "")
                if not _valid_id(str(uid)):
                    continue
                if not isinstance(name, str) or len(name) > MAX_NAME_LEN:
                    name = ""
                parts.append({"uid": str(uid), "name": name})
            if parts:
                # Build "@name" segments appended to text; offsets computed.
                base = text
                segs = []
                for p in parts:
                    tag = "@" + (p["name"] or p["uid"])
                    offset = len(base)
                    base += (" " if base and not base.endswith(" ") else "") + tag
                    segs.append({"pos": offset, "len": len(tag),
                                 "uid": p["uid"], "type": 0})
                text = base
                mention_payload = json.dumps(segs)
        if mention_payload and tt == "group":
            msg = Message(text=text, mention=mention_payload)
            client.sendMentionMessage(msg, to)
        else:
            client.sendMessage(Message(text=text), to, thread)
        _json(self, {"ok": True})

    def _h_react(self, body):
        msg_id = body.get("msg_id")
        cli_msg_id = body.get("cli_msg_id", "")
        to = body.get("to")
        tt = _thread_type(body.get("thread_type"))
        emoji = body.get("emoji")
        if not _valid_id(str(msg_id)):
            _err(self, "msg_id invalid", 400); return
        if not _valid_id(to):
            _err(self, "field 'to' invalid", 400); return
        if tt is None:
            _err(self, "thread_type invalid", 400); return
        if emoji not in REACTIONS:
            _err(self, "emoji must be one of the 14 supported", 400); return
        client = self._client_or_503()
        if client is None:
            return
        from zlapi.models import ThreadType
        thread = ThreadType.USER if tt == "user" else ThreadType.GROUP
        # messageObject needs msgId/cliMsgId for the reaction payload.
        msg_obj = {"msgId": str(msg_id), "cliMsgId": str(cli_msg_id)}
        client.sendReaction(msg_obj, emoji, to, thread)
        _json(self, {"ok": True})

    def _h_recall(self, body):
        msg_id = body.get("msg_id")
        cli_msg_id = body.get("cli_msg_id", "")
        to = body.get("to")
        tt = _thread_type(body.get("thread_type"))
        if not _valid_id(str(msg_id)):
            _err(self, "msg_id invalid", 400); return
        if not _valid_id(to):
            _err(self, "field 'to' invalid", 400); return
        if tt is None:
            _err(self, "thread_type invalid", 400); return
        client = self._client_or_503()
        if client is None:
            return
        from zlapi.models import ThreadType
        thread = ThreadType.USER if tt == "user" else ThreadType.GROUP
        client.undoMessage(str(msg_id), str(cli_msg_id), to, thread)
        _json(self, {"ok": True})

    def _h_send_image(self, body):
        to = body.get("to"); tt = _thread_type(body.get("thread_type"))
        fpath = _safe_path(body.get("file_path"), ALLOWED_IMG_EXT)
        caption = body.get("caption") or ""
        if not _valid_id(to) or tt is None:
            _err(self, "to/thread_type invalid", 400); return
        if fpath is None:
            _err(self, "file_path invalid or outside $BOOM_HOME", 400); return
        if not isinstance(caption, str) or len(caption) > 1000:
            _err(self, "caption too long", 400); return
        client = self._client_or_503()
        if client is None:
            return
        from zlapi.models import ThreadType, Message
        thread = ThreadType.USER if tt == "user" else ThreadType.GROUP
        msg = Message(text=caption) if caption else None
        client.sendLocalImage(fpath, to, thread, message=msg)
        _json(self, {"ok": True})

    def _h_send_file(self, body):
        to = body.get("to"); tt = _thread_type(body.get("thread_type"))
        fpath = _safe_path(body.get("file_path"), ALLOWED_FILE_EXT)
        if not _valid_id(to) or tt is None:
            _err(self, "to/thread_type invalid", 400); return
        if fpath is None:
            _err(self, "file_path invalid or outside $BOOM_HOME", 400); return
        client = self._client_or_503()
        if client is None:
            return
        from zlapi.models import ThreadType
        thread = ThreadType.USER if tt == "user" else ThreadType.GROUP
        client.uploadAttachment([fpath], to, thread)
        _json(self, {"ok": True})

    def _h_send_voice(self, body):
        to = body.get("to"); tt = _thread_type(body.get("thread_type"))
        fpath = _safe_path(body.get("file_path"), ALLOWED_VOICE_EXT)
        if not _valid_id(to) or tt is None:
            _err(self, "to/thread_type invalid", 400); return
        if fpath is None:
            _err(self, "file_path must be .m4a inside $BOOM_HOME", 400); return
        client = self._client_or_503()
        if client is None:
            return
        from zlapi.models import ThreadType
        thread = ThreadType.USER if tt == "user" else ThreadType.GROUP
        size = os.path.getsize(fpath)
        # uploadVoice expects a local path in this zlapi version.
        try:
            client.uploadVoice(fpath, to, thread)
        except TypeError:
            # Fallback: some versions take (filePath, thread_id, thread_type).
            client.uploadVoice(fpath, to, thread)
        _json(self, {"ok": True, "size": size})

    def _h_send_sticker(self, body):
        to = body.get("to"); tt = _thread_type(body.get("thread_type"))
        keyword = body.get("keyword", "")
        if not _valid_id(to) or tt is None:
            _err(self, "to/thread_type invalid", 400); return
        if not isinstance(keyword, str) or not keyword.strip() or len(keyword) > 50:
            _err(self, "keyword invalid", 400); return
        client = self._client_or_503()
        if client is None:
            return
        from zlapi.models import ThreadType
        thread = ThreadType.USER if tt == "user" else ThreadType.GROUP
        results = client.searchSticker(keyword.strip(), limit=5)
        sticker = _pick_sticker(results)
        if sticker is None:
            _err(self, "no sticker found", 404); return
        stype, sid, cid = sticker
        client.sendSticker(stype, sid, cid, to, thread)
        _json(self, {"ok": True, "sticker": {"id": sid, "cate": cid}})

    def _h_group_rename(self, body):
        group_id = body.get("group_id"); name = body.get("name", "")
        if not _valid_id(group_id):
            _err(self, "group_id invalid", 400); return
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_LEN:
            _err(self, "name invalid", 400); return
        client = self._client_or_503()
        if client is None:
            return
        client.changeGroupName(name.strip(), group_id)
        _json(self, {"ok": True})

    def _h_group_avatar(self, body):
        group_id = body.get("group_id")
        fpath = _safe_path(body.get("file_path"), ALLOWED_IMG_EXT)
        if not _valid_id(group_id):
            _err(self, "group_id invalid", 400); return
        if fpath is None:
            _err(self, "file_path invalid", 400); return
        client = self._client_or_503()
        if client is None:
            return
        client.changeGroupAvatar(fpath, group_id)
        _json(self, {"ok": True})

    def _h_group_add_deputy(self, body):
        group_id = body.get("group_id"); uid = body.get("uid")
        if not _valid_id(group_id) or not _valid_id(str(uid)):
            _err(self, "group_id/uid invalid", 400); return
        client = self._client_or_503()
        if client is None:
            return
        client.addGroupAdmins([str(uid)], group_id)
        _json(self, {"ok": True})

    def _h_group_kick(self, body):
        group_id = body.get("group_id"); uid = body.get("uid")
        if not _valid_id(group_id) or not _valid_id(str(uid)):
            _err(self, "group_id/uid invalid", 400); return
        client = self._client_or_503()
        if client is None:
            return
        client.kickUsersInGroup([str(uid)], group_id)
        _json(self, {"ok": True})

    def _h_group_block(self, body):
        group_id = body.get("group_id"); uid = body.get("uid")
        if not _valid_id(group_id) or not _valid_id(str(uid)):
            _err(self, "group_id/uid invalid", 400); return
        client = self._client_or_503()
        if client is None:
            return
        client.blockUsersInGroup([str(uid)], group_id)
        _json(self, {"ok": True})

    def _h_find_user(self, body):
        phone = body.get("phone", "")
        if not _valid_phone(phone):
            _err(self, "phone invalid", 400); return
        client = self._client_or_503()
        if client is None:
            return
        info = client.fetchPhoneNumber(phone)
        _json(self, {"result": _extract_profile(info)})

    def _qr_profile(self, uid, method="qr"):
        """Shared lookup core for /lookup-qr (/qr-lookup) and /qr-lookup-image.

        Even for users with hidden profiles, Zalo's getprofiles API usually
        returns at least the UID (and often name/avatar, since the same API
        renders group member lists). Fields the user hid come back empty —
        reported explicitly via `visibility`; this cannot be bypassed.
        """
        client = self._client_or_503()
        if client is None:
            return
        try:
            info = client.fetchUserInfo(uid)
            profile = _extract_profile(info)
            if not profile.get("uid"):
                _err(self, "no profile found for this QR", 404)
                return
            _json(self, {"ok": True, "method": method, "uid": uid,
                         "profile": profile,
                         "visibility": _visibility_report(profile)})
        except Exception:
            log.error("qr lookup failed")
            _err(self, "lookup failed", 502)

    def _h_lookup_phone(self, body):
        """Look up a user by phone number — no friending needed.

        Fallback chain:
          1. fetchPhoneNumber (Zalo's phone-search API — works when the
             target allows "find me by phone" in privacy settings).
        If Zalo blocks it (user hid phone search), the phone number is
        unresolvable server-side and cannot be bypassed; the response says
        so explicitly with the attempt log.
        """
        phone = body.get("phone", "")
        if not _valid_phone(phone):
            _err(self, "phone invalid", 400); return
        if not _lookup_allowed():
            _err(self, "rate limited, try again later", 429); return
        client = self._client_or_503()
        if client is None:
            return
        attempts = []
        try:
            info = client.fetchPhoneNumber(phone)
            profile = _extract_profile(info)
            if profile.get("uid"):
                _json(self, {"ok": True, "method": "phone_number",
                             "profile": profile,
                             "visibility": _visibility_report(profile)})
                return
            attempts.append("phone_number: empty result")
        except Exception as e:
            attempts.append("phone_number: " + type(e).__name__)
        _json(self, {
            "ok": False,
            "error": "phone lookup blocked or not found",
            "hint": ("The number is not searchable: the owner may have "
                     "disabled 'find by phone' in Zalo privacy settings, or "
                     "the number is not on Zalo. Zalo enforces this "
                     "server-side and it cannot be bypassed. If you have "
                     "their QR code, try POST /lookup-qr instead."),
            "attempts": attempts,
        }, 404)

    def _h_qr_lookup(self, body):
        """Decode a Zalo QR payload string -> UID -> profile, no friending."""
        qr_data = body.get("qr_data", "")
        if not isinstance(qr_data, str) or not qr_data.strip() \
                or len(qr_data) > 2048:
            _err(self, "qr_data invalid", 400); return
        if not _lookup_allowed():
            _err(self, "rate limited, try again later", 429); return
        uid = _uid_from_qr_data(qr_data)
        if uid is None:
            _err(self, "could not extract UID from QR data", 400); return
        self._qr_profile(uid, method="qr")

    def _h_qr_lookup_image(self, body):
        """Decode QR from an uploaded image -> UID -> profile, no friending."""
        fpath = _safe_path(body.get("file_path"), ALLOWED_QR_IMG_EXT)
        if fpath is None:
            _err(self, "file_path invalid or outside $BOOM_HOME", 400); return
        if not _lookup_allowed():
            _err(self, "rate limited, try again later", 429); return
        payloads = _decode_qr_image(fpath)
        if payloads is None:
            _err(self, "QR decode unavailable: pip install pyzbar Pillow "
                       "(plus system libzbar0, e.g. apt install libzbar0)",
                 501); return
        if not payloads:
            _err(self, "no QR code found in image", 404); return
        uid = None
        for p in payloads:
            uid = _uid_from_qr_data(p)
            if uid:
                break
        if uid is None:
            _err(self, "no Zalo UID in QR image", 400); return
        self._qr_profile(uid)


# ---------------------------------------------------------------------------
# Extraction helpers (never log raw payloads)
# ---------------------------------------------------------------------------

def _extract_members(info):
    """Normalize fetchGroupInfo result to [{uid, name, avatar}]."""
    members = []
    try:
        raw = info.get("members") if isinstance(info, dict) else getattr(info, "members", [])
        for m in raw or []:
            if isinstance(m, dict):
                uid = str(m.get("id") or m.get("uid") or "")
                name = str(m.get("display_name") or m.get("name") or "")
                avatar = str(m.get("avatar") or "")
            else:
                uid = str(getattr(m, "uid", "") or getattr(m, "id", ""))
                name = str(getattr(m, "display_name", "") or getattr(m, "name", ""))
                avatar = str(getattr(m, "avatar", ""))
            if uid:
                members.append({"uid": uid, "name": name, "avatar": avatar})
    except Exception:
        pass
    return members


def _extract_profile(info):
    try:
        if isinstance(info, dict):
            d = info
            get = d.get
        else:
            get = lambda k, default="": getattr(info, k, default)
        return {
            "uid": str(get("uid", "") or get("id", "")),
            "name": str(get("display_name", "") or get("name", "")),
            "avatar": str(get("avatar", "")),
            "phone": str(get("phone", "") or get("phone_number", "")),
        }
    except Exception:
        return {}


def _visibility_report(profile):
    """Which profile fields Zalo actually returned vs hid.

    Zalo enforces privacy server-side: hidden fields come back empty and
    cannot be bypassed. This report makes that explicit per lookup.
    """
    return {f: bool(profile.get(f)) for f in ("uid", "name", "avatar", "phone")}


def _pick_sticker(results):
    """Pick the first usable sticker from searchSticker results."""
    try:
        items = results.get("stickers") if isinstance(results, dict) else results
        if not items:
            return None
        s = items[0]
        if isinstance(s, dict):
            stype = s.get("stickerType", 1)
            sid = s.get("id") or s.get("stickerId")
            cid = s.get("cateId") or s.get("cate_id")
        else:
            stype = getattr(s, "stickerType", 1)
            sid = getattr(s, "id", None) or getattr(s, "stickerId", None)
            cid = getattr(s, "cateId", None) or getattr(s, "cate_id", None)
        if sid and cid:
            return (stype, sid, cid)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    _ensure_state_dir()
    try:
        import zlapi  # noqa: F401
    except ImportError:
        print("Missing dependency: run `pip install -r requirements.txt` first.",
              file=sys.stderr)
        sys.exit(1)

    threading.Thread(target=login_worker, name="zalo-login", daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log.info("Boom Zalo Personal bridge listening on 127.0.0.1:%d", PORT)
    log.info("Scan the QR at http://127.0.0.1:%d/qr (or GET /qr) to log in.", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
