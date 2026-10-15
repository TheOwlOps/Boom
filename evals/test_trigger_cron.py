"""Offline tests for triggers/cron_wiring.py. No credentials, no network."""

import asyncio
import sys
import tempfile
import types
from pathlib import Path

# Import root is agent/src (repo convention).
SRC = Path(__file__).resolve().parents[1] / "agent" / "src"
sys.path.insert(0, str(SRC))

from triggers import cron_wiring
from triggers.cron_wiring import (
    TRIGGER_CRON_EXPR,
    TRIGGER_CRON_ID,
    TRIGGER_CRON_PROMPT,
    build_check_tool,
    build_ctx,
    check_triggers_job,
    register_trigger_cron,
)
from triggers.models import Trigger
from triggers.store import TriggerStore

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL:", name, detail)


class FakeEnv:
    def __init__(self, root_dir=None, daemon=None):
        self.root_dir = root_dir
        self.daemon = daemon


class FakeDaemon:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    async def send_to(self, channel, target, text):
        self.sent.append((channel, target, text))
        return not self.fail


# ------------------------------------------------------------------ build_ctx


def _patch_connector(monkey_name, fake):
    orig = getattr(cron_wiring, monkey_name)
    setattr(cron_wiring, monkey_name, lambda: fake)
    return orig


def test_ctx_success_paths():
    class Gmail:
        def gmail_search(self, q):
            assert q == "from:boss"
            return {"messages": [{"id": "m1", "subject": "hi"}]}

    class News:
        def news_search(self, kw):
            assert kw == "AI"
            return {"headlines": [{"link": "http://x", "title": "t"}]}

    class Tiki:
        def tiki_product(self, url):
            assert "tiki.vn" in url
            return {"price": 19900000}

    o1 = _patch_connector("_get_gmail_connector", Gmail())
    o2 = _patch_connector("_get_news_connector", News())
    o3 = _patch_connector("_get_tiki_connector", Tiki())
    try:
        ctx = build_ctx(FakeEnv())
        check("ctx has 3 callables",
              set(ctx) == {"gmail_search", "news_search", "get_price"})
        check("gmail_search passthrough",
              ctx["gmail_search"]("from:boss")["messages"][0]["id"] == "m1")
        check("news_search passthrough",
              ctx["news_search"]("AI")["headlines"][0]["title"] == "t")
        check("get_price float",
              ctx["get_price"]("https://tiki.vn/p1") == 19900000.0)
    finally:
        setattr(cron_wiring, "_get_gmail_connector", o1)
        setattr(cron_wiring, "_get_news_connector", o2)
        setattr(cron_wiring, "_get_tiki_connector", o3)


def test_ctx_error_paths():
    def boom():
        raise RuntimeError("network down")

    o1 = _patch_connector("_get_gmail_connector", types.SimpleNamespace(gmail_search=lambda q: boom()))
    o2 = _patch_connector("_get_news_connector", types.SimpleNamespace(news_search=lambda k: boom()))
    o3 = _patch_connector("_get_tiki_connector", types.SimpleNamespace(tiki_product=lambda u: boom()))
    try:
        ctx = build_ctx(FakeEnv())
        check("gmail_search error -> []", ctx["gmail_search"]("q") == [])
        check("news_search error -> []", ctx["news_search"]("k") == [])
        check("get_price error -> None", ctx["get_price"]("u") is None)
    finally:
        setattr(cron_wiring, "_get_gmail_connector", o1)
        setattr(cron_wiring, "_get_news_connector", o2)
        setattr(cron_wiring, "_get_tiki_connector", o3)


def test_ctx_connector_error_dict():
    o1 = _patch_connector("_get_gmail_connector",
                          types.SimpleNamespace(gmail_search=lambda q: {"error": "no oauth"}))
    o3 = _patch_connector("_get_tiki_connector",
                          types.SimpleNamespace(tiki_product=lambda u: {"error": "not found"}))
    try:
        ctx = build_ctx(FakeEnv())
        check("gmail_search {'error'} -> []", ctx["gmail_search"]("q") == [])
        check("get_price {'error'} -> None", ctx["get_price"]("u") is None)
        # non-numeric price -> None
        o3b = _patch_connector("_get_tiki_connector",
                               types.SimpleNamespace(tiki_product=lambda u: {"price": "abc"}))
        try:
            check("get_price non-numeric -> None",
                  build_ctx(FakeEnv())["get_price"]("u") is None)
        finally:
            setattr(cron_wiring, "_get_tiki_connector", o3b)
    finally:
        setattr(cron_wiring, "_get_gmail_connector", o1)
        setattr(cron_wiring, "_get_tiki_connector", o3)


def test_ctx_no_connectors_importable():
    # Helpers raise ImportError -> safe values, never raise.
    o1 = _patch_connector("_get_gmail_connector", None)
    setattr(cron_wiring, "_get_gmail_connector",
            lambda: (_ for _ in ()).throw(ImportError("no module")))
    try:
        ctx = build_ctx(FakeEnv())
        check("gmail_search import fail -> []", ctx["gmail_search"]("q") == [])
    finally:
        setattr(cron_wiring, "_get_gmail_connector", o1)


# ------------------------------------------------------------------ check_triggers_job


def _make_trigger(**kw):
    base = dict(
        id="", name="t", kind="webhook", config={},
        action={"type": "notify", "channel": "telegram",
                "target": "123", "template": "fired: {detail}"},
        enabled=True, cooldown_sec=0,
    )
    base.update(kw)
    return Trigger(**base)


