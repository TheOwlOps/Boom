"""Offline tests for the Zalo Personal adapter (gateway/channels/zalo_personal.py).

No network, no real zlapi, no real credentials:

- importing the adapter must NOT require zlapi (lazy import);
- login() without zlapi installed -> RuntimeError with the exact mandated
  message;
- session save/load round-trip with a mocked zlapi module (fake ZaloAPI);
- phone masking;
- can_deliver() False when not logged in; logout() removes the session;
- the persisted session file is mode 0600 and never contains the password;
- send_text() without a session -> False (no network attempted).

Run: python3 evals/test_zalo_personal.py (from the repo root, or anywhere —
the script inserts the repo root into sys.path itself).
"""

import asyncio
import json
import os
import stat
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # repo root -> `gateway` package

PASS = 0


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        raise AssertionError(f"FAIL {name} {extra}")


# ---------------------------------------------------------------- fixtures

EXPECTED_MESSAGE = "Zalo Personal requires 'zlapi': pip install zlapi"


def _isolated_home():
    """Point Path.home() at a fresh temp dir (POSIX: reads $HOME)."""
    tmp = tempfile.mkdtemp(prefix="zalo_personal_test_")
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = tmp
    return tmp, old_home


def _restore_home(old_home):
    if old_home is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = old_home


class _FakeCookies:
    def get_dict(self):
        return {"zpw_sek": "fake-secret", "zpsid": "fake-sid"}


class _FakeSession:
    cookies = _FakeCookies()


class _FakeZaloAPI:
    """Stand-in for zlapi.ZaloAPI: records constructor args, no network."""

    instances = []

    def __init__(self, phone, password, imei=None, session_cookies=None):
        self.phone = phone
        self.password = password
        self.imei = imei
        self.session_cookies = session_cookies
        self.session = _FakeSession()
        self.sent = []
        _FakeZaloAPI.instances.append(self)

    def sendMessage(self, message, thread_id, thread_type):
        self.sent.append((message, thread_id, thread_type))
        return {"ok": True}


def _install_fake_zlapi():
    """Inject a fake `zlapi` module into sys.modules (no pip package)."""
    mod = types.ModuleType("zlapi")
    mod.ZaloAPI = _FakeZaloAPI
    models = types.ModuleType("zlapi.models")
    thread_type = types.SimpleNamespace(USER="USER")
    models.ThreadType = thread_type
    mod.models = models
    sys.modules["zlapi"] = mod
    sys.modules["zlapi.models"] = models
    return mod


def _uninstall_fake_zlapi():
    for name in ("zlapi", "zlapi.models"):
        sys.modules.pop(name, None)


# ---------------------------------------------------------------- tests

# 1. Import works without zlapi installed (lazy import).
_uninstall_fake_zlapi()
assert "zlapi" not in sys.modules


def _stub_tools_ws_client():
    """Stub `tools.ws_client` (a discord-adapter dep living under agent/src).

    Importing `gateway.channels.zalo_personal` runs the `gateway.channels`
    package __init__, which imports every sibling adapter including discord.
    That import only needs `tools` on sys.path in a full dev checkout; for
    this offline test a stub keeps the import path canonical without
    dragging in unrelated deps.
    """
    pkg = types.ModuleType("tools")
    pkg.__path__ = []  # mark as a package
    sub = types.ModuleType("tools.ws_client")
    sub.WebSocket = type("WebSocket", (), {})
    pkg.ws_client = sub
    sys.modules.setdefault("tools", pkg)
    sys.modules.setdefault("tools.ws_client", sub)


try:
    import gateway.channels.zalo_personal as zp
except ModuleNotFoundError as exc:
    if "tools" not in str(exc):
        raise
    _stub_tools_ws_client()
    import gateway.channels.zalo_personal as zp  # noqa: E402
check("import-without-zlapi", True)
check("no-zlapi-in-sys-modules-after-import", "zlapi" not in sys.modules)

adapter = zp.ZaloPersonalAdapter()
check("adapter-name", adapter.name == "zalo_personal")

# 2. login() without zlapi -> RuntimeError with the exact message.
tmp_home, old_home = _isolated_home()
try:
    try:
        adapter.login("0912345678", "secret-password")
        raised = None
    except RuntimeError as exc:
        raised = exc
    check("login-without-zlapi-raises-RuntimeError", raised is not None)
    check("login-without-zlapi-message",
          raised is not None and str(raised) == EXPECTED_MESSAGE,
          f"got {raised!r}")
    check("login-without-zlapi-no-session-file",
          not (_session_path := zp._session_path()).exists())
