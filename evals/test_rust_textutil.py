"""Tests for textutil.py (Python wrapper over the boom-text Rust binary).

Run: python3 test_rust_textutil.py
The Rust binary is discovered via CARGO_TARGET_DIR (pointed at the local
target/ dir). Fallback behavior is tested by forcing BOOM_TEXT_BINARY to a
nonexistent path.
"""

import collections
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "agent" / "src" / "rust_bridge"))

# Point CARGO_TARGET_DIR at this module's target/ so the wrapper finds the
# binary we just built; clear any env override so it does not win.
os.environ["CARGO_TARGET_DIR"] = str(HERE / "target")
os.environ.pop("BOOM_TEXT_BINARY", None)

import textutil  # noqa: E402

STOPWORDS = textutil.STOPWORDS

PASS = 0


def check(name, cond):
    global PASS
    assert cond, f"FAILED: {name}"
    PASS += 1
    print(f"  ok: {name}")


def reference_keywords(text):
    c = collections.Counter()
    for tok in text.split():
        w = tok.lower()
        if len(w) >= 3 and w.isalpha() and w not in STOPWORDS:
            c[w] += 1
    return [{"word": w, "count": n}
            for w, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:10]]


SAMPLE = "Xin chào thế giới\nRust thật nhanh.\nEmoji: 🦉🚀 café\n\nDòng cuối 123."

print("== stats via wrapper (binary) ==")
s = textutil.rust_text_stats(SAMPLE)
check("stats.lines", s["lines"] == len(SAMPLE.split("\n")) == 5)
check("stats.words", s["words"] == len(SAMPLE.split()) == 13)
check("stats.chars unicode-safe", s["chars"] == len(SAMPLE) == 65)
check("stats.est_tokens ceil(chars/4)", s["est_tokens"] == (65 + 3) // 4 == 17)
check("stats.error null", s["error"] is None)

print("== keywords via wrapper (binary) ==")
KW = ("Rust is fast and safe. The rust compiler is fast. Safe code and fast code. "
      "Boom uses rust for agents. Agents run tools; tools return data. Data flows fast. "
      "The the and for are stopwords. Hi! ok? a bb ccc dddd")
kw = textutil.rust_keywords(KW)
exp_kw = reference_keywords(KW)
check("keywords top order matches Counter reference", kw == exp_kw)
check("keywords top word is rust x3", kw[0] == {"word": "rust", "count": 3})
check("keywords capped at 10", len(kw) <= 10)
check("keywords stopwords excluded",
      all(e["word"] not in {"the", "and", "for", "are"} for e in kw))
check("keywords non-alpha tokens excluded",
      all(e["word"] not in {"hi!", "ok?", "tools;"} for e in kw))

print("== lines_around via wrapper (binary) ==")
BIG = "\n".join(f"line {i} body" for i in range(1, 21))
w = textutil.rust_lines_around(BIG, 5, context=3)
check("lines_around window", [l["n"] for l in w["lines"]] == [2, 3, 4, 5, 6, 7, 8])
check("lines_around start/end", w["start_line"] == 2 and w["end_line"] == 8)
check("lines_around text payload", w["lines"][0] == {"n": 2, "text": "line 2 body"})
w = textutil.rust_lines_around(BIG, 0, context=3)  # clamp low
check("lines_around clamp low", w["start_line"] == 1 and w["end_line"] == 4)
w = textutil.rust_lines_around(BIG, 10**9, context=3)  # clamp high
check("lines_around clamp high", w["start_line"] == 17 and w["end_line"] == 20)

print("== empty text ==")
s = textutil.rust_text_stats("")
check("empty stats all zero",
      s["lines"] == 0 and s["words"] == 0 and s["chars"] == 0 and s["est_tokens"] == 0)

print("== fallback when binary missing ==")
os.environ["BOOM_TEXT_BINARY"] = "/nonexistent/boom-text"  # force miss
s = textutil.rust_text_stats(SAMPLE)
check("fallback stats correct", s["lines"] == 5 and s["chars"] == 65 and s["error"] is None)
kw = textutil.rust_keywords(KW)
check("fallback keywords correct", kw == exp_kw)
w = textutil.rust_lines_around(BIG, 5, context=3)
check("fallback lines_around correct", w["start_line"] == 2 and w["end_line"] == 8)

os.environ.pop("BOOM_TEXT_BINARY", None)
print(f"\n{PASS} assertions PASSED")