def test_job_fires_and_notifies():
    with tempfile.TemporaryDirectory() as tmp:
        daemon = FakeDaemon()
        env = FakeEnv(root_dir=tmp, daemon=daemon)
        store = TriggerStore(Path(tmp) / "memories" / "triggers.db")
        tid = store.add(_make_trigger(name="webhook test"))
        report = check_triggers_job(env)
        check("job checked 1", report.get("checked") == 1, report)
        check("job fired", report.get("fired") == [tid], report)
        check("job notified", report.get("notified") == [tid], report)
        check("no errors", report.get("errors") == [], report)
        check("send_to called once", len(daemon.sent) == 1, daemon.sent)
        ch, tgt, text = daemon.sent[0]
        check("send_to channel", ch == "telegram")
        check("send_to target", tgt == "123")
        check("send_to text has detail", "webhook invoked" in text, text)


def test_job_no_root_dir():
    report = check_triggers_job(FakeEnv(root_dir=None))
    check("no root_dir -> error report",
          report.get("checked") == 0 and len(report.get("errors", [])) == 1,
          report)


def test_job_notify_failure_retries():
    with tempfile.TemporaryDirectory() as tmp:
        daemon = FakeDaemon(fail=True)
        env = FakeEnv(root_dir=tmp, daemon=daemon)
        store = TriggerStore(Path(tmp) / "memories" / "triggers.db")
        tid = store.add(_make_trigger(name="fail notify"))
        report = check_triggers_job(env)
        check("notify fail: fired", report.get("fired") == [tid], report)
        check("notify fail: not notified", report.get("notified") == [], report)
        check("notify fail: error recorded",
              any("notify" in e for e in report.get("errors", [])), report)
        # not marked -> last_fired_at stays None -> would fire again next cycle
        check("notify fail: not marked", store.get(tid).last_fired_at is None)


def test_job_no_daemon():
    with tempfile.TemporaryDirectory() as tmp:
        env = FakeEnv(root_dir=tmp, daemon=None)
        store = TriggerStore(Path(tmp) / "memories" / "triggers.db")
        tid = store.add(_make_trigger(name="no daemon"))
        report = check_triggers_job(env)
        check("no daemon: error recorded",
              any("send_to" in e or "daemon" in e for e in report.get("errors", [])),
              report)
        check("no daemon: not notified", report.get("notified") == [], report)


def test_job_empty_store():
    with tempfile.TemporaryDirectory() as tmp:
        env = FakeEnv(root_dir=tmp, daemon=FakeDaemon())
        report = check_triggers_job(env)
        check("empty store: checked 0", report.get("checked") == 0, report)
        check("empty store: no errors", report.get("errors") == [], report)


# ------------------------------------------------------------------ build_check_tool


def test_check_tool_shape():
    with tempfile.TemporaryDirectory() as tmp:
        env = FakeEnv(root_dir=tmp, daemon=FakeDaemon())
        tools = build_check_tool(env)
        check("tool present", "triggers_check_now" in tools)
        t = tools["triggers_check_now"]
        check("tool has name/func/description/schema",
              all(k in t for k in ("name", "func", "description", "schema")))
        check("tool callable", callable(t["func"]))
        out = t["func"]()
        check("tool runs job", isinstance(out, dict) and "checked" in out, out)


# ------------------------------------------------------------------ register_trigger_cron


def test_register_creates_job():
    with tempfile.TemporaryDirectory() as tmp:
        env = FakeEnv(root_dir=tmp)
        res = register_trigger_cron(env)
        check("register ok", "job" in res and "error" not in res, res)
        job = res["job"]
        check("job id", job.get("id") == TRIGGER_CRON_ID, job)
        check("job cron expr", job.get("cron") == TRIGGER_CRON_EXPR, job)
        check("job prompt mentions tool",
              "triggers_check_now" in job.get("prompt", ""), job.get("prompt"))
        check("not already_existed", res.get("already_existed") is False, res)
        # cron expression parses
        from tools.cronjob_tool import parse_cron
        try:
            parse_cron(job["cron"])
            check("cron expr valid", True)
        except ValueError as e:
            check("cron expr valid", False, str(e))


def test_register_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        env = FakeEnv(root_dir=tmp)
        r1 = register_trigger_cron(env)
        r2 = register_trigger_cron(env)
        check("second register already_existed",
              r2.get("already_existed") is True, r2)
        check("same job id", r2["job"].get("id") == TRIGGER_CRON_ID)
        check("first was new", r1.get("already_existed") is False)


def test_register_no_root_dir():
    res = register_trigger_cron(FakeEnv(root_dir=None))
    check("no root_dir -> error", "error" in res, res)


if __name__ == "__main__":
    test_ctx_success_paths()
    test_ctx_error_paths()
    test_ctx_connector_error_dict()
    test_ctx_no_connectors_importable()
    test_job_fires_and_notifies()
    test_job_no_root_dir()
    test_job_notify_failure_retries()
    test_job_no_daemon()
    test_job_empty_store()
    test_check_tool_shape()
    test_register_creates_job()
    test_register_idempotent()
    test_register_no_root_dir()
    print("=" * 60)
    print(f"PASS: {PASS}  FAIL: {FAIL}")
    sys.exit(1 if FAIL else 0)
