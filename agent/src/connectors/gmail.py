"""Gmail connector for the Boom agent: search, read, and send mail.

Credential: a user OAuth 2.0 access token in ``BOOM_GMAIL_ACCESS_TOKEN``.
This module never performs the OAuth flow itself — the operator provisions the
token (e.g. via the Google OAuth Playground, ``gcloud auth print-access-token``,
or their own OAuth client) and exports it before starting the agent::

    export BOOM_GMAIL_ACCESS_TOKEN="<access token>"

Required OAuth scope: ``https://www.googleapis.com/auth/gmail.modify``
(``gmail.readonly`` is enough if sending is never used).

Only stdlib + httpx. Every tool returns ``{"error": ...}`` on failure and
never raises to the caller; a success is only reported when the Gmail API
actually confirmed it.
"""

import base64
import binascii
import contextlib
from email.message import EmailMessage
from html.parser import HTMLParser
from html import unescape as _html_unescape
from typing import Any, Dict, Iterator, List, Optional

import httpx

from connectors.base import Connector, _schema, sync_bearer_request
from connectors.credentials import get_token_for

API_BASE = "https://gmail.googleapis.com/gmail/v1"
# Scope needed: https://www.googleapis.com/auth/gmail.modify
# (gmail.readonly suffices when gmail_send is never used).


# ------------------------------------------------------------------ helpers


def _b64decode(data: str) -> bytes:
    """Decode Gmail's base64url (padding is usually omitted)."""
    s = (data or "").strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)


def _decode_text(data: str) -> str:
    """Best-effort bytes -> str for a message body part."""
    raw = _b64decode(data)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


class _TextExtractor(HTMLParser):
    """Collect visible text from HTML, dropping tags/scripts/styles."""

    def __init__(self) -> None:
        super().__init__()  # convert_charrefs=True: entities arrive decoded in handle_data
        self._chunks: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._chunks.append(data)

    def text(self) -> str:
        return _html_unescape(" ".join("".join(self._chunks).split()))


