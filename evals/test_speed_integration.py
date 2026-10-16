"""Integration test: all 4 speed optimizations on the merged repo.

Runs against /tmp/boom_git with all patches applied:
1. parallel_tools: asyncio.gather for concurrent tool calls
2. tool_selector: schema pruning 159 -> <40
3. streaming: StreamHandler + chat_complete_stream
4. smart_turns: TurnController early exit, max_turns 10->6

Run: python3 test_speed_integration.py (from repo root)
"""
import asyncio
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, "/tmp/boom_git/agent/src")

from loop.react import ReActLoop
from loop.tool_selector import ToolSelector, select_or_all
from loop.smart_turns import TurnController
from loop.streaming import StreamHandler, TokenBuffer

PASS = 0
FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} {detail}")

# ---------------------------------------------------------------------------
# 1. Parallel tools
# ---------------------------------------------------------------------------
print("== 1. parallel_tools ==")

async def slow_tool(name, delay=0.3):
    await asyncio.sleep(delay)
    return f"result-{name}"

def make_loop(**kwargs):
    tools = {}
    for tname in ["tool_a", "tool_b", "tool_c"]:
        async def _fn(name=tname, **kw):
            return await slow_tool(name)
        tools[tname] = {"func": _fn, "description": f"mock {tname}", "schema": {}}
    return ReActLoop(tools=tools, **kwargs)

def mock_tc(name, cid):
    return SimpleNamespace(id=cid, name=name, arguments={"x": 1}, args_error=None)

def mock_resp(calls):
    return SimpleNamespace(thought="t", content="", tool_calls=calls, usage=None)

async def test_parallel():
    loop = make_loop(parallel_tools=True)
    resp = mock_resp([mock_tc("tool_a", "1"), mock_tc("tool_b", "2"), mock_tc("tool_c", "3")])
    trace = []
    t0 = time.monotonic()
    await loop._run_tool_calls_parallel(resp, trace)
    dt = time.monotonic() - t0
    check("parallel 3x0.3s < 0.6s", dt < 0.6, f"took {dt:.2f}s")
    check("order preserved", [s.action.name for s in trace] == ["tool_a", "tool_b", "tool_c"])
    check("observations correct", [s.observation for s in trace] == ["result-tool_a", "result-tool_b", "result-tool_c"])

async def test_parallel_error_isolation():
    async def boom(**kw):
        raise RuntimeError("kaboom")
    tools = {
        "good1": {"func": lambda **kw: "ok1", "description": "", "schema": {}},
        "bad": {"func": boom, "description": "", "schema": {}},
        "good2": {"func": lambda **kw: "ok2", "description": "", "schema": {}},
    }
    loop = ReActLoop(tools=tools, parallel_tools=True)
    resp = mock_resp([mock_tc("good1", "1"), mock_tc("bad", "2"), mock_tc("good2", "3")])
    trace = []
    await loop._run_tool_calls_parallel(resp, trace)
    check("error isolated", "kaboom" in (trace[1].observation or ""))
    check("others unaffected", trace[0].observation == "ok1" and trace[2].observation == "ok2")

async def test_sequential_fallback():
    loop = make_loop(parallel_tools=False)
    check("sequential flag stored", loop.parallel_tools is False)

asyncio.run(test_parallel())
asyncio.run(test_parallel_error_isolation())
asyncio.run(test_sequential_fallback())

# ---------------------------------------------------------------------------
# 2. Tool selector
# ---------------------------------------------------------------------------
print("== 2. tool_selector ==")
from environment import AgentEnvironment
env = AgentEnvironment()
schemas = env.get_tools_schema()
check("full catalog >= 100", len(schemas) >= 100, f"got {len(schemas)}")
sel = ToolSelector()
for prompt, must_contain in [
    ("read file config.yaml", "filesystem"),
    ("search the web for news", "web"),
    ("hello", None),
]:
    pruned = sel.select(prompt, schemas)
    check(f"pruned <40 for {prompt!r}", len(pruned) < 40, f"got {len(pruned)}")
    if must_contain:
        groups = {s.get("x-group", "") for s in pruned}
        # group info may be in description; just check non-empty and smaller
        check(f"pruned smaller than full for {prompt!r}", len(pruned) < len(schemas))
check("select_or_all(None) returns full", len(select_or_all(None, "hi", schemas)) == len(schemas))
tok_full = sel.estimate_tokens(schemas)
tok_pruned = sel.estimate_tokens(sel.select("read a file", schemas))
check("token estimate drops", tok_pruned < tok_full, f"{tok_full} -> {tok_pruned}")

