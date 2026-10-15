#!/usr/bin/env python3
"""Test gom toan module triggers (models/store/checkers/runner/tools).

Offline, khong can network/credential. Chay tu ~/workspace/boom-mods/triggers:
    PYTHONPATH=agent/src python3 evals/test_triggers.py
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "agent", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from triggers.models import Trigger, VALID_KINDS
from triggers.store import TriggerStore
from triggers.checkers import check_trigger
from triggers.runner import run_checks, reset_cooldown_state, _extract_newest_id
from triggers import tools as triggers_tools

PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append("%s %s" % (name, extra))


def fresh_store():
    tmp = tempfile.TemporaryDirectory()
    store = TriggerStore(os.path.join(tmp.name, "t.db"))
    return tmp, store


def make_trigger(**kw):
    base = dict(id="", name="t", kind="gmail_new",
                config={"query": "q"}, action={"type": "log"},
                cooldown_sec=3600)
    base.update(kw)
    return Trigger(**base)


# ------------------------------------------------------------- models

t = Trigger(id="x", name="n", kind="gmail_new",
            config={"query": "q"}, action={"type": "log"})
d = t.to_dict()
t2 = Trigger.from_dict(d)
check("models round-trip", t2.to_dict() == d, str(t2.to_dict()))

weird = Trigger.from_dict({"id": "w", "name": "w", "kind": "kind_la_hoac"})
check("models from_dict kind la khong reject", weird.kind == "kind_la_hoac")

check("models VALID_KINDS du 4 kind",
      VALID_KINDS == frozenset({"gmail_new", "price_below",
                                "keyword_news", "webhook"}), str(VALID_KINDS))

# ------------------------------------------------------------- store

tmp, store = fresh_store()
tid = store.add(make_trigger(name="store-test"))
check("store add tra id", tid.startswith("trg_"), tid)
got = store.get(tid)
check("store get", got is not None and got.name == "store-test")
check("store list", len(store.list()) == 1)
check("store list enabled_only", len(store.list(enabled_only=True)) == 1)
check("store mark_fired", store.mark_fired(tid, at="m9") and
      store.get(tid).last_fired_at == "m9")
check("store toggle off", store.toggle(tid) and
      store.get(tid).enabled is False)
check("store list enabled_only sau toggle", store.list(enabled_only=True) == [])
check("store toggle on", store.toggle(tid) and
      store.get(tid).enabled is True)
check("store delete", store.delete(tid) and store.get(tid) is None)
check("store delete missing", store.delete("nope") is False)
check("store toggle missing", store.toggle("nope") is False)
check("store mark_fired missing", store.mark_fired("nope") is False)
check("store get missing", store.get("nope") is None)
tmp.cleanup()

# ------------------------------------------------------------- checkers

# gmail_new
g = make_trigger(kind="gmail_new", config={"query": "invoice"})
res = check_trigger(g, {"gmail_search": lambda q: [{"id": "m1", "subject": "A"}]})
check("checkers gmail_new fired lan dau",
      res["fired"] and "newest id=m1" in res["detail"], str(res))
g.last_fired_at = "m1"
res = check_trigger(g, {"gmail_search": lambda q: [{"id": "m1"}]})
check("checkers gmail_new khong fired khi id giong",
      not res["fired"], str(res))
g.last_fired_at = "m1"
res = check_trigger(g, {"gmail_search": lambda q: [{"id": "m2", "subject": "B"}]})
check("checkers gmail_new fired khi id moi",
      res["fired"] and "id=m2" in res["detail"], str(res))
res = check_trigger(g, {"gmail_search": lambda q: []})
check("checkers gmail_new khong fired khi rong", not res["fired"])
res = check_trigger(g, {})
check("checkers gmail_new thieu callable -> error",
      not res["fired"] and res["detail"].startswith("error:"), str(res))
res = check_trigger(make_trigger(kind="gmail_new", config={}),
                    {"gmail_search": lambda q: []})
check("checkers gmail_new thieu query -> error",
      not res["fired"] and "query" in res["detail"], str(res))
res = check_trigger(g, {"gmail_search": lambda q: {"error": "bad token"}})
check("checkers gmail_new callable tra error -> error",
      not res["fired"] and res["detail"].startswith("error:"), str(res))
res = check_trigger(g, {"gmail_search": lambda q:
                        {"messages": [{"id": "m3", "subject": "C"}]}})
check("checkers gmail_new chiu wrapper messages",
      res["fired"] and "id=m3" in res["detail"], str(res))

# keyword_news
n = make_trigger(kind="keyword_news", config={"keyword": "AI"})
res = check_trigger(n, {"news_search": lambda k:
                        [{"link": "https://a.com/1", "title": "T1"}]})
check("checkers keyword_news fired lan dau",
      res["fired"] and "T1" in res["detail"], str(res))
res = check_trigger(n, {"news_search": lambda k:
                        {"headlines": [{"link": "https://a.com/2", "title": "T2"}]}})
check("checkers keyword_news chiu wrapper headlines", res["fired"], str(res))
n.last_fired_at = "https://a.com/2"
res = check_trigger(n, {"news_search": lambda k:
                        [{"link": "https://a.com/2", "title": "T2"}]})
check("checkers keyword_news khong fired khi link giong",
      not res["fired"], str(res))
res = check_trigger(n, {"news_search": lambda k: []})
check("checkers keyword_news khong fired khi rong", not res["fired"])
res = check_trigger(make_trigger(kind="keyword_news", config={}),
                    {"news_search": lambda k: []})
check("checkers keyword_news thieu keyword -> error",
      not res["fired"] and "keyword" in res["detail"], str(res))
res = check_trigger(n, {})
check("checkers keyword_news thieu callable -> error",
      not res["fired"] and res["detail"].startswith("error:"), str(res))

# price_below
p = make_trigger(kind="price_below",
                 config={"url": "https://tiki.vn/p/1", "threshold": 1000000})
res = check_trigger(p, {"get_price": lambda u: 900000})
check("checkers price_below fired khi gia thap",
      res["fired"] and "900000" in res["detail"], str(res))
res = check_trigger(p, {"get_price": lambda u: 1200000})
check("checkers price_below khong fired khi gia cao", not res["fired"])
res = check_trigger(make_trigger(kind="price_below",
                                 config={"url": "u", "threshold": "abc"}),
                    {"get_price": lambda u: 1})
check("checkers price_below threshold sai -> error",
      not res["fired"] and "threshold" in res["detail"], str(res))
res = check_trigger(p, {"get_price": lambda u: None})
check("checkers price_below get_price None -> error",
      not res["fired"] and res["detail"].startswith("error:"), str(res))
res = check_trigger(p, {})
check("checkers price_below thieu callable -> error",
      not res["fired"] and res["detail"].startswith("error:"), str(res))

# webhook + kind la
w = make_trigger(kind="webhook", config={}, action={"type": "notify"})
res = check_trigger(w, {})
check("checkers webhook luon fired", res["fired"], str(res))
res = check_trigger(make_trigger(kind="kind_khong_ton_tai"), {})
check("checkers kind la -> error",
      not res["fired"] and res["detail"].startswith("error:"), str(res))

# ------------------------------------------------------------- runner

def run_scenario(ctx, triggers_def, notify_fn=None, clock_t=1700000000.0,
                 advance=0.0):
    """Dung store moi, nap trigger, chay run_checks 1 lan."""
    reset_cooldown_state()
    tmpd, st = fresh_store()
    ids = [st.add(make_trigger(**td)) for td in triggers_def]
    calls = []

    def _notify(ch, tgt, text):
        if notify_fn is not None:
            notify_fn(ch, tgt, text)
        calls.append((ch, tgt, text))

    state = {"t": clock_t + advance}
    rep = run_checks(st, ctx, _notify, clock=lambda: state["t"])
    return tmpd, st, ids, calls, rep


# 1. fired -> notify goi dung, mark_fired dung newest id
ctx = {"gmail_search": lambda q: [{"id": "m1", "subject": "Hello"}]}
tmpd, st, ids, calls, rep = run_scenario(
    ctx, [dict(name="mail", action={"type": "notify", "channel": "telegram",
                                   "target": "123",
                                   "template": "[{name}] {detail}"})])
tid = ids[0]
check("runner fired", rep["fired"] == [tid], str(rep))
check("runner notify duoc goi 1 lan", len(calls) == 1)
ch, tgt, text = calls[0]
check("runner notify dung channel/target",
      ch == "telegram" and tgt == "123", str(calls[0]))
check("runner text co name + detail",
      "[mail]" in text and "newest id=m1" in text, text)
check("runner mark_fired dung newest id",
      st.get(tid).last_fired_at == "m1", st.get(tid).last_fired_at)
check("runner notified", rep["notified"] == [tid], str(rep))
check("runner checked", rep["checked"] == 1 and rep["errors"] == [], str(rep))
tmpd.cleanup()

# 2. item MOI den ngay trong cooldown -> fired nhung skipped_cooldown,
#    notify KHONG goi lan 2, khong mark_fired lai
reset_cooldown_state()
tmpd2, st2 = fresh_store()
tid2 = st2.add(make_trigger(name="mail2",
                            action={"type": "notify", "channel": "telegram",
                                    "target": "123", "template": "{detail}"}))
calls2 = []
clock_state = {"t": 1700000000.0}
rep1 = run_checks(st2, ctx, lambda c, t, x: calls2.append((c, t, x)),
                  clock=lambda: clock_state["t"])
check("runner lan 1 fired binh thuong", rep1["notified"] == [tid2], str(rep1))
ctx_m2 = {"gmail_search": lambda q: [{"id": "m2", "subject": "New"}]}
rep2 = run_checks(st2, ctx_m2, lambda c, t, x: calls2.append((c, t, x)),
                  clock=lambda: clock_state["t"])
check("runner lan 2 skipped_cooldown",
      rep2["skipped_cooldown"] == [tid2] and rep2["fired"] == [tid2], str(rep2))
check("runner notify khong goi lan 2", len(calls2) == 1, str(len(calls2)))
check("runner lan 2 khong notified", rep2["notified"] == [], str(rep2))
check("runner lan 2 khong mark_fired lai",
      st2.get(tid2).last_fired_at == "m1")

# 3. tua clock qua cooldown + item moi -> fire lai
#    (LUU Y: tmpd2.cleanup() chi goi SAU scenario 3 — db van can dung)
clock_state["t"] += 3601
rep3 = run_checks(st2, ctx_m2, lambda c, t, x: calls2.append((c, t, x)),
                  clock=lambda: clock_state["t"])
check("runner sau cooldown + item moi fire lai",
      rep3["notified"] == [tid2], str(rep3))
check("runner notify lan 2 sau cooldown", len(calls2) == 2)
check("runner mark_fired id moi", st2.get(tid2).last_fired_at == "m2",
      st2.get(tid2).last_fired_at)
tmpd2.cleanup()

# 4. disabled trigger -> skip (khong nam trong checked)
reset_cooldown_state()
tmpd4, st4 = fresh_store()
tid4 = st4.add(make_trigger(name="off", enabled=False))
rep4 = run_checks(st4, ctx, lambda c, t, x: None,
                  clock=lambda: 1700000000.0)
check("runner bo qua trigger disabled",
      rep4["checked"] == 0 and rep4["fired"] == [], str(rep4))
tmpd4.cleanup()

# 5. action log -> khong goi notify nhung mark_fired
reset_cooldown_state()
tmpd5, st5 = fresh_store()
tid5 = st5.add(make_trigger(name="logger", action={"type": "log"}))
calls5 = []
rep5 = run_checks(st5, ctx, lambda c, t, x: calls5.append(1),
                  clock=lambda: 1700000000.0)
check("runner action log khong goi notify", calls5 == [])
check("runner action log van mark_fired",
      st5.get(tid5).last_fired_at == "m1")
check("runner action log vao notified", rep5["notified"] == [tid5], str(rep5))
tmpd5.cleanup()

# 6. notify_fn raise -> errors co ghi, khong mark_fired, khong cooldown
reset_cooldown_state()
tmpd6, st6 = fresh_store()
tid6 = st6.add(make_trigger(name="badnotify",
                            action={"type": "notify", "channel": "telegram",
                                    "target": "123", "template": "{detail}"}))

def boom(ch, tgt, text):
    raise RuntimeError("send failed")

rep6 = run_checks(st6, ctx, boom, clock=lambda: 1700000000.0)
check("runner notify raise -> errors ghi nhan",
      len(rep6["errors"]) == 1 and "send failed" in rep6["errors"][0],
      str(rep6["errors"]))
check("runner notify raise -> khong mark_fired",
      st6.get(tid6).last_fired_at is None)
check("runner notify raise -> khong notified", rep6["notified"] == [])
check("runner notify raise -> fired van ghi", rep6["fired"] == [tid6])
tmpd6.cleanup()

# 7. template format loi -> dung template nguyen
reset_cooldown_state()
tmpd7, st7 = fresh_store()
tid7 = st7.add(make_trigger(name="badtpl",
                            action={"type": "notify", "channel": "telegram",
                                    "target": "123", "template": "bad {oops"}))
calls7 = []
rep7 = run_checks(st7, ctx, lambda c, t, x: calls7.append((c, t, x)),
                  clock=lambda: 1700000000.0)
check("runner template loi van notify", len(calls7) == 1)
check("runner template loi dung template nguyen",
      calls7[0][2] == "bad {oops", calls7[0][2])
tmpd7.cleanup()

# 8. action type la -> errors, khong mark
reset_cooldown_state()
tmpd8, st8 = fresh_store()
tid8 = st8.add(make_trigger(name="weird", action={"type": "sms"}))
rep8 = run_checks(st8, ctx, lambda c, t, x: None,
                  clock=lambda: 1700000000.0)
check("runner action la -> errors",
      len(rep8["errors"]) == 1 and "unknown action" in rep8["errors"][0],
      str(rep8["errors"]))
check("runner action la -> khong mark_fired",
      st8.get(tid8).last_fired_at is None)
tmpd8.cleanup()

# 9. cooldown ton trong that: cooldown nho, chua het -> skip; het -> fire
reset_cooldown_state()
tmpd9, st9 = fresh_store()
tid9 = st9.add(make_trigger(name="cd", cooldown_sec=10,
                            action={"type": "notify", "channel": "t",
                                    "target": "x", "template": "{detail}"}))
clk = {"t": 1700000000.0}
n9 = []
nf = lambda c, t, x: n9.append(1)
r_a = run_checks(st9, ctx, nf, clock=lambda: clk["t"])
clk["t"] += 5
ctx9 = {"gmail_search": lambda q: [{"id": "mX", "subject": "Z"}]}
r_b = run_checks(st9, ctx9, nf, clock=lambda: clk["t"])  # item moi nhung chua het cooldown
n_after_b = len(n9)  # snapshot ngay: check sau chay r_c se lam sai so
clk["t"] += 6  # tong 11 > 10
r_c = run_checks(st9, ctx9, nf, clock=lambda: clk["t"])
check("runner cooldown 10s: lan 1 fire", r_a["notified"] == [tid9])
check("runner cooldown 10s: +5s skip",
      r_b["skipped_cooldown"] == [tid9] and n_after_b == 1, str(r_b))
check("runner cooldown 10s: +11s fire lai",
      r_c["notified"] == [tid9] and len(n9) == 2, str(r_c))
tmpd9.cleanup()

# 10. trich id: keyword_news co id trong detail (format moi) -> lay id that
check("runner extract gmail_new first",
      _extract_newest_id(make_trigger(kind="gmail_new"),
                         "first check: 2 message(s), newest id=abc123") == "abc123")
check("runner extract gmail_new lan sau",
      _extract_newest_id(make_trigger(kind="gmail_new"),
                         "new message id=z9 (prev=a1), 1 message(s), subject='x'") == "z9")
check("runner extract keyword_news first",
      _extract_newest_id(make_trigger(kind="keyword_news"),
                         "first check: 1 headline(s), newest id=https://n/1 'T'") == "https://n/1")
check("runner extract keyword_news lan sau",
      _extract_newest_id(make_trigger(kind="keyword_news"),
                         "new headline id=https://n/2 (prev=https://n/1): 'T2'") == "https://n/2")
reset_cooldown_state()
tmpd10, st10 = fresh_store()
tid10 = st10.add(make_trigger(name="news", kind="keyword_news",
                              config={"keyword": "AI"}, action={"type": "log"}))
rep10 = run_checks(st10, {"news_search": lambda k: [{"link": "https://n/1",
                                                     "title": "T"}]},
                   lambda c, t, x: None, clock=lambda: 1700000000.0)
lf = st10.get(tid10).last_fired_at
check("runner keyword_news mark_fired luu id that (dedup duoc qua restart)",
      rep10["notified"] == [tid10] and lf == "https://n/1", str(lf))
# fire lai ngay voi cung id -> khong fire nua (dedup, khong phu thuoc cooldown)
rep10b = run_checks(st10, {"news_search": lambda k: [{"link": "https://n/1",
                                                      "title": "T"}]},
                    lambda c, t, x: None, clock=lambda: 1700000000.0)
check("runner keyword_news khong fire lai cung id",
      rep10b["fired"] == [], str(rep10b))
tmpd10.cleanup()

# 11. price_below + webhook -> mark_fired timestamp
reset_cooldown_state()
tmpd11, st11 = fresh_store()
tid11 = st11.add(make_trigger(name="price", kind="price_below",
                              config={"url": "u", "threshold": 100},
                              action={"type": "log"}))
tid12 = st11.add(make_trigger(name="hook", kind="webhook", config={},
                              action={"type": "log"}))
rep11 = run_checks(st11, {"get_price": lambda u: 50.0},
                   lambda c, t, x: None, clock=lambda: 1700000000.0)
check("runner price_below + webhook deu notified",
      sorted(rep11["notified"]) == sorted([tid11, tid12]), str(rep11))
check("runner price_below mark timestamp",
      "T" in (st11.get(tid11).last_fired_at or ""))
tmpd11.cleanup()

# ------------------------------------------------------------- tools

class FakeEnv:
    def __init__(self, root_dir):
        self.root_dir = root_dir

with tempfile.TemporaryDirectory() as root:
    built = triggers_tools.build_tools(FakeEnv(root))
    check("tools du 4 tools",
          set(built) == {"trigger_create", "trigger_list",
                         "trigger_delete", "trigger_toggle"}, str(set(built)))
    ok_shape = all(
        set(v) == {"name", "func", "description", "schema"} and v["name"] == k
        and callable(v["func"]) and isinstance(v["description"], str)
        and isinstance(v["schema"], dict)
        for k, v in built.items())
    check("tools shape chuan", ok_shape)
    create = built["trigger_create"]["func"]
    lst = built["trigger_list"]["func"]
    delete = built["trigger_delete"]["func"]
    toggle = built["trigger_toggle"]["func"]

    r = create("x", "kind_sai", {}, {"type": "log"})
    check("tools create kind sai -> error", "error" in r, str(r))
    r = create("x", "gmail_new", {}, {"type": "log"})
    check("tools create gmail_new thieu query -> error",
          "error" in r and "query" in r["error"], str(r))
    r = create("x", "price_below", {"url": "u", "threshold": "abc"},
               {"type": "log"})
    check("tools create price_below threshold sai -> error",
          "error" in r and "threshold" in r["error"], str(r))
    r = create("x", "keyword_news", {}, {"type": "log"})
    check("tools create keyword_news thieu keyword -> error",
          "error" in r, str(r))
    r = create("x", "gmail_new", {"query": "q"}, {"type": "notify",
                                                  "channel": "telegram",
                                                  "template": "t"})
    check("tools create notify thieu target -> error",
          "error" in r and "target" in r["error"], str(r))
    r = create("x", "gmail_new", {"query": "q"}, {"type": "sms"})
    check("tools create action type la -> error", "error" in r, str(r))
    r = create("", "gmail_new", {"query": "q"}, {"type": "log"})
    check("tools create thieu name -> error", "error" in r, str(r))
    r = create("x", "gmail_new", {"query": "q"}, {"type": "log"},
               cooldown_sec="nhieu")
    check("tools create cooldown_sec sai -> error", "error" in r, str(r))

    r = create("Mail moi", "gmail_new", {"query": "invoice"},
               {"type": "notify", "channel": "telegram", "target": "123",
                "template": "{detail}"}, cooldown_sec=60)
    check("tools create ok", "trigger" in r and r["trigger"]["id"], str(r))
    new_id = r["trigger"]["id"]
    check("tools create tra dung field",
          r["trigger"]["name"] == "Mail moi" and
          r["trigger"]["cooldown_sec"] == 60, str(r["trigger"]))
    r = lst()
    check("tools list thay trigger moi",
          r["total"] == 1 and r["triggers"][0]["id"] == new_id, str(r))
    r = lst(enabled_only=True)
    check("tools list enabled_only", r["total"] == 1)
    r = toggle(new_id)
    check("tools toggle tat", r["enabled"] is False, str(r))
    r = lst(enabled_only=True)
    check("tools list enabled_only sau toggle", r["total"] == 0)
    r = toggle(new_id)
    check("tools toggle bat", r["enabled"] is True, str(r))
    r = toggle("khong-co")
    check("tools toggle missing -> None", r["enabled"] is None, str(r))
    r = delete(new_id)
    check("tools delete", r["deleted"] is True, str(r))
    r = delete(new_id)
    check("tools delete lai -> False", r["deleted"] is False, str(r))
    r = lst()
    check("tools list rong sau delete", r["total"] == 0)

    # db that o <root>/memories/triggers.db
    import pathlib
    check("tools db dung vi tri",
          pathlib.Path(root, "memories", "triggers.db").exists())

# root None -> moi tool tra error
built_none = triggers_tools.build_tools(None)
errs = [built_none[k]["func"](*a) for k, a in
        {"trigger_create": ("n", "gmail_new", {"query": "q"}, {"type": "log"}),
         "trigger_list": (), "trigger_delete": ("x",),
         "trigger_toggle": ("x",)}.items()]
check("tools root None -> error het",
      all("error" in e for e in errs), str(errs))

# env dang path thuan (khong phai object co root_dir)
with tempfile.TemporaryDirectory() as root2:
    built2 = triggers_tools.build_tools(root2)
    r = built2["trigger_create"]["func"]("P", "webhook", {},
                                         {"type": "log"})
    check("tools nhan path thuan", "trigger" in r, str(r))

# ------------------------------------------------------------- bao cao

print("=" * 60)
print("PASS: %d  FAIL: %d" % (PASS, FAIL))
for f in FAILURES:
    print("FAIL:", f)
sys.exit(1 if FAIL else 0)
