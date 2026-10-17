# -*- coding: utf-8 -*-
"""Minimal Zalo Personal bridge for Boom.

Exposes a tiny local HTTP API (localhost only) so the Boom core can
send/receive messages through a personal Zalo account:

    GET  /status    -> {"status": "qr_pending"|"connected"|"error", ...}
    GET  /qr        -> PNG QR code to scan with the Zalo mobile app
    POST /send      -> {"to": "<user/group id>", "text": "...", "thread_type": "user"|"group"}
    GET  /messages  -> {"messages": [{id, thread_id, thread_type, author_id, text, ts}]}

Design goals: minimal and light. No WebSocket is kept open; incoming
messages are fetched with HTTP polling (every POLL_INTERVAL seconds).
Only text send/receive is implemented — nothing more.

Security notes:
  * Binds to 127.0.0.1 only. Never expose this port to a network.
  * Session cookies are stored with mode 0600 under $BOOM_HOME/zalo_personal/.
  * All request bodies are validated; inputs are length-capped.
  * Credentials never appear in logs.

This bridge uses the unofficial `zalo-api` PyPI package (reverse-engineered
Zalo web client). Zalo does not bless automation: use a SECONDARY account,
expect possible rate-limits or locks. You accept that risk.
"""

import base64
import hashlib
import json
import logging
import os
import queue
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("BOOM_ZALO_BRIDGE_PORT", "17890"))
POLL_INTERVAL = int(os.environ.get("BOOM_ZALO_POLL_INTERVAL", "5"))  # seconds
QR_TTL = 120  # QR code expires after 2 minutes
MSG_QUEUE_CAP = 500
MAX_TEXT_LEN = 4000
MAX_ID_LEN = 64

BOOM_HOME = os.environ.get(
    "BOOM_HOME", os.path.join(os.path.expanduser("~"), ".boom")
)
STATE_DIR = os.path.join(BOOM_HOME, "zalo_personal")
SESSION_PATH = os.path.join(STATE_DIR, "session.json")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [zalo-bridge] %(levelname)s %(message)s"
)
log = logging.getLogger("zalo-bridge")


# ---------------------------------------------------------------------------
# State (shared between threads)
# ---------------------------------------------------------------------------

class BridgeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "starting"  # starting|qr_pending|connected|error
        self.error = None
        self.qr_png = None          # raw PNG bytes of the current login QR
        self.qr_issued_at = 0.0
        self.client = None          # ZaloAPI instance once logged in
        self.self_id = None
        self.messages = queue.Queue()  # inbound normalized messages
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
        try:
            self.messages.put_nowait(msg)
        except queue.Full:
            try:
                self.messages.get_nowait()
            except queue.Empty:
                pass
            self.messages.put_nowait(msg)

    def drain_messages(self):
        out = []
        while True:
            try:
                out.append(self.messages.get_nowait())
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
# Zalo client
# ---------------------------------------------------------------------------

def _new_client(session_cookies=None, imei=None):
    from zlapi import ZaloAPI

    # Mutable holder so onMessage can read the own uid resolved after login.
    self_id = {"uid": None}

    class BoomZaloClient(ZaloAPI):
        def onMessage(self, mid=None, author_id=None, message=None,
                      message_object=None, thread_id=None, thread_type=None):
            try:
                # Ignore our own echoes.
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
            except Exception as e:  # never let a callback kill the loop
                log.warning("onMessage handler failed: %s", e)

    kwargs = dict(phone=None, password=None, auto_login=bool(session_cookies))
    if session_cookies:
        kwargs["session_cookies"] = session_cookies
    if imei:
        kwargs["imei"] = imei
    client = BoomZaloClient(**kwargs)
    return client, self_id


def _resolve_self_id(client, self_id):
    """Best-effort: fetch own uid so we can ignore message echoes."""
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
        log.info("QR code ready (%d bytes). Scan it with the Zalo mobile app.", len(png))
    except OSError as e:
        log.error("Failed to read generated QR: %s", e)


def login_worker():
    """Runs in a background thread: restore session or do QR login."""
    from zlapi import ZaloAPI  # noqa: F401  (import check)

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
            log.warning("Saved session invalid, falling back to QR login: %s", e)

    # Fresh QR login.
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
        # Login succeeded: resolve own uid + persist the session.
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
    """Polling listener in its own daemon thread (no persistent WebSocket)."""
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
# HTTP API (localhost only)
# ---------------------------------------------------------------------------

def _json(handler, obj, code=200):
    body = json.dumps(obj).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _valid_id(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_ID_LEN
        and all(c.isalnum() or c in "-_" for c in value)
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "BoomZaloBridge/1.0"

    def log_message(self, fmt, *args):  # quieter than default
        log.info("%s - %s", self.address_string(), fmt % args)

    def _read_json(self, max_bytes=65536):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > max_bytes:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/status":
            with STATE.lock:
                status, error, has_qr = STATE.status, STATE.error, STATE.qr_png is not None
            _json(self, {"status": status, "error": error, "qr_available": has_qr})
        elif path == "/qr":
            with STATE.lock:
                png, issued = STATE.qr_png, STATE.qr_issued_at
            if not png or (time.time() - issued) > QR_TTL:
                _json(self, {"error": "no valid QR code (login may have completed or expired)"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(png)
        elif path == "/messages":
            _json(self, {"messages": STATE.drain_messages()})
        else:
            _json(self, {"error": "not found"}, 404)

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/send":
            _json(self, {"error": "not found"}, 404)
            return
        body = self._read_json()
        if not isinstance(body, dict):
            _json(self, {"error": "invalid JSON body"}, 400)
            return
        to = body.get("to")
        text = body.get("text")
        thread_type = str(body.get("thread_type", "user")).lower()
        if not _valid_id(to):
            _json(self, {"error": "field 'to' must be a 1-64 char id (alnum, -, _)"}, 400)
            return
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_LEN:
            _json(self, {"error": "field 'text' must be 1-%d chars" % MAX_TEXT_LEN}, 400)
            return
        if thread_type not in ("user", "group"):
            _json(self, {"error": "thread_type must be 'user' or 'group'"}, 400)
            return
        client = STATE.client
        if client is None or STATE.status != "connected":
            _json(self, {"error": "not connected to Zalo"}, 503)
            return
        try:
            from zlapi.models import Message, ThreadType
            tt = ThreadType.USER if thread_type == "user" else ThreadType.GROUP
            client.sendMessage(Message(text=text), to, tt)
            _json(self, {"ok": True})
        except Exception as e:
            log.error("sendMessage failed: %s", type(e).__name__)
            _json(self, {"error": "send failed"}, 502)


def main():
    _ensure_state_dir()
    # Fail fast if the dependency is missing, with a helpful message.
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
