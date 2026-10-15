"""Native Gmail tool for the Boom agent: search, read, draft, and send mail.

Authentication is OAuth2 desktop flow with a cached token — lazy, so importing
this module never touches the network or the browser:

1. First call: looks for a cached token at ``~/.boom/gmail_token.json``
   (or ``$BOOM_HOME/gmail_token.json``). If the token is expired but has a
   refresh token, it is refreshed silently.
2. No usable token: runs ``google.auth.oauthlib.flow.InstalledAppFlow`` which
   opens the browser once. Client secrets come from
   ``~/.boom/gmail_client_secret.json`` or the ``BOOM_GMAIL_CLIENT_ID`` /
   ``BOOM_GMAIL_CLIENT_SECRET`` environment variables.

Draft-not-send: ``gmail_draft`` builds a *local* draft (no Gmail API call at
all) and returns a ``draft_id`` plus a preview. ``gmail_send`` takes that
``draft_id`` and performs the real send — it is intentionally NOT in the
approval gate's safe list, so the agent is always asked before anything
leaves the outbox.

If ``google-auth-oauthlib`` is not installed, every function returns a clear
``{"error": ...}`` telling the operator to run ``pip install boom[gmail]``.
"""

import base64
import json
import os
import secrets
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

API_BASE = "https://gmail.googleapis.com/gmail/v1"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
TOKEN_FILENAME = "gmail_token.json"
CLIENT_SECRET_FILENAME = "gmail_client_secret.json"

# In-memory draft store: draft_id -> {to, subject, body, created_at}.
# Drafts live here, NOT in Gmail, so gmail_draft has zero external side effects.
_DRAFTS: Dict[str, Dict[str, Any]] = {}
_DRAFT_TTL_SECONDS = 3600


def _boom_home() -> Path:
    return Path(os.environ.get("BOOM_HOME", str(Path.home() / ".boom")))


def _missing_dep_error() -> Dict[str, str]:
    return {
        "error": (
            "Gmail OAuth libraries are not installed. "
            "Run: pip install 'boom[gmail]'  (needs google-auth-oauthlib)"
        )
    }


def _get_credentials():
    """Return valid OAuth2 credentials, running the browser flow only if needed.

    Never called at import time — only when a gmail_* function actually runs.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        return None

    home = _boom_home()
    token_path = home / TOKEN_FILENAME
    creds = None

    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception:
            creds = None  # corrupt cache: fall through to a fresh flow

    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception:
            creds = None  # refresh failed: fall through to a fresh flow

    # Fresh OAuth2 desktop flow. Client secrets from file or env.
    client_secret_path = home / CLIENT_SECRET_FILENAME
    client_config = None
    if client_secret_path.exists():
        try:
            client_config = json.loads(client_secret_path.read_text(encoding="utf-8"))
        except Exception:
            client_config = None
    if client_config is None:
        client_id = os.environ.get("BOOM_GMAIL_CLIENT_ID", "").strip()
        client_secret = os.environ.get("BOOM_GMAIL_CLIENT_SECRET", "").strip()
        if client_id and client_secret:
            client_config = {
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
    if client_config is None:
        raise RuntimeError(
            "No Gmail OAuth client configured. Put your client_secret JSON at "
            f"{client_secret_path} or set BOOM_GMAIL_CLIENT_ID / BOOM_GMAIL_CLIENT_SECRET."
        )

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    home.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        pass
    return creds


def _api(path: str, method: str = "GET", json_body: Optional[Dict[str, Any]] = None,
         params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Call the Gmail REST API. Returns parsed JSON or {"error": ...}."""
    try:
        creds = _get_credentials()
    except ImportError:
        return _missing_dep_error()
    except RuntimeError as e:
        return {"error": str(e)}
    if creds is None:  # google-auth-oauthlib missing
        return _missing_dep_error()

    # Refresh the access token if needed (cheap, local).
    try:
        from google.auth.transport.requests import Request
        if getattr(creds, "expired", False):
            creds.refresh(Request())
    except Exception as e:
        return {"error": f"Gmail token refresh failed: {e}"}

    headers = {"Authorization": f"Bearer {creds.token}"}
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.request(
                method, f"{API_BASE}{path}",
                headers=headers, json=json_body, params=params,
            )
        if resp.status_code in (401, 403):
            return {"error": f"Gmail API denied (HTTP {resp.status_code}): check OAuth scopes."}
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": f"Gmail API call failed: {e}"}


