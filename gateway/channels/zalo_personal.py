"""Zalo Personal adapter: drive a personal Zalo account via the `zlapi` library.

.. warning::

    **UNOFFICIAL API — USE AT YOUR OWN RISK.** `zlapi`
    (https://github.com/tuyenhuy026/zlapi) is *not* an official Zalo API: it
    imitates the Zalo web client, which may violate Zalo's Terms of Service.
    Zalo can and does lock/ban accounts that automate this way. **Only use a
    throwaway/secondary account**, never your main one. This adapter exists
    for personal-automation convenience, not for production use.

Design notes:

- `zlapi` is an *optional* dependency. It is imported lazily inside the
  methods that need it, so `import gateway.channels.zalo_personal` never
  fails on a machine without it. When a method needs zlapi and it is not
  installed, it raises ``RuntimeError("Zalo Personal requires 'zlapi': pip
  install zlapi")``.
- Security: the password is used once, in-memory, for the initial login and
  is **never logged and never stored**. Only the resulting session cookies
  (plus the phone number and a random IMEI) are persisted to
  ``~/.boom/zalo_personal_session.json`` (mode 0600). Subsequent sends
  rebuild the client from those cookies, without the password.
- The interface mirrors the Zalo OA adapter (same :class:`ChannelAdapter`
  base): ``async send_text(target, text) -> bool`` and
  ``can_deliver() -> bool``, plus the account-management helpers
  ``login`` / ``logout`` / ``is_logged_in`` / ``get_masked_phone``.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any, Dict, Optional

from gateway.channel import ChannelAdapter

# Exact message the task mandates when zlapi is missing.
ZLAPI_MISSING_MESSAGE = "Zalo Personal requires 'zlapi': pip install zlapi"

SESSION_FILENAME = "zalo_personal_session.json"


def _require_zlapi() -> Any:
    """Import zlapi lazily; raise the mandated RuntimeError when missing."""
    try:
        from zlapi import ZaloAPI  # type: ignore
    except ImportError as exc:
        raise RuntimeError(ZLAPI_MISSING_MESSAGE) from exc
    return ZaloAPI


def _session_path() -> Path:
    """Session file location: ~/.boom/zalo_personal_session.json."""
    return Path.home() / ".boom" / SESSION_FILENAME


def _mask_phone(phone: str) -> str:
    """Mask a phone number for display, e.g. '0912345678' -> '09***5678'."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 6:
        return "***" if not digits else "*" * len(digits)
    return f"{digits[:2]}***{digits[-4:]}"


def _random_imei() -> str:
    """Generate a random 16-digit numeric IMEI-like device identifier."""
    return "".join(str(secrets.randbelow(10)) for _ in range(16))


def _extract_cookies(client: Any) -> Dict[str, str]:
    """Best-effort extraction of the session cookies from a zlapi client.

    zlapi keeps the login state in an internal requests session; attribute
    names may vary between versions, so try the known shapes without raising.
    """
    try:
        session = getattr(client, "session", None)
        if session is None and callable(getattr(client, "getSession", None)):
            session = client.getSession()  # type: ignore
        jar = getattr(session, "cookies", None) if session is not None else None
        if jar is not None:
            if hasattr(jar, "get_dict"):
                data = jar.get_dict()
            else:
                data = dict(jar)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        direct = getattr(client, "cookies", None)
        if isinstance(direct, dict):
            return {str(k): str(v) for k, v in direct.items()}
    except Exception:
        pass
    return {}


def _save_session(phone: str, imei: str, cookies: Dict[str, str]) -> None:
    """Persist the login session (cookies only — never the password).

    The file is written with mode 0600; the write is done to a temp file and
    renamed so a partial write can never corrupt the previous session.
    """
    path = _session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "phone": phone,
        "phone_masked": _mask_phone(phone),
        "imei": imei,
        "session_cookies": cookies,
    }
    tmp = path.with_suffix(".tmp")
    # 0600 from creation: use os.open so the file is never world-readable.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    # Belt and braces: ensure the final mode is 0600 even if the umask/path
    # did something unexpected.
    os.chmod(path, 0o600)


