"""Offline, deterministic tests for the artifacts module.

Builds all four document types into a temp dir, checks the files exist with the
right content, and proves path traversal is rejected and overwrite=False holds.
Nothing here touches the network or the real repo.
"""

import csv
import json
import sys
import tempfile
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))

from artifacts.documents import DocumentBuilder
from artifacts.tools import build_tools


def check(name, cond):
    print(("[PASS] " if cond else "[FAIL] ") + name)
    if not cond:
        raise SystemExit(f"FAILED: {name}")


def test_markdown(tmp):
    b = DocumentBuilder(tmp)
    res = b.build_markdown("Bao cao tuan", "## Noi dung\n- muc 1")
    check("markdown ok", "path" in res and "size" in res and res["size"] > 0)
    p = Path(res["path"])
    check("markdown file exists", p.is_file() and p.parent.name == "artifacts")
    text = p.read_text(encoding="utf-8")
    check("markdown header", text.startswith("# Bao cao tuan\n"))
    check("markdown body kept", "## Noi dung" in text and "muc 1" in text)
    # auto-derived filename from the title
    check("markdown slug filename", p.name == "bao-cao-tuan.md")


def test_html(tmp):
    b = DocumentBuilder(tmp)
    res = b.build_html('Tieu de <b>"dac biet"</b>', "<p>Xin chao</p>", filename="page.html")
    check("html ok", "path" in res)
    text = Path(res["path"]).read_text(encoding="utf-8")
    check("html doctype", text.lstrip().startswith("<!DOCTYPE html>"))
    check("html title escaped",
          "<title>Tieu de &lt;b&gt;&quot;dac biet&quot;&lt;/b&gt;</title>" in text)
    check("html body embedded", "<p>Xin chao</p>" in text)
    check("html closed", text.rstrip().endswith("</html>"))


def test_csv(tmp):
    b = DocumentBuilder(tmp)
    rows = [{"ten": "An", "tuoi": 30}, {"ten": "Binh", "thanh_pho": "Hanoi"}]
    res = b.build_csv("people.csv", rows)
    check("csv ok", "path" in res)
    with open(res["path"], newline="", encoding="utf-8") as f:
        got = list(csv.DictReader(f))
    check("csv header union", list(got[0].keys()) == ["ten", "tuoi", "thanh_pho"])
    check("csv row values", got[0]["ten"] == "An" and got[0]["tuoi"] == "30")
    check("csv missing cell empty", got[1]["tuoi"] == "" and got[1]["thanh_pho"] == "Hanoi")
    # bad input is an error dict, not an exception
    check("csv empty rows error", "error" in b.build_csv("e.csv", []))
    check("csv non-dict row error", "error" in b.build_csv("e.csv", [{"a": 1}, "x"]))


def test_json(tmp):
    b = DocumentBuilder(tmp)
    data = {"ten": "An", "diem": [9, 8], "meta": {"nam": 2026}}
    res = b.build_json("data.json", data)
    check("json ok", "path" in res)
    check("json round trip", json.loads(Path(res["path"]).read_text(encoding="utf-8")) == data)
    check("json pretty", '  "ten": "An"' in Path(res["path"]).read_text(encoding="utf-8"))
    check("json unserializable error", "error" in b.build_json("e.json", {1, 2, 3}))


def test_path_traversal_blocked(tmp):
    b = DocumentBuilder(tmp)
    evil = ["../../x", "../secret.md", "/etc/passwd", "sub/dir.md", "..\\win.md", ""]
    for name in evil:
        res = b.build_markdown("t", "body", filename=name or "../../x")
        check(f"traversal rejected {name!r}", "error" in res)
    # nothing escaped: tmp contains only the artifacts dir with our legit files
    outside = [p for p in Path(tmp).rglob("*") if p.is_file()
               and "artifacts" not in p.parts]
    check("no file escaped artifacts/", outside == [])


def test_overwrite_guard(tmp):
    b = DocumentBuilder(tmp)
    r1 = b.build_markdown("T", "ban dau", filename="note.md")
    check("first write ok", "path" in r1)
    r2 = b.build_markdown("T", "ghi de", filename="note.md")
    check("overwrite=False refused", "error" in r2 and "overwrite" in r2["error"])
    check("content untouched",
          Path(r1["path"]).read_text(encoding="utf-8").strip().endswith("ban dau"))
    r3 = b.build_markdown("T", "ghi de", filename="note.md", overwrite=True)
    check("overwrite=True works", "path" in r3)
    check("content replaced",
          Path(r3["path"]).read_text(encoding="utf-8").strip().endswith("ghi de"))


def test_build_tools_wiring(tmp):
    class Env:
        root_dir = Path(tmp)

    tools = build_tools(Env())
    check("four tools", sorted(tools) == ["artifact_csv", "artifact_html",
                                          "artifact_json", "artifact_markdown"])
    for name, meta in tools.items():
        check(f"{name} dict shape",
              set(meta) == {"name", "func", "description", "schema"}
              and meta["name"] == name
              and meta["schema"]["type"] == "object")
    res = tools["artifact_markdown"]["func"]("Bao cao", "nd")
    check("tool func builds", "path" in res and Path(res["path"]).is_file())
    # no root_dir -> honest error, no surprise write
    tools2 = build_tools(object())
    check("no root_dir errors",
          all("error" in t["func"]("t", "b") for t in
              [tools2["artifact_markdown"], tools2["artifact_html"]]))
    check("no root_dir csv/json errors",
          "error" in tools2["artifact_csv"]["func"]("f.csv", [{"a": 1}])
          and "error" in tools2["artifact_json"]["func"]("f.json", {}))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_markdown(tmp)
        test_html(tmp)
        test_csv(tmp)
        test_json(tmp)
        test_path_traversal_blocked(tmp)
        test_overwrite_guard(tmp)
        test_build_tools_wiring(tmp)
    print("ALL ARTIFACT TESTS PASSED")
