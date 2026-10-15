"""Tong hop test cho module learning: models, store, extractor, consolidator,
curator, tools.

Chay offline: fake llm (sync callable prompt -> str), SQLite trong tmp_path,
khong mang, khong model that.

Chay:  python3 evals/test_learning.py   (tu ~/workspace/boom-mods/learning)
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

from learning.models import Learning, LearningType
from learning.store import LearningStore
from learning.extractor import extract_learnings, should_learn
from learning.consolidator import consolidate
from learning.curator import run_curation
from learning.tools import build_tools

CHECKS = 0


def check(name, cond):
    global CHECKS
    CHECKS += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(f"FAILED: {name}")


def make_learning(content, ltype=LearningType.PREFERENCE, confidence=0.8,
                  lid="", updated="2026-10-15T10:00:00", source="test"):
    return Learning(
        id=lid,
        type=ltype,
        content=content,
        confidence=confidence,
        source=source,
        created_at="2026-10-10T10:00:00",
        updated_at=updated,
    )


def make_store(tmpdir):
    return LearningStore(Path(tmpdir) / "memories" / "learnings.db")


# ---------------------------------------------------------------- models

def test_models():
    l = Learning(id="a1", type=LearningType.FACT, content="user lives in Hanoi",
                 confidence=0.7, source="chat", created_at="2026-01-01",
                 updated_at="2026-01-02", superseded_by=None)
    d = l.to_dict()
    check("models to_dict keys", set(d) == {"id", "type", "content", "confidence",
                                            "source", "created_at", "updated_at",
                                            "superseded_by"})
    check("models to_dict type is str", d["type"] == "fact")
    l2 = Learning.from_dict(d)
    check("models round-trip id", l2.id == "a1")
    check("models round-trip type", l2.type == LearningType.FACT)
    check("models round-trip content", l2.content == "user lives in Hanoi")
    check("models round-trip confidence", l2.confidence == 0.7)
    check("models round-trip timestamps", l2.created_at == "2026-01-01"
          and l2.updated_at == "2026-01-02")
    check("models superseded_by default None", l.superseded_by is None)

    weird = Learning(id="w", type="mystery-type", content="x", confidence=0.5,
                     source="", created_at="", updated_at="")
    check("models unknown type -> PREFERENCE", weird.type == LearningType.PREFERENCE)
    weird2 = Learning.from_dict({"id": "w2", "type": "nope", "content": "x"})
    check("models from_dict unknown type -> PREFERENCE",
          weird2.type == LearningType.PREFERENCE)

    hi = Learning(id="c", type=LearningType.FACT, content="x", confidence=2.0,
                  source="", created_at="", updated_at="")
    check("models confidence 2.0 clamps to 1.0", hi.confidence == 1.0)
    lo = Learning(id="c2", type=LearningType.FACT, content="x", confidence=-3.0,
                  source="", created_at="", updated_at="")
    check("models confidence -3.0 clamps to 0.0", lo.confidence == 0.0)
    bad = Learning.from_dict({"id": "c3", "type": "fact", "content": "x",
                              "confidence": "abc"})
    check("models non-numeric confidence -> 0.0", bad.confidence == 0.0)
    check("models LearningType members",
          {t.value for t in LearningType} == {"preference", "fact", "decision",
                                              "correction"})


# ---------------------------------------------------------------- store

def test_store(tmpdir):
    store = make_store(tmpdir)
    a = make_learning("User drinks black coffee every morning",
                      LearningType.PREFERENCE, 0.9)
    b = make_learning("User lives in Hanoi", LearningType.FACT, 0.5)
    c = make_learning("Shipped the web UI", LearningType.DECISION, 0.7)
    ida = store.add(a)
    idb = store.add(b)
    idc = store.add(c)
    check("store add returns id str", isinstance(ida, str) and ida.startswith("learn_"))
    check("store add unique ids", len({ida, idb, idc}) == 3)
    check("store count", store.count() == 3)

    got = store.get(ida)
    check("store get", got is not None and got.content == a.content
          and got.type == LearningType.PREFERENCE and abs(got.confidence - 0.9) < 1e-9)
    check("store get unknown -> None", store.get("nope") is None)

    prefs = store.list(type=LearningType.PREFERENCE)
    check("store list by type", len(prefs) == 1 and prefs[0].id == ida)
    prefs2 = store.list(type="preference")
    check("store list by type str", len(prefs2) == 1 and prefs2[0].id == ida)
    all_items = store.list()
    check("store list all", len(all_items) == 3)

    d = make_learning("User hates waiting in queues", LearningType.PREFERENCE, 0.4)
    idd = store.add(d)
    hits = store.search("User")
    check("store search confidence desc",
          [h.id for h in hits] == [ida, idb, idd])
    check("store search narrow", [h.id for h in store.search("queues")] == [idd])
    check("store search none", store.search("zzzz-nope") == [])

    store.supersede(idb, idc)
    check("store supersede marks old", store.get(idb).superseded_by == idc)
    default_list = store.list()
    check("store superseded hidden from default list",
          idb not in {l.id for l in default_list} and len(default_list) == 3)
    full_list = store.list(include_superseded=True)
    check("store include_superseded shows all",
          len(full_list) == 4 and idb in {l.id for l in full_list})
    check("store search skips superseded", idb not in {h.id for h in store.search("Hanoi")})

    check("store remove true", store.remove(idd) is True)
    check("store remove again false", store.remove(idd) is False)
    check("store remove unknown false", store.remove("nope") is False)
    check("store count after remove", store.count() == 3)

    try:
        store.add(make_learning("dup", lid=ida))
        check("store add duplicate id raises", False)
    except ValueError:
        check("store add duplicate id raises", True)

    try:
        store.supersede("nope", idc)
        check("store supersede unknown raises", False)
    except ValueError:
        check("store supersede unknown raises", True)


# ---------------------------------------------------------------- extractor

_EXTRACT_JSON = json.dumps([
    {"type": "preference", "content": "User prefers dark mode", "confidence": 0.9},
    {"type": "fact", "content": "User lives in Hanoi", "confidence": 0.8},
    {"type": "correction", "content": "User corrected the report title", "confidence": 0.95},
    {"type": "bogus", "content": "Unknown type falls back", "confidence": 0.5},
    {"type": "fact", "content": "the password=supersecret123 for wifi", "confidence": 0.9},
    {"type": "fact", "content": "", "confidence": 0.9},
    "not-a-dict",
])

_FAKE_EXTRACT = lambda prompt: _EXTRACT_JSON  # noqa: E731


def test_extractor():
    learnings = extract_learnings("User prefers dark mode, remember this", _FAKE_EXTRACT,
                                  source="chat-1")
    types = [l.type for l in learnings]
    check("extractor preference extracted", LearningType.PREFERENCE in types)
    check("extractor fact extracted", LearningType.FACT in types)
    check("extractor correction extracted", LearningType.CORRECTION in types)
    check("extractor keeps 4 (3 + bogus, drops secret/empty/nondict)",
          len(learnings) == 4)
    check("extractor unknown type -> PREFERENCE",
          any(l.content == "Unknown type falls back" and l.type == LearningType.PREFERENCE
              for l in learnings))
    check("extractor secret filtered",
          not any("password" in l.content for l in learnings))
    check("extractor source kept", all(l.source == "chat-1" for l in learnings))
    check("extractor confidence kept",
          any(l.content == "User prefers dark mode" and abs(l.confidence - 0.9) < 1e-9
              for l in learnings))

    check("extractor broken JSON -> []",
          extract_learnings("remember this", lambda p: "not json {") == [])
    check("extractor non-list JSON -> []",
          extract_learnings("remember this", lambda p: '{"a": 1}') == [])
    check("extractor llm raises -> []",
          extract_learnings("remember this", _raising_llm) == [])
    check("extractor empty text -> []",
          extract_learnings("   ", _FAKE_EXTRACT) == [])

    check("should_learn True (remember)", should_learn("Please remember: I like tea") is True)
    check("should_learn True (prefer)", should_learn("I prefer concise answers") is True)
    check("should_learn True (vietnamese)", should_learn("Nhớ giúp anh việc này") is True)
    check("should_learn False", should_learn("hello, how is the weather today") is False)
    check("should_learn empty False", should_learn("") is False)
    check("should_learn blank False", should_learn("   ") is False)


def _raising_llm(prompt):
    raise RuntimeError("llm down")


# ---------------------------------------------------------------- consolidator

def test_consolidator():
    a = make_learning("User drinks black coffee every morning", LearningType.PREFERENCE,
                      0.9, lid="l1")
    b = make_learning("User likes black coffee in the morning", LearningType.PREFERENCE,
                      0.6, lid="l2")

    dup_llm = lambda p: json.dumps({"relation": "duplicate",  # noqa: E731
                                    "merged_content": "User drinks black coffee in the morning"})
    res = consolidate([a, b], dup_llm)
    check("consolidate duplicate merged 1", len(res["merged"]) == 1)
    m = res["merged"][0]
    check("consolidate merged content from llm",
          m.content == "User drinks black coffee in the morning")
    check("consolidate merged confidence = max",
          abs(m.confidence - 0.9) < 1e-9)
    check("consolidate merged placeholder id", m.id == "__pending_0__")
    check("consolidate merged type kept", m.type == LearningType.PREFERENCE)
    check("consolidate duplicate superseded pairs",
          sorted(res["superseded"]) == [("l1", "__pending_0__"), ("l2", "__pending_0__")])
    check("consolidate no errors", res["errors"] == [])

    # duplicate without merged_content -> fallback to highest-confidence content
    res2 = consolidate([a, b], lambda p: json.dumps({"relation": "duplicate"}))
    check("consolidate duplicate fallback content",
          res2["merged"][0].content == a.content)

    # contradiction: newest updated_at wins
    old = make_learning("User eats meat with every meal daily", LearningType.FACT,
                        0.9, lid="old1", updated="2026-10-10T10:00:00")
    new = make_learning("User is vegetarian never eats meat daily", LearningType.FACT,
                        0.5, lid="new1", updated="2026-10-15T10:00:00")
    res3 = consolidate([old, new], lambda p: json.dumps({"relation": "contradiction"}))
    check("consolidate contradiction no merged", res3["merged"] == [])
    check("consolidate contradiction newer wins",
          res3["superseded"] == [("old1", "new1")])
    check("consolidate contradiction no errors", res3["errors"] == [])

    # distinct: untouched
    res4 = consolidate([a, b], lambda p: json.dumps({"relation": "distinct"}))
    check("consolidate distinct keeps all",
          res4["merged"] == [] and res4["superseded"] == [] and res4["errors"] == [])

    # no candidate group (different types / too few shared words)
    x = make_learning("User drinks black coffee every morning", LearningType.PREFERENCE,
                      0.9, lid="x1")
    y = make_learning("User drinks black coffee every morning", LearningType.FACT,
                      0.9, lid="y1")
    res5 = consolidate([x, y], dup_llm)
    check("consolidate different types -> no group",
          res5["merged"] == [] and res5["superseded"] == [])

    # broken llm: raises / garbage / unknown relation -> errors, no crash
    res6 = consolidate([a, b], _raising_llm)
    check("consolidate llm raises -> error, no crash",
          len(res6["errors"]) == 1 and res6["merged"] == [])
    res7 = consolidate([a, b], lambda p: "garbage {")
    check("consolidate bad JSON -> error, no crash",
          len(res7["errors"]) == 1 and res7["merged"] == [])
    res8 = consolidate([a, b], lambda p: json.dumps({"relation": "whatever"}))
    check("consolidate unknown relation -> error, no crash",
          len(res8["errors"]) == 1 and res8["merged"] == [])
    check("consolidate empty input", consolidate([], dup_llm) ==
          {"merged": [], "superseded": [], "errors": []})


# ---------------------------------------------------------------- curator

def _curator_llm(prompt):
    if "coffee" in prompt:
        return json.dumps({"relation": "duplicate",
                           "merged_content": "User drinks black coffee in the morning"})
    if "meat" in prompt:
        return json.dumps({"relation": "contradiction"})
    return json.dumps({"relation": "distinct"})


def test_curator(tmpdir):
    store = make_store(tmpdir)
    a = store.add(make_learning("User drinks black coffee every morning",
                                LearningType.PREFERENCE, 0.9, updated="2026-10-15T09:00:00"))
    b = store.add(make_learning("User likes black coffee in the morning",
                                LearningType.PREFERENCE, 0.6, updated="2026-10-15T09:01:00"))
    old = store.add(make_learning("User eats meat with every meal daily", LearningType.FACT,
                                  0.9, updated="2026-10-10T10:00:00"))
    new = store.add(make_learning("User is vegetarian never eats meat daily", LearningType.FACT,
                                  0.5, updated="2026-10-15T10:00:00"))
    lone = store.add(make_learning("Completely unrelated piano hobby note", LearningType.FACT,
                                   0.7, updated="2026-10-15T08:00:00"))

    report = run_curation(store, _curator_llm)
    check("curator report merged", report["merged"] == 1)
    check("curator report superseded", report["superseded"] == 3)
    check("curator report kept", report["kept"] == 2)
    check("curator report errors empty", report["errors"] == [])

    active = store.list()
    check("curator active count", len(active) == 3)  # merged + vegetarian winner + lone
    check("curator merged stored with real id",
          any(l.content == "User drinks black coffee in the morning" for l in active))
    merged_real = next(l for l in active
                       if l.content == "User drinks black coffee in the morning")
    check("curator merged has real id", merged_real.id.startswith("learn_"))
    check("curator duplicate olds point at merged",
          store.get(a).superseded_by == merged_real.id
          and store.get(b).superseded_by == merged_real.id)
    check("curator contradiction loser points at winner",
          store.get(old).superseded_by == new)
    check("curator vegetarian winner stays active",
          store.get(new).superseded_by is None)
    check("curator distinct item untouched", store.get(lone).superseded_by is None)
    check("curator merged confidence = max",
          abs(merged_real.confidence - 0.9) < 1e-9)

    # single learning: nothing to do
    store2 = make_store(Path(tmpdir) / "one")
    store2.add(make_learning("solo note"))
    r2 = run_curation(store2, _curator_llm)
    check("curator single -> zeros", r2 == {"merged": 0, "superseded": 0, "kept": 1,
                                           "errors": []})
    # llm always failing, but a candidate group exists: no crash,
    # nothing merged/superseded, everything kept, errors recorded
    store3 = make_store(Path(tmpdir) / "failing")
    store3.add(make_learning("User drinks black coffee every morning",
                             LearningType.PREFERENCE, 0.9))
    store3.add(make_learning("User likes black coffee in the morning",
                             LearningType.PREFERENCE, 0.6))
    r3 = run_curation(store3, _raising_llm)
    check("curator llm failing -> kept, errors recorded",
          r3["merged"] == 0 and r3["superseded"] == 0 and r3["kept"] == 2
          and len(r3["errors"]) > 0)
    check("curator llm failing -> nothing superseded in store",
          all(l.superseded_by is None for l in store3.list()))
    check("curator default max_items", _default_max_items())


def _default_max_items():
    import inspect
    return inspect.signature(run_curation).parameters["max_items"].default == 50


# ---------------------------------------------------------------- tools

class _Env:
    def __init__(self, root_dir, llm):
        self.root_dir = Path(root_dir)
        self.llm = llm


def test_tools(tmpdir):
    env = _Env(tmpdir, _FAKE_EXTRACT)
    tools = build_tools(env)
    check("tools names", set(tools) == {"learn_from_conversation", "recall_learned",
                                        "forget_learning"})
    for name, t in tools.items():
        check(f"tools {name} shape",
              set(t) == {"name", "func", "description", "schema"} and t["name"] == name
              and callable(t["func"]) and isinstance(t["description"], str)
              and t["schema"]["type"] == "object")

    # missing llm
    class _NoLlm:
        def __init__(self, root):
            self.root_dir = Path(root)
    r = build_tools(_NoLlm(tmpdir))["learn_from_conversation"]["func"](
        "User prefers dark mode, remember this")
    check("tools no llm -> error", r == {"error": "no llm configured"})
    # non-callable llm
    r = build_tools(_Env(tmpdir, "not-callable"))["learn_from_conversation"]["func"](
        "User prefers dark mode, remember this")
    check("tools non-callable llm -> error", r == {"error": "no llm configured"})
    # env is a plain Path: learn errors (no llm), recall/forget work
    r = build_tools(Path(tmpdir))["learn_from_conversation"]["func"](
        "User prefers dark mode, remember this")
    check("tools Path env learn -> no llm error", r == {"error": "no llm configured"})
    # no root at all: error envelope
    r = build_tools(None)["recall_learned"]["func"]("x")
    check("tools no root -> error", isinstance(r, dict) and "error" in r)

    # skipped path
    r = tools["learn_from_conversation"]["func"]("hello, how is the weather today")
    check("tools skipped", r == {"status": "skipped", "reason": "nothing durable to learn"})

    # real learn: stored into <root_dir>/memories/learnings.db
    r = tools["learn_from_conversation"]["func"](
        "User prefers dark mode, remember this. User lives in Hanoi, note that too.",
        source="chat-42")
    check("tools learned count", r.get("learned") == 4)  # secret/empty/nondict dropped
    check("tools ids returned", isinstance(r.get("ids"), list) and len(r["ids"]) == 4
          and all(i.startswith("learn_") for i in r["ids"]))
    db_path = Path(tmpdir) / "memories" / "learnings.db"
    check("tools db file exists", db_path.exists())
    store = LearningStore(db_path)
    check("tools learnings persisted", store.count() == 4)
    stored = [store.get(i) for i in r["ids"]]
    check("tools ids resolvable", all(s is not None for s in stored))
    check("tools source recorded", all(s.source == "chat-42" for s in stored))

    # recall
    r = tools["recall_learned"]["func"]("dark mode")
    check("tools recall finds", isinstance(r.get("learnings"), list)
          and len(r["learnings"]) == 1
          and r["learnings"][0]["content"] == "User prefers dark mode")
    check("tools recall returns dicts", all(isinstance(x, dict) for x in r["learnings"]))
    r = tools["recall_learned"]["func"]("User")
    confs = [x["confidence"] for x in r["learnings"]]
    check("tools recall confidence desc", confs == sorted(confs, reverse=True))
    r = tools["recall_learned"]["func"]("no-such-thing")
    check("tools recall empty", r == {"learnings": []})

    # forget
    target = tools["recall_learned"]["func"]("dark mode")["learnings"][0]["id"]
    check("tools forget true", tools["forget_learning"]["func"](target) == {"removed": True})
    check("tools forget again false",
          tools["forget_learning"]["func"](target) == {"removed": False})
    check("tools forgotten gone from recall",
          tools["recall_learned"]["func"]("dark mode") == {"learnings": []})

    # learn tool never raises on broken llm output
    env_bad = _Env(tmpdir, lambda p: "garbage")
    r = build_tools(env_bad)["learn_from_conversation"]["func"]("remember: I like tea")
    check("tools broken llm JSON -> learned 0", r == {"learned": 0, "ids": []})


# ---------------------------------------------------------------- main

if __name__ == "__main__":
    print("== learning ==")
    test_models()
    with tempfile.TemporaryDirectory() as tmp:
        test_store(tmp)
    test_extractor()
    test_consolidator()
    with tempfile.TemporaryDirectory() as tmp:
        test_curator(tmp)
    with tempfile.TemporaryDirectory() as tmp:
        test_tools(tmp)
    print(f"ALL LEARNING TESTS PASSED ({CHECKS} checks)")
