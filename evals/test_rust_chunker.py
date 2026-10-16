"""Tests for agent/src/rust_bridge/chunker.py (and the boom-chunk Rust binary).

Run: python3 test_rust_chunker.py
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             os.pardir, "agent", "src", "rust_bridge"))

import chunker

PASS = 0


def check(name, cond):
    global PASS
    assert cond, f"FAILED: {name}"
    PASS += 1
    print(f"  ok: {name}")


def main():
    # Point the wrapper at the freshly built binary via env override.
    here = os.path.dirname(os.path.abspath(__file__))
    binary = os.path.join(here, os.pardir, "target", "release", "boom-chunk")
    if os.path.isfile(binary):
        os.environ["BOOM_CHUNK_BINARY"] = binary
    print(f"binary under test: {chunker._find_binary()}")

    # 1. sane chunk count for a multi-line document
    text = "\n".join(f"Line {i}: some sample content here" for i in range(40))
    chunks = chunker.rust_chunk(text, max_tokens=50, overlap=10)  # 200 chars max
    check("multi-line returns multiple chunks", len(chunks) > 1)

    # 2. every chunk within the char limit
    check("all chunks <= max_chars", all(len(c) <= 200 for c in chunks))

    # 3. overlap exists between consecutive chunks
    ok_overlap = True
    for a, b in zip(chunks, chunks[1:]):
        if b[:15] not in a:
            ok_overlap = False
    check("overlap present between consecutive chunks", ok_overlap)

    # 4. full coverage: every line appears in some chunk
    check(
        "all lines covered",
        all(any(f"Line {i}:" in c for c in chunks) for i in range(40)),
    )

    # 5. single very long line gets hard-split, still within limit
    chunks = chunker.rust_chunk("Q" * 2500, max_tokens=100, overlap=20)
    check("long line hard-split into >= 6 chunks", len(chunks) >= 6)
    check("hard-split chunks <= limit", all(len(c) <= 400 for c in chunks))

    # 6. empty text -> []
    check("empty text -> []", chunker.rust_chunk("", max_tokens=100) == [])

    # 7. unicode (Vietnamese) is never split mid-char
    text = ("Tiếng Việt có dấu: à á ả ã ạ ă â ê ô ơ ư đ — “trích dẫn”\n" * 30)
    chunks = chunker.rust_chunk(text, max_tokens=60, overlap=12)
    check("unicode chunks within limit", all(len(c) <= 240 for c in chunks))
    for c in chunks:
        c.encode("utf-8")  # would raise on broken surrogate/char
    check("unicode chunks encode cleanly", True)
    check("unicode content preserved", any("Tiếng Việt" in c for c in chunks))

    # 8. binary error responses don't raise: max_tokens <= 0 -> []
    check("max_tokens=0 -> [] without raising", chunker.rust_chunk("abc", max_tokens=0) == [])

    # 9. fallback when binary path is bogus: still returns chunks, no raise
    os.environ["BOOM_CHUNK_BINARY"] = "/nonexistent/boom-chunk"
    # clear CARGO_TARGET_DIR influence and repo-path lookup won't exist either
    chunks = chunker.rust_chunk(text, max_tokens=60, overlap=12)
    check("fallback works with bogus binary path", len(chunks) > 1)
    check("fallback chunks within limit", all(len(c) <= 240 for c in chunks))
    check("fallback covers content", any("Tiếng Việt" in c for c in chunks))

    # 10. fallback with binary env var unset and no binary anywhere
    if "BOOM_CHUNK_BINARY" in os.environ:
        del os.environ["BOOM_CHUNK_BINARY"]
    old_find = chunker._find_binary
    chunker._find_binary = lambda: None  # simulate "no binary on this machine"
    try:
        chunks = chunker.rust_chunk("hello\nworld\n" * 50, max_tokens=25, overlap=5)
        check("fallback with no binary at all", len(chunks) > 1)
    finally:
        chunker._find_binary = old_find

    print(f"\n{PASS} assertions PASSED")


if __name__ == "__main__":
    main()