def _decode_body(payload: Dict[str, Any]) -> str:
    """Best-effort plain-text extraction from a Gmail message payload."""
    def b64d(data: str) -> bytes:
        s = (data or "").replace("-", "+").replace("_", "/")
        s += "=" * (-len(s) % 4)
        return base64.b64decode(s)

    text_parts: List[str] = []

    def walk(part: Dict[str, Any]) -> None:
        mime = (part.get("mimeType") or "")
        body = part.get("body", {}) or {}
        data = body.get("data")
        if data and mime.startswith("text/plain"):
            try:
                text_parts.append(b64d(data).decode("utf-8", errors="replace"))
            except Exception:
                pass
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload or {})
    return "\n".join(text_parts).strip()[:20000]


def gmail_search(query: str, max_results: int = 10) -> Dict[str, Any]:
    """Search Gmail. Returns a list of {id, snippet} — no bodies fetched."""
    if not query or not query.strip():
        return {"error": "query must be a non-empty string."}
    max_results = max(1, min(int(max_results or 10), 50))
    data = _api("/users/me/messages", params={"q": query.strip(), "maxResults": max_results})
    if "error" in data:
        return data
    messages = data.get("messages", []) or []
    return {
        "query": query.strip(),
        "count": len(messages),
        "messages": [{"id": m.get("id"), "threadId": m.get("threadId")} for m in messages],
    }


def gmail_read(message_id: str) -> Dict[str, Any]:
    """Read one Gmail message: headers, snippet, and plain-text body."""
    if not message_id or not message_id.strip():
        return {"error": "message_id must be a non-empty string."}
    data = _api(f"/users/me/messages/{message_id.strip()}", params={"format": "full"})
    if "error" in data:
        return data
    payload = data.get("payload", {}) or {}
    headers = {h.get("name", "").lower(): h.get("value", "")
               for h in payload.get("headers", []) or []}
    return {
        "id": data.get("id"),
        "threadId": data.get("threadId"),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "snippet": data.get("snippet", ""),
        "body": _decode_body(payload),
    }


def gmail_draft(to: str, subject: str, body: str) -> Dict[str, Any]:
    """Create a LOCAL draft (no Gmail API call). Returns draft_id + preview.

    Nothing is sent and nothing touches Gmail — the draft lives in memory until
    gmail_send(draft_id) is called and approved.
    """
    to = (to or "").strip()
    subject = (subject or "").strip()
    body = (body or "").strip()
    if not to:
        return {"error": "'to' must be a non-empty email address."}
    if "@" not in to:
        return {"error": f"'{to}' does not look like an email address."}
    if not body:
        return {"error": "'body' must be non-empty."}

    # Expire old drafts so the store cannot grow forever.
    now = time.time()
    for did in [d for d, v in _DRAFTS.items() if now - v["created_at"] > _DRAFT_TTL_SECONDS]:
        del _DRAFTS[did]

    draft_id = f"draft_{secrets.token_hex(8)}"
    _DRAFTS[draft_id] = {"to": to, "subject": subject, "body": body, "created_at": now}
    preview = body if len(body) <= 500 else body[:500] + "… [truncated]"
    return {
        "draft_id": draft_id,
        "to": to,
        "subject": subject,
        "preview": preview,
        "note": "Local draft only — nothing sent. Call gmail_send with this draft_id to send (requires approval).",
    }


def gmail_send(draft_id: str) -> Dict[str, Any]:
    """Send a previously created draft. REQUIRES approval (not a safe tool).

    The approval gate prompts before this runs. Without approval the draft
    stays local and nothing is sent.
    """
    draft_id = (draft_id or "").strip()
    draft = _DRAFTS.get(draft_id)
    if not draft:
        return {"error": f"Unknown or expired draft_id '{draft_id}'. Create one with gmail_draft first."}

    msg = EmailMessage()
    msg["To"] = draft["to"]
    msg["Subject"] = draft["subject"] or "(no subject)"
    msg.set_content(draft["body"])
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    data = _api("/users/me/messages/send", method="POST", json_body={"raw": raw})
    if "error" in data:
        return data
    del _DRAFTS[draft_id]  # sent: draft is consumed
    return {
        "sent": True,
        "gmail_id": data.get("id"),
        "threadId": data.get("threadId"),
        "to": draft["to"],
    }


def gmail_discard(draft_id: str) -> Dict[str, Any]:
    """Discard a local draft without sending. Safe — no side effects."""
    draft_id = (draft_id or "").strip()
    if draft_id in _DRAFTS:
        del _DRAFTS[draft_id]
        return {"discarded": True, "draft_id": draft_id}
    return {"error": f"Unknown draft_id '{draft_id}'."}
