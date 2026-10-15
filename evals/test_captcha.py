"""Offline tests for the 2captcha connector.

No network, no real key: all HTTP goes through httpx.MockTransport with
fixtures shaped like the real 2captcha API v1 (in.php / res.php).
Run: python3 evals/test_captcha.py
"""

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# repo layout: evals/test_captcha.py, agent/src is the import root
sys.path.insert(0, str(HERE.parent / "agent" / "src"))
# contract lives in the shared _contract dir when testing standalone
sys.path.insert(0, str(Path.home() / "workspace" / "boom-mods" / "_contract"))

import httpx

from connectors.captcha import CaptchaConnector, CONNECTOR

PASS = 0


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {extra}")
        raise AssertionError(name)


# ---------------------------------------------------------------- fixtures

TASK_ID = "2122988149"
SOLUTION = "abcd1234"


def make_transport(script):
    """script: dict with optional keys:
    in_php  -> response dict for POST /in.php (default success)
    balance -> response dict for getbalance (default 10.5)
    get_seq -> list of response dicts for action=get, consumed in order
    """
    state = {"posts": 0, "gets": 0, "balances": 0, "requests": 0}
    get_seq = list(script.get("get_seq", []))

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"] += 1
        path = request.url.path
        if path == "/in.php":
            state["posts"] += 1
            body = script.get(
                "in_php", {"status": 1, "request": TASK_ID}
            )
            return httpx.Response(200, json=body)
        if path == "/res.php":
            params = dict(request.url.params)
            if params.get("action") == "getbalance":
                state["balances"] += 1
                body = script.get(
                    "balance", {"status": 1, "request": "10.5"}
                )
                return httpx.Response(200, json=body)
            if params.get("action") == "get":
                state["gets"] += 1
                if get_seq:
                    return httpx.Response(200, json=get_seq.pop(0))
                return httpx.Response(
                    200, json={"status": 1, "request": f"OK|{SOLUTION}"}
                )
        return httpx.Response(404, json={"status": 0, "request": "ERROR_UNKNOWN"})

    return httpx.MockTransport(handler), state


def make_connector(script, api_key="test-key-123"):
    transport, state = make_transport(script)
    client = httpx.Client(transport=transport)
    conn = CaptchaConnector(api_key=api_key, client=client)
    return conn, state


# ---------------------------------------------------------------- contract

check("connector name", CONNECTOR.name == "captcha")
tools = CONNECTOR.build_tools()
check("tool names prefixed", set(tools) == {"captcha_solve_image", "captcha_balance"})
for tname, t in tools.items():
    check(f"shape {tname}", t["name"] == tname and callable(t["func"]))
    check(f"schema {tname}", t["schema"]["type"] == "object")

# ---------------------------------------------------------------- status

os.environ.pop("BOOM_2CAPTCHA_KEY", None)
bare = CaptchaConnector()
check("no key -> disconnected", bare.status()["connected"] is False)
os.environ["BOOM_2CAPTCHA_KEY"] = "env-key"
check("env key -> connected", bare.status()["connected"] is True)
os.environ.pop("BOOM_2CAPTCHA_KEY", None)

# ---------------------------------------------------------------- balance

conn, state = make_connector({})
res = conn.captcha_balance()
check("balance parses float", res == {"balance": 10.5}, res)
check("balance made one call", state["balances"] == 1 and state["requests"] == 1)

conn2, state2 = make_connector(
    {"balance": {"status": 0, "request": "ERROR_WRONG_USER_KEY"}}
)
res = conn2.captcha_balance()
check("bad key balance -> error", "ERROR_WRONG_USER_KEY" in res.get("error", ""), res)

conn3, state3 = make_connector({}, api_key="")
res = conn3.captcha_balance()
check("missing key balance -> error, no HTTP",
      "error" in res and state3["requests"] == 0, res)

# ---------------------------------------------------------------- solve happy path

conn, state = make_connector(
    {
        "get_seq": [
            {"status": 0, "request": "CAPCHA_NOT_READY"},
            {"status": 0, "request": "CAPCHA_NOT_READY"},
            {"status": 1, "request": f"OK|{SOLUTION}"},
        ]
    }
)
res = conn.captcha_solve_image("aGVsbG8=", timeout=30, poll_interval=0.01)
check("solve returns text",
      res.get("solved") is True and res.get("text") == SOLUTION
      and res.get("task_id") == TASK_ID, res)
check("polled until ready (1 submit + 3 gets)",
      state["posts"] == 1 and state["gets"] == 3, state)

# ---------------------------------------------------------------- solve errors

conn, state = make_connector(
    {"in_php": {"status": 0, "request": "ERROR_WRONG_USER_KEY"}}
)
res = conn.captcha_solve_image("aGVsbG8=", poll_interval=0.01)
check("submit error -> error, no polling",
      "ERROR_WRONG_USER_KEY" in res.get("error", "") and state["gets"] == 0, res)

conn, state = make_connector(
    {"in_php": {"status": 0, "request": "ERROR_ZERO_BALANCE"}}
)
res = conn.captcha_solve_image("aGVsbG8=", poll_interval=0.01)
check("zero balance -> honest error", "ERROR_ZERO_BALANCE" in res.get("error", ""), res)

conn, state = make_connector(
    {"get_seq": [{"status": 0, "request": "CAPCHA_NOT_READY"}] * 100}
)
res = conn.captcha_solve_image("aGVsbG8=", timeout=0.15, poll_interval=0.05)
check("timeout -> error, not hang",
      "timed out" in res.get("error", ""), res)
check("timeout polled a few times then stopped",
      1 <= state["gets"] <= 5, state)

conn, state = make_connector({}, api_key="")
res = conn.captcha_solve_image("aGVsbG8=")
check("missing key solve -> error, no HTTP",
      "error" in res and state["requests"] == 0, res)

res = conn.captcha_solve_image("   ")
check("empty image -> error, no HTTP",
      "error" in res and state["requests"] == 0, res)

# network failure mid-poll
def boom_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/in.php":
        return httpx.Response(200, json={"status": 1, "request": TASK_ID})
    raise httpx.ConnectError("no route to host")

conn = CaptchaConnector(
    api_key="k", client=httpx.Client(transport=httpx.MockTransport(boom_handler))
)
res = conn.captcha_solve_image("aGVsbG8=", timeout=5, poll_interval=0.01)
check("connect error on poll -> error envelope", "error" in res, res)

print(f"\nALL CAPTCHA TESTS PASSED ({PASS} checks)")
