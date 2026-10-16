"""HTTP-level tests for the Zalo Personal endpoints in gateway/api.py.

Runs a real ThreadingHTTPServer on an ephemeral port; stdlib only, no
network beyond localhost, no real zlapi, no real credentials.

Covers:
- GET  /api/channels/zalo-personal/status -> {"connected": False,
  "phone_masked": None} when not logged in;
- POST /api/channels/zalo-personal/login validation: missing/empty phone
  or password -> 400; invalid JSON -> 400;
- POST login with fake credentials while zlapi is not installed -> 500
  with the mandated 'zlapi' message; the server does NOT crash;
- POST /api/channels/zalo-personal/logout when never logged in ->
  200 {"ok": True}, no crash;
- a mocked-zlapi success round-trip: login -> 200 {"ok": true,
  "phone_masked": "09***4567"}, status -> connected, logout ->
  disconnected;
- the password never appears in any response body or in the API log
  buffer (and the raw phone never appears in responses, only the mask).

Usage: python3 evals/test_zalo_personal_api.py (from the repo root)
"""

import json
import os
import sys
import tempfile
import threading
import types
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # repo root -> `gateway` package

from gateway import api as boom_api  # noqa: E402

PASSED = 0
FAILED = 0
FAILURES = []
RAW_BODIES = []  # every response body, scanned for credential leaks


def check(name, cond, extra=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED += 1
        FAILURES.append(f"{name} {extra}".strip())
        print(f"FAIL {name} {extra}".strip())


class QuietHandler(boom_api._Handler):
    def log_message(self, fmt, *args):  # silence request chatter in tests
        pass


BASE = None


def http(method, path, body=None, raw_bytes=None):
    url = BASE + path
    data = raw_bytes
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        RAW_BODIES.append(raw.decode("utf-8", "replace"))
        return e.code, json.loads(raw.decode("utf-8"))
    RAW_BODIES.append(raw.decode("utf-8", "replace"))
    return r.status, json.loads(raw.decode("utf-8"))


# ------------------------------------------------------------ fake zlapi

PHONE = "0901234567"
SECRET = "s3cr3t-pw-leakcheck-987"
MASKED = "09***4567"  # adapter's _mask_phone(PHONE)


class _FakeCookies:
    def get_dict(self):
        return {"zpw_sek": "fake-secret", "zpsid": "fake-sid"}


class _FakeSession:
    cookies = _FakeCookies()


class _FakeZaloAPI:
    """Stand-in for zlapi.ZaloAPI: no network, cookies on demand."""

    def __init__(self, phone, password, imei=None, session_cookies=None):
        self.phone = phone
        self.password = password
        self.imei = imei
        self.session = _FakeSession()


def _install_fake_zlapi():
    mod = types.ModuleType("zlapi")
    mod.ZaloAPI = _FakeZaloAPI
    sys.modules["zlapi"] = mod


def _remove_fake_zlapi():
    sys.modules.pop("zlapi", None)


# ----------------------------------------------------------------- tests

def test_status_not_logged_in():
    status, body = http("GET", "/api/channels/zalo-personal/status")
    check("status: 200", status == 200, f"got {status}")
    check("status: connected False", body.get("connected") is False, body)
    check("status: phone_masked None", body.get("phone_masked") is None, body)


def test_login_validation():
    for name, payload, want in [
        ("empty body", {}, 400),
        ("missing password", {"phone": PHONE}, 400),
        ("missing phone", {"password": SECRET}, 400),
        ("empty phone", {"phone": "  ", "password": SECRET}, 400),
        ("empty password", {"phone": PHONE, "password": ""}, 400),
        ("non-string phone", {"phone": 123, "password": SECRET}, 400),
    ]:
        status, body = http("POST", "/api/channels/zalo-personal/login", payload)
        check(f"login validation {name}: 400", status == 400,
              f"got {status} {body}")
        check(f"login validation {name}: no password in body",
              SECRET not in json.dumps(body), body)

    status, body = http("POST", "/api/channels/zalo-personal/login",
                        raw_bytes=b"{not json")
    check("login invalid JSON: 400", status == 400, f"got {status} {body}")


def test_login_fake_creds_no_zlapi():
    # zlapi is not installed in this environment -> clean RuntimeError,
    # reported as 500. The server must stay up afterwards.
    status, body = http("POST", "/api/channels/zalo-personal/login",
                        {"phone": PHONE, "password": SECRET})
    check("login no zlapi: 500", status == 500, f"got {status} {body}")
    err = str(body.get("error", ""))
    check("login no zlapi: 'zlapi' in message", "zlapi" in err.lower(), err)
    check("login no zlapi: password not in response", SECRET not in err, err)

    # server still alive
    status2, body2 = http("GET", "/api/channels/zalo-personal/status")
    check("server alive after failed login", status2 == 200
          and body2.get("connected") is False, (status2, body2))


def test_logout_not_logged_in():
    status, body = http("POST", "/api/channels/zalo-personal/logout")
    check("logout not logged in: 200", status == 200, f"got {status}")
    check("logout not logged in: ok true", body.get("ok") is True, body)


def test_success_round_trip():
    _install_fake_zlapi()
    try:
        status, body = http("POST", "/api/channels/zalo-personal/login",
                            {"phone": PHONE, "password": SECRET})
        check("login success: 200", status == 200, f"got {status} {body}")
        check("login success: ok true", body.get("ok") is True, body)
        check("login success: phone_masked", body.get("phone_masked") == MASKED,
              body)

        status, body = http("GET", "/api/channels/zalo-personal/status")
        check("status after login: connected", body.get("connected") is True,
              body)
        check("status after login: masked phone",
              body.get("phone_masked") == MASKED, body)

        status, body = http("POST", "/api/channels/zalo-personal/logout")
        check("logout after login: 200 ok", status == 200
              and body.get("ok") is True, (status, body))

        status, body = http("GET", "/api/channels/zalo-personal/status")
        check("status after logout: disconnected",
              body.get("connected") is False
              and body.get("phone_masked") is None, body)
    finally:
        _remove_fake_zlapi()


def test_no_credential_leaks():
    check("raw phone never in any response", all(PHONE not in b for b in RAW_BODIES))
    check("password never in any response", all(SECRET not in b for b in RAW_BODIES))
    logs = " ".join(str(e.get("msg", "")) for e in boom_api._LOG_BUFFER)
    check("password never in api logs", SECRET not in logs)
    check("raw phone never in api logs", PHONE not in logs, logs[-500:])


def main():
    # Isolate the adapter's session file: Path.home() reads $HOME on POSIX,
    # so point it at a temp dir to avoid touching the real ~/.boom/.
    tmp_home = tempfile.mkdtemp(prefix="zalo_personal_api_test_")
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = tmp_home
    try:
        server = boom_api.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        server.api = boom_api.BoomAPI()
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        global BASE
        BASE = f"http://127.0.0.1:{server.server_port}"
        try:
            test_status_not_logged_in()
            test_login_validation()
            test_login_fake_creds_no_zlapi()
            test_logout_not_logged_in()
            test_success_round_trip()
            test_no_credential_leaks()
        finally:
            server.shutdown()
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home

    print(f"\n{PASSED} passed, {FAILED} failed")
    if FAILED:
        print("FAILURES:", FAILURES)
        sys.exit(1)


if __name__ == "__main__":
    main()