def _load_session() -> Optional[Dict[str, Any]]:
    """Read the persisted session; None when absent or unreadable."""
    path = _session_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _thread_type(default: str = "USER") -> Any:
    """Resolve the thread-type enum for a 1:1 user chat.

    zlapi's sendMessage takes a thread type; prefer the real enum from
    zlapi.models, fall back to the plain string so a version without the
    enum still works.
    """
    try:
        from zlapi.models import ThreadType  # type: ignore
        return getattr(ThreadType, "USER", default)
    except ImportError:
        return default


class ZaloPersonalAdapter(ChannelAdapter):
    """Channel adapter for a personal Zalo account via the `zlapi` library.

    See the module docstring for the unofficial-API warning and the security
    model (password is transient, only cookies are stored).
    """

    name = "zalo_personal"

    # -- account management ------------------------------------------------

    def login(self, phone: str, password: str) -> Dict[str, Any]:
        """Log in with a phone number and password, then persist the session.

        The password is used only for this call and never stored or logged.
        On success returns ``{"ok": True, "phone": <masked>}``. On auth or
        network failure raises RuntimeError (never a fabricated success).
        """
        ZaloAPI = _require_zlapi()
        imei = _random_imei()
        # NOTE: the password below is in-memory only — _save_session() stores
        # cookies, never the password.
        try:
            client = ZaloAPI(phone, password, imei=imei)
        except Exception as exc:
            raise RuntimeError(f"Zalo Personal login failed: {exc}") from exc
        cookies = _extract_cookies(client)
        if not cookies:
            raise RuntimeError(
                "Zalo Personal login failed: no session cookies were "
                "captured after login (account may require extra verification)"
            )
        _save_session(phone, imei, cookies)
        print(f"[zalo_personal] logged in as {_mask_phone(phone)}", flush=True)
        return {"ok": True, "phone": _mask_phone(phone)}

    def logout(self) -> None:
        """Delete the persisted session; the account stays untouched."""
        try:
            _session_path().unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeError(f"Zalo Personal logout failed: {exc}") from exc

    def is_logged_in(self) -> bool:
        """True when a usable session file exists on disk."""
        data = _load_session()
        if not data:
            return False
        return bool(data.get("session_cookies")) and bool(data.get("phone"))

    def get_masked_phone(self) -> str:
        """Return the stored phone number masked, e.g. '09***5678'.

        Returns "" when no session exists.
        """
        data = _load_session()
        if not data:
            return ""
        masked = data.get("phone_masked")
        if isinstance(masked, str) and masked:
            return masked
        phone = data.get("phone")
        return _mask_phone(str(phone)) if phone else ""

    # -- reply path --------------------------------------------------------

    def can_deliver(self) -> bool:
        """True when a stored session exists to send with."""
        return self.is_logged_in()

    def _build_client_from_session(self, session: Dict[str, Any]) -> Any:
        """Rebuild a zlapi client from stored cookies (no password needed)."""
        ZaloAPI = _require_zlapi()
        phone = str(session.get("phone", ""))
        imei = str(session.get("imei", "")) or _random_imei()
        cookies = session.get("session_cookies") or {}
        try:
            return ZaloAPI(phone, None, imei=imei, session_cookies=cookies)
        except TypeError:
            # Older zlapi versions may not accept session_cookies — fall back
            # to the plain constructor so the caller's exception path reports
            # the real failure honestly.
            return ZaloAPI(phone, None, imei=imei)

    async def send_text(self, target: str, text: str) -> bool:
        """Send plain text to a user/group `target` id via the saved session.

        Returns True when zlapi accepted the message, False when there is no
        session or the send raised (never fabricates success).
        """
        if not text:
            return False
        session = _load_session() if self.is_logged_in() else None
        if not session:
            return False
        try:
            client = self._build_client_from_session(session)
        except RuntimeError:
            # zlapi not installed — the mandated error, surfaced honestly.
            raise
        except Exception as exc:
            print(f"[zalo_personal] -> client build failed: {exc}", flush=True)
            return False
        thread_type = _thread_type()
        try:
            # zlapi's sync client blocks; keep the event loop free.
            await asyncio.to_thread(client.sendMessage, text, str(target),
                                   thread_type)
        except Exception as exc:
            print(f"[zalo_personal] -> send failed: {exc}", flush=True)
            return False
        print(f"[zalo_personal] -> sent {len(text)} chars to {target}",
              flush=True)
        return True

    # -- ingress path -------------------------------------------------------
    # Inbound Zalo Personal traffic arrives through a bridge posting canonical
    # events; the base normalize() passes them through untouched.
