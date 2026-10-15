"""Tests for agent/src/tools/ocr_tool.py — real Tesseract, no stubs."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.expanduser(
    "~/workspace/boom-mods/upgrade1/agent/src/tools"))

from ocr_tool import (  # noqa: E402
    build_tools, ocr_image, ocr_image_url, _looks_like_image)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL:", name, detail)


def fuzzy(haystack, *needles):
    h = (haystack or "").lower().replace(" ", "")
    return all(n.lower().replace(" ", "") in h for n in needles)


def make_image(lines, path):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (700, 60 + 50 * len(lines)), "white")
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((20, 20 + 50 * i), line, fill="black")
    img.save(path)
    return path


TMP = tempfile.mkdtemp(prefix="boom_ocr_test_")
IMG_EN = make_image(["Hello OCR 123"], os.path.join(TMP, "en.png"))
IMG_VI = make_image(["Xin chao Viet Nam"], os.path.join(TMP, "vi.png"))
TXT = os.path.join(TMP, "note.txt")
open(TXT, "w").write("not an image")

# --- ocr_image: happy paths (real tesseract) ---
r = ocr_image(IMG_EN, "eng")
check("en text fuzzy", fuzzy(r.get("text", ""), "hello", "ocr"),
      repr(r.get("text")))
check("en no error", "error" not in r, repr(r))
check("en lang echoed", r.get("lang") == "eng", repr(r))
check("en char_count", r.get("char_count", 0) > 0, repr(r))

r = ocr_image(IMG_VI)  # default vie+eng
check("vi text fuzzy", fuzzy(r.get("text", ""), "xin", "viet", "nam"),
      repr(r.get("text")))

# --- ocr_image: error paths ---
r = ocr_image("/nonexistent/xyz.png")
check("missing file -> error", "error" in r and "not found" in r["error"],
      repr(r))

r = ocr_image("")
check("empty path -> error", "error" in r, repr(r))

r = ocr_image(TXT)
check("non-image -> error", "error" in r and "not a supported image" in r["error"],
      repr(r))

r = ocr_image(IMG_EN, "xx_invalid_lang")
check("bad lang -> error", "error" in r, repr(r))

r = ocr_image(IMG_EN, "")
check("empty lang -> error", "error" in r, repr(r))

# --- _looks_like_image ---
check("png ext ok", _looks_like_image(IMG_EN))
noext = os.path.join(TMP, "noext")
open(noext, "wb").write(open(IMG_EN, "rb").read())
check("magic bytes ok (no ext)", _looks_like_image(noext), noext)
check("txt not image", not _looks_like_image(TXT))

# --- ocr_image_url: error paths (no network needed) ---
r = ocr_image_url("")
check("empty url -> error", "error" in r, repr(r))

r = ocr_image_url("ftp://example.com/x.png")
check("non-http url -> error", "error" in r and "http" in r["error"], repr(r))

r = ocr_image_url("http://127.0.0.1:9/nope.png")
check("unreachable -> error", "error" in r and "download failed" in r["error"],
      repr(r))

# --- ocr_image_url: happy path via local HTTP server ---
import functools  # noqa: E402
import http.server  # noqa: E402
import threading  # noqa: E402

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=TMP)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
try:
    r = ocr_image_url("http://127.0.0.1:%d/en.png" % port, "eng")
    check("url ocr fuzzy", fuzzy(r.get("text", ""), "hello", "ocr"),
          repr(r.get("text")))
    check("url no error", "error" not in r, repr(r))
finally:
    srv.shutdown()

# --- build_tools convention ---
tools = build_tools(None)
check("build_tools keys", set(tools) == {"ocr_image", "ocr_image_url"},
      repr(sorted(tools)))
for name, entry in tools.items():
    check("%s has func" % name, callable(entry.get("func")))
    check("%s has description" % name, bool(entry.get("description")))
    check("%s schema props" % name,
          entry.get("schema", {}).get("type") == "object"
          and "properties" in entry["schema"])
check("tools work via build_tools",
      fuzzy(tools["ocr_image"]["func"](IMG_EN, "eng").get("text", ""),
            "hello", "ocr"))

print("=" * 40)
print("PASS: %d  FAIL: %d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