def _strip_html(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


def _headers(payload: Dict[str, Any]) -> Dict[str, str]:
    """Payload headers as {lowercase-name: value}."""
    out: Dict[str, str] = {}
    for header in payload.get("headers", []) or []:
        name = str(header.get("name", "")).lower()
        if name and name not in out:
            out[name] = str(header.get("value", ""))
    return out


def _walk_parts(payload: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """Yield every MIME part depth-first (nested multiparts included)."""
    stack = list(payload.get("parts", []) or [])
    while stack:
        part = stack.pop(0)
        yield part
        stack.extend(part.get("parts", []) or [])


def _extract_body(payload: Dict[str, Any]) -> tuple:
    """(text, mime_type). Prefers text/plain; falls back to text/html stripped.

    Returns ("", "") when the message has no decodable text part.
    """
    mime = str(payload.get("mimeType", "") or "").lower()
    data = (payload.get("body", {}) or {}).get("data") or ""
    if data and mime.startswith("text/"):
        try:
            text = _decode_text(data)
        except (binascii.Error, ValueError):
            return "", ""
        return (text, "text/plain") if mime == "text/plain" else (_strip_html(text), mime)

    plain: Optional[str] = None
    html: Optional[str] = None
    for part in _walk_parts(payload):
        pmime = str(part.get("mimeType", "") or "").lower()
        pdata = (part.get("body", {}) or {}).get("data") or ""
        if not pdata or not pmime.startswith("text/"):
            continue
        try:
            text = _decode_text(pdata)
        except (binascii.Error, ValueError):
            continue
        if pmime == "text/plain" and plain is None:
            plain = text
        elif pmime == "text/html" and html is None:
            html = _strip_html(text)
    if plain is not None:
        return plain, "text/plain"
    if html is not None:
        return html, "text/html"
    return "", ""


# ------------------------------------------------------------------ connector


class GmailConnector(Connector):
    """Gmail over the REST API, Bearer token from BOOM_GMAIL_ACCESS_TOKEN."""

    name = "gmail"

    # Shared sync Bearer envelope (connectors.base.sync_bearer_request) reads these.
    TOKEN_ENV = "BOOM_GMAIL_ACCESS_TOKEN"
    SERVICE = "Gmail"
    TOKEN_HINT = "export a user OAuth 2.0 access token to use Gmail."
    EXTRA_HEADERS: Dict[str, str] = {}
    STRICT_DICT_RESPONSE = True

    def _api_error(self, resp: httpx.Response) -> str:
        """Human-readable error from a failed Gmail API response."""
        try:
            body = resp.json()
        except ValueError:
            body = None
        detail = ""
        if isinstance(body, dict):
            err = body.get("error", {})
            if isinstance(err, dict):
                detail = str(err.get("message", "") or "")
        if not detail:
            detail = (resp.text or "").strip()[:300]
        return f"Gmail API error {resp.status_code}: {detail or 'no detail'}"

    def __init__(
        self,
        token: Optional[str] = None,
        client: Optional[httpx.Client] = None,
        api_base: str = API_BASE,
        timeout: float = 30.0,
    ) -> None:
        # token=None means "read BOOM_GMAIL_ACCESS_TOKEN lazily at call time",
        # so an env change after import is still picked up. A non-None value
        # is an explicit override (used by tests with a fake transport).
        self._token_override = token
        self._injected_client = client
        self._api_base = (api_base or API_BASE).rstrip("/")
        self._timeout = timeout

    # -- config ------------------------------------------------------

    def _token(self) -> str:
        if self._token_override is not None:
            return self._token_override.strip()
        return get_token_for("gmail", "access_token")

    def status(self) -> Dict[str, Any]:
        if self._token():
            return {
                "connected": True,
                "detail": "BOOM_GMAIL_ACCESS_TOKEN is set; Gmail API ready.",
            }
        return {
            "connected": False,
            "detail": (
                "BOOM_GMAIL_ACCESS_TOKEN is not set: export a user OAuth 2.0 "
                "access token (scope gmail.modify) to enable Gmail."
            ),
        }

    # -- HTTP --------------------------------------------------------

    @contextlib.contextmanager
    def _session(self) -> Iterator[httpx.Client]:
        """Yield a client; an injected one is never closed by us."""
        if self._injected_client is not None:
            yield self._injected_client
        else:
            with httpx.Client(timeout=self._timeout) as client:
                yield client

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """One Gmail API call. Parsed JSON on success, {"error": ...} otherwise."""
        return sync_bearer_request(self, method, path, params, json_body)

    # -- tools -------------------------------------------------------

    def gmail_search(self, query: str, max_results: int = 10) -> Dict[str, Any]:
        """Search mail. Returns id/threadId/snippet/from/subject/date per hit."""
        if not (query or "").strip():
            return {"error": "gmail_search needs a query string."}
        try:
            limit = int(max_results)
        except (TypeError, ValueError):
            return {"error": "max_results must be an integer."}
        limit = max(1, min(limit, 100))

        data = self._request(
            "GET", "/users/me/messages",
            params={"q": query.strip(), "maxResults": limit},
        )
        if "error" in data:
            return data

        out: List[Dict[str, Any]] = []
        for msg in data.get("messages", []) or []:
            meta = self._request(
                "GET",
                f"/users/me/messages/{msg.get('id')}",
                params={
                    "format": "metadata",
                    "metadataHeaders": ["From", "Subject", "Date"],
                },
            )
            if "error" in meta:
                # The list call worked; keep the id and say what failed honestly.
                out.append({
                    "id": msg.get("id"),
                    "threadId": msg.get("threadId"),
                    "error": meta["error"],
                })
                continue
            headers = _headers(meta.get("payload", {}) or {})
            out.append({
                "id": meta.get("id"),
                "threadId": meta.get("threadId"),
                "snippet": meta.get("snippet", ""),
                "from": headers.get("from", ""),
                "subject": headers.get("subject", ""),
                "date": headers.get("date", ""),
            })
        return {
            "messages": out,
            "resultSizeEstimate": data.get("resultSizeEstimate", len(out)),
        }

    def gmail_read(self, message_id: str) -> Dict[str, Any]:
        """Read one message fully: headers plus decoded body text."""
        if not (message_id or "").strip():
            return {"error": "gmail_read needs a message_id."}
        data = self._request(
            "GET", f"/users/me/messages/{message_id.strip()}",
            params={"format": "full"},
        )
        if "error" in data:
            return data
        payload = data.get("payload", {}) or {}
        headers = _headers(payload)
        body, mime = _extract_body(payload)
        return {
            "id": data.get("id"),
            "threadId": data.get("threadId"),
            "labelIds": data.get("labelIds", []),
            "from": headers.get("from", ""),
            "subject": headers.get("subject", ""),
            "date": headers.get("date", ""),
            "snippet": data.get("snippet", ""),
            "body": body,
            "body_mime": mime,
        }

    def gmail_send(self, to: str, subject: str, body: str) -> Dict[str, Any]:
        """Send a plain-text email. Only reports sent when the API confirms."""
        to = (to or "").strip()
        subject = (subject or "").strip()
        if not to:
            return {"error": "gmail_send needs a recipient in 'to'."}
        if not subject:
            return {"error": "gmail_send needs a subject."}
        if not (body or "").strip():
            return {"error": "gmail_send needs a non-empty body."}

        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")

        data = self._request("POST", "/users/me/messages/send", json_body={"raw": raw})
        if "error" in data:
            return data
        return {
            "sent": True,
            "id": data.get("id"),
            "threadId": data.get("threadId"),
        }

    # -- contract ----------------------------------------------------

    def build_tools(self, env: Any = None) -> Dict[str, Dict[str, Any]]:
        _ = env  # accepted for environment.py's factory(self) convention; unused.
        return {
            "gmail_search": {
                "name": "gmail_search",
                "func": self.gmail_search,
                "description": (
                    "Search Gmail with the Gmail query syntax "
                    "(e.g. 'from:boss subject:report newer_than:7d'). Returns "
                    "id, threadId, snippet, from, subject and date per hit."
                ),
                "schema": _schema(
                    {
                        "query": {
                            "type": "string",
                            "description": "Gmail search query, e.g. 'is:unread from:acme'.",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Max hits (1-100, default 10).",
                        },
                    },
                    ["query"],
                ),
            },
            "gmail_read": {
                "name": "gmail_read",
                "func": self.gmail_read,
                "description": (
                    "Read one Gmail message fully by id: from, subject, date, "
                    "labels, snippet and decoded body text (text/plain "
                    "preferred, HTML stripped as fallback)."
                ),
                "schema": _schema(
                    {
                        "message_id": {
                            "type": "string",
                            "description": "Message id from gmail_search.",
                        },
                    },
                    ["message_id"],
                ),
            },
            "gmail_send": {
                "name": "gmail_send",
                "func": self.gmail_send,
                "description": (
                    "Send a plain-text email through Gmail. Reports sent only "
                    "when the Gmail API confirms the message."
                ),
                "schema": _schema(
                    {
                        "to": {
                            "type": "string",
                            "description": "Recipient email address.",
                        },
                        "subject": {"type": "string", "description": "Email subject."},
                        "body": {"type": "string", "description": "Plain-text body."},
                    },
                    ["to", "subject", "body"],
                ),
            },
        }


def build_tools(env: Any = None) -> Dict[str, Dict[str, Any]]:
    """Module-level factory for environment.py's tool-module convention."""
    return GmailConnector().build_tools(env)


# The connectors registry (connectors.registry.load_default_registry) picks up
# each connector module through this instance.
CONNECTOR = GmailConnector()
