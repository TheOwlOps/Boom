"""Offline tests for the ACP adapter. No network, no Boom agent needed.

Run: python3 evals/test_acp.py
"""

import asyncio
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The importable `acp` package lives one level down: <root>/acp/acp/
# (the outer <root>/acp/ dir is just a container, not a package).
sys.path.insert(0, str(HERE.parent / "acp"))  # makes `import acp.server` work

from acp.server import AcpServer, PROTOCOL_VERSION

PASS = 0


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {extra}")
        raise AssertionError(name)


async def fake_turn(session_id, text):
    return f"echo:{text}"


def make_server(turn_fn=fake_turn):
    out = io.StringIO()
    srv = AcpServer(turn_fn=turn_fn, out=out)
    return srv, out


def last_response(out):
    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    return json.loads(lines[-1]) if lines else None


def req(srv, method, params=None, rid=1):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    asyncio.run(srv._handle_line(json.dumps(msg)))


def test_initialize():
    srv, out = make_server()
    req(srv, "initialize", {}, rid=1)
    r = last_response(out)
    check("initialize result", r["id"] == 1 and r["result"]["protocolVersion"] == PROTOCOL_VERSION)
    check("initialize capabilities", r["result"]["agentCapabilities"]["promptCapabilities"]["text"] is True)


def test_session_lifecycle():
    srv, out = make_server()
    req(srv, "session/new", {}, rid=1)
    sid = last_response(out)["result"]["sessionId"]
    check("session/new returns id", isinstance(sid, str) and len(sid) >= 8)

    req(srv, "session/list", {}, rid=2)
    sessions = last_response(out)["result"]["sessions"]
    check("session/list contains it", any(s["sessionId"] == sid for s in sessions))

    req(srv, "session/prompt", {"sessionId": sid, "prompt": "hello"}, rid=3)
    r = last_response(out)
    check("prompt returns turn_fn text", r["result"]["text"] == "echo:hello", str(r))

    req(srv, "session/cancel", {"sessionId": sid}, rid=4)
    check("cancel ok", last_response(out)["result"]["cancelled"] is True)

    req(srv, "session/cancel", {"sessionId": "nope"}, rid=5)
    check("cancel unknown session errors", "error" in last_response(out))


def test_prompt_block_format():
    srv, out = make_server()
    req(srv, "session/new", {}, rid=1)
    sid = last_response(out)["result"]["sessionId"]
    blocks = [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]
    req(srv, "session/prompt", {"sessionId": sid, "prompt": blocks}, rid=2)
    check("block list extracted", last_response(out)["result"]["text"] == "echo:first\nsecond")


def test_prompt_errors():
    srv, out = make_server()
    req(srv, "session/new", {}, rid=1)
    sid = last_response(out)["result"]["sessionId"]

    req(srv, "session/prompt", {"sessionId": "unknown", "prompt": "hi"}, rid=2)
    check("prompt unknown session errors", "error" in last_response(out))

    req(srv, "session/prompt", {"sessionId": sid, "prompt": "   "}, rid=3)
    check("prompt empty text errors", "error" in last_response(out))

    req(srv, "session/prompt", {"sessionId": sid}, rid=4)
    check("prompt missing text errors", "error" in last_response(out))


def test_no_loop_honest():
    srv, out = make_server(turn_fn=None)
    req(srv, "session/new", {}, rid=1)
    sid = last_response(out)["result"]["sessionId"]
    req(srv, "session/prompt", {"sessionId": sid, "prompt": "hello"}, rid=2)
    r = last_response(out)
    check("no loop -> error, no fabricated reply",
          "error" in r and "result" not in r, str(r))


def test_protocol_errors():
    srv, out = make_server()
    # malformed JSON -> parse error, server survives
    asyncio.run(srv._handle_line("{not json"))
    r = last_response(out)
    check("malformed -> -32700", r["error"]["code"] == -32700, str(r))

    req(srv, "initialize", {}, rid=9)
    check("server alive after bad line", last_response(out)["id"] == 9)

    req(srv, "nope/method", {}, rid=10)
    check("unknown method -> -32601", last_response(out)["error"]["code"] == -32601)

    # notification (no id) -> no response
    before = out.getvalue()
    asyncio.run(srv._handle_line(json.dumps(
        {"jsonrpc": "2.0", "method": "initialized", "params": {}})))
    check("notification -> no reply", out.getvalue() == before)

    # non-dict request
    asyncio.run(srv._handle_line("[1,2,3]"))
    check("non-dict -> -32600", last_response(out)["error"]["code"] == -32600)


def test_full_stdio_roundtrip():
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}},
        "{broken",
        {"jsonrpc": "2.0", "id": 3, "method": "session/list", "params": {}},
    ]
    inp = io.StringIO("\n".join(
        ln if isinstance(ln, str) else json.dumps(ln) for ln in lines) + "\n")
    out = io.StringIO()
    srv = AcpServer(turn_fn=fake_turn, inp=inp, out=out)
    asyncio.run(srv._serve_async())
    responses = [json.loads(ln) for ln in out.getvalue().splitlines() if ln.strip()]
    by_id = {r.get("id"): r for r in responses}
    check("stdio: 4 responses (incl. parse error with id None)",
          len(responses) == 4, str(responses))
    check("stdio: initialize ok", by_id[1]["result"]["protocolVersion"] == PROTOCOL_VERSION)
    check("stdio: parse error keeps server alive",
          any(r.get("error", {}).get("code") == -32700 for r in responses))
    check("stdio: list after bad line",
          any(s["sessionId"] == by_id[2]["result"]["sessionId"]
              for s in by_id[3]["result"]["sessions"]))
    check("stdio: session/list works", "sessions" in by_id[3]["result"])


def test_no_external_deps():
    src = (HERE.parent / "acp" / "acp" / "server.py").read_text(encoding="utf-8")
    for banned in ("hermes", "import acp\n", "from acp "):
        check(f"no {banned.strip()} reference", banned not in src)
    main_src = (HERE.parent / "acp" / "acp" / "__main__.py").read_text(encoding="utf-8")
    check("__main__ wires BoomCLI seam",
          "BoomCLI" in main_src and "loop.step" in main_src and "load_session" in main_src)


if __name__ == "__main__":
    test_initialize()
    test_session_lifecycle()
    test_prompt_block_format()
    test_prompt_errors()
    test_no_loop_honest()
    test_protocol_errors()
    test_full_stdio_roundtrip()
    test_no_external_deps()
    print(f"ALL ACP TESTS PASSED ({PASS} checks)")