finally:
    _restore_home(old_home)

# 3. Session save/load round-trip with mocked zlapi.
_FakeZaloAPI.instances.clear()
_install_fake_zlapi()
tmp_home, old_home = _isolated_home()
try:
    a2 = zp.ZaloPersonalAdapter()
    check("can_deliver-false-when-not-logged-in", a2.can_deliver() is False)
    check("is_logged_in-false-when-not-logged-in", a2.is_logged_in() is False)
    check("masked-phone-empty-when-not-logged-in",
          a2.get_masked_phone() == "")

    result = a2.login("0912345678", "s3cr3t-p@ss")
    check("login-returns-ok", result == {"ok": True, "phone": "09***5678"},
          f"got {result!r}")

    # Constructor got phone+password+imei, exactly as the README documents.
    inst = _FakeZaloAPI.instances[-1]
    check("fake-got-phone", inst.phone == "0912345678")
    check("fake-got-password", inst.password == "s3cr3t-p@ss")
    check("fake-got-imei", isinstance(inst.imei, str) and len(inst.imei) == 16
          and inst.imei.isdigit())

    # Session file: exists, 0600, has cookies, NO password anywhere.
    path = zp._session_path()
    check("session-file-exists", path.is_file())
    mode = stat.S_IMODE(path.stat().st_mode)
    check("session-file-mode-0600", mode == 0o600, f"mode={oct(mode)}")
    raw = path.read_text(encoding="utf-8")
    check("session-file-no-password",
          "s3cr3t-p@ss" not in raw and "password" not in raw.lower())
    data = json.loads(raw)
    check("session-has-cookies",
          data.get("session_cookies") == {"zpw_sek": "fake-secret",
                                          "zpsid": "fake-sid"})
    check("session-has-phone", data.get("phone") == "0912345678")
    check("session-has-masked-phone", data.get("phone_masked") == "09***5678")
    check("session-has-imei", data.get("imei") == inst.imei)

    # Round-trip: a fresh adapter sees the login.
    a3 = zp.ZaloPersonalAdapter()
    check("is_logged_in-true-after-login", a3.is_logged_in() is True)
    check("can_deliver-true-after-login", a3.can_deliver() is True)
    check("get_masked_phone-after-login",
          a3.get_masked_phone() == "09***5678")

    # send_text rebuilds the client from cookies, without the password.
    before = len(_FakeZaloAPI.instances)
    ok = asyncio.run(a3.send_text("123456789", "hello from boom"))
    check("send_text-returns-true", ok is True)
    check("send_text-built-new-client", len(_FakeZaloAPI.instances) == before + 1)
    sender = _FakeZaloAPI.instances[-1]
    check("send_text-no-password-reuse", sender.password is None)
    check("send_text-reused-cookies",
          sender.session_cookies == {"zpw_sek": "fake-secret",
                                    "zpsid": "fake-sid"})
    check("send_text-reused-phone", sender.phone == "0912345678")
    check("send_text-payload",
          sender.sent == [("hello from boom", "123456789", "USER")],
          f"got {sender.sent!r}")

    # logout removes the session; delivery flips back off.
    a3.logout()
    check("logout-removes-file", not path.exists())
    a4 = zp.ZaloPersonalAdapter()
    check("is_logged_in-false-after-logout", a4.is_logged_in() is False)
    check("can_deliver-false-after-logout", a4.can_deliver() is False)
    a3.logout()  # idempotent: no raise when already gone
    check("logout-idempotent", True)

    # send_text with no session -> False, and never touches zlapi.
    before = len(_FakeZaloAPI.instances)
    ok = asyncio.run(a4.send_text("123456789", "should not send"))
    check("send_text-no-session-false", ok is False)
    check("send_text-no-session-no-client-built",
          len(_FakeZaloAPI.instances) == before)
    ok = asyncio.run(a4.send_text("123456789", ""))
    check("send_text-empty-text-false", ok is False)
finally:
    _restore_home(old_home)
    _uninstall_fake_zlapi()

# 4. Phone masking unit checks.
check("mask-standard", zp._mask_phone("0912345678") == "09***5678")
check("mask-with-spaces", zp._mask_phone("0912 345 678") == "09***5678")
check("mask-short", zp._mask_phone("123") == "***")
check("mask-long-intl", zp._mask_phone("+84912345678") == "84***5678")

print(f"\nAll {PASS} checks passed.")