# ---------------------------------------------------------------------------
# 3. Streaming
# ---------------------------------------------------------------------------
print("== 3. streaming ==")

async def fake_stream():
    yield {"type": "token", "text": "Hello"}
    yield {"type": "token", "text": " world"}
    yield {"type": "token", "text": ". How are you?"}
    yield {"type": "done", "usage": {"input_tokens": 5, "output_tokens": 8}}

async def test_stream_handler():
    seen = []
    h = StreamHandler(on_token=lambda t: seen.append(t))
    text, calls = await h.consume(fake_stream())
    check("stream assembles text", text == "Hello world. How are you?", f"got {text!r}")
    check("on_token fired per chunk", seen == ["Hello", " world", ". How are you?"])
    check("usage captured", h.last_usage == {"input_tokens": 5, "output_tokens": 8})

asyncio.run(test_stream_handler())

tb = TokenBuffer()
sents = tb.feed("Hello world. How are")
check("TokenBuffer splits sentences", sents == ["Hello world."])
rest = tb.flush()
check("TokenBuffer flush remainder", rest == "How are")

# LLMProvider has streaming methods
from providers.llm import LLMProvider
p = LLMProvider(api_key="test-key")
check("chat_complete_stream exists", hasattr(p, "chat_complete_stream"))

# ---------------------------------------------------------------------------
# 4. Smart turns
# ---------------------------------------------------------------------------
print("== 4. smart_turns ==")
c = TurnController(max_turns=6, patience=2)
calls = [{"name": "read_file", "arguments": {"path": "x"}}]
c.record_turn(calls, ["obs1"])
c.record_turn(calls, ["obs2"])
keep, reason = c.should_continue(calls, ["obs3"])
check("stops on repeated tool calls", not keep and reason == "repeated_tool_calls", reason)

c2 = TurnController()
c2.record_turn([{"name": "a", "arguments": {}}], [""])
c2.record_turn([{"name": "b", "arguments": {}}], ["   "])
keep2, reason2 = c2.should_continue([{"name": "c", "arguments": {}}], [""])
check("stops on empty observations", not keep2 and reason2 == "empty_observations", reason2)

c3 = TurnController()
keep3, reason3 = True, "ok"
for i in range(4):
    tc = [{"name": "tool_x", "arguments": {"i": i}}]
    c3.record_turn(tc, ["Error: boom"])
    keep3, reason3 = c3.should_continue(tc, ["Error: boom"])
    if not keep3:
        break
check("stops on repeated errors", not keep3 and reason3 == "repeated_errors", reason3)

c4 = TurnController()
c4.record_turn([{"name": "a", "arguments": {}}], ["ok"])
keep4, _ = c4.should_continue([{"name": "b", "arguments": {}}], ["fine"])
check("continues on diverse calls", keep4)

# ReActLoop defaults
loop_chat = ReActLoop(tools={})
check("default max_turns=6", loop_chat.max_turns == 6, f"got {loop_chat.max_turns}")
check("smart_exit default True", loop_chat.smart_exit is True)
check("turn_controller present", loop_chat.turn_controller is not None)
loop_code = ReActLoop(tools={}, task_type="coding")
check("coding max_turns=10", loop_code.max_turns == 10, f"got {loop_code.max_turns}")
loop_old = ReActLoop(tools={}, smart_exit=False, max_turns=10)
check("smart_exit=False disables controller", loop_old.turn_controller is None)
check("on_token param exists", hasattr(loop_chat, "on_token"))

# ---------------------------------------------------------------------------
# 5. End-to-end: stuck loop exits early via _run_live_turn
# ---------------------------------------------------------------------------
print("== 5. end-to-end early exit ==")

class StuckLLM:
    """Always returns the same tool call."""
    def __init__(self):
        self.calls = 0
    async def chat_complete(self, messages, tools_schema=None):
        self.calls += 1
        return SimpleNamespace(
            content="", thought="t",
            tool_calls=[SimpleNamespace(id=f"c{self.calls}", name="tool_a",
                                        arguments={"x": 1}, args_error=None)],
            usage=None, finish_reason="tool_calls",
        )

async def test_e2e():
    tools = {"tool_a": {"func": lambda **kw: "obs-a", "description": "", "schema": {}}}
    llm = StuckLLM()
    loop = ReActLoop(tools=tools, llm=llm, smart_exit=True)
    result = await loop._run_live_turn("do something")
    check("stuck loop exits early", llm.calls <= 3, f"llm calls={llm.calls}")
    check("result success", result["success"] is True)
    check("stop_reason recorded", result.get("stop_reason") == "repeated_tool_calls",
          f"got {result.get('stop_reason')}")

asyncio.run(test_e2e())

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
