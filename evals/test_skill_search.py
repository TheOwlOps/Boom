import sys
"""Tests for the upgraded SkillsHub.search_catalog() scoring search.

Builds a temporary 5-skill catalog:
  1. pdf-editor      — query token appears in the skill NAME
  2. doc-helper      — query token appears in frontmatter DESCRIPTION
  3. text-tools      — query token appears in SKILL.md body CONTENT
  4. image-tools     — does NOT contain the query at all
  5. pdf-freq        — contains the token several times in the body
                       (presence per (token, field) counts once)

Run:  python3 test_skill_search.py
"""

import tempfile
from pathlib import Path

# Import root is agent/src (repo convention); the modules under test live
# in subpackages there (they previously lived next to this file).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))
from extensibility.skills_hub import SkillsHub


def make_catalog(base: Path) -> Path:
    catalog = base / "catalog"
    skills = {
        # 1. name match: token in skill name
        "pdf-editor": (
            "---\nname: pdf-editor\ndescription: Edit and manipulate files\n---\n"
            "This skill helps you work with documents."
        ),
        # 2. description match: token in frontmatter description
        "doc-helper": (
            "---\nname: doc-helper\ndescription: Merge and split PDF files quickly\n---\n"
            "A helper for various document tasks."
        ),
        # 3. content match: token in SKILL.md body only
        "text-tools": (
            "---\nname: text-tools\ndescription: Process plain text files\n---\n"
            "Also converts markdown and pdf into readable text."
        ),
        # 4. no match at all
        "image-tools": (
            "---\nname: image-tools\ndescription: Resize and crop pictures\n---\n"
            "Works with png and jpeg images."
        ),
        # 5. contains the token multiple times in the body
        #    (still scores +1 per (token, field) — presence based)
        "repeat-tools": (
            "---\nname: pdf-freq\ndescription: Nothing relevant here\n---\n"
            "pdf pdf pdf pdf pdf — repetition does not inflate the score."
        ),
    }
    for name, md in skills.items():
        d = catalog / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(md, encoding="utf-8")
    return catalog


def check(label: str, cond: bool) -> None:
    print(("PASS" if cond else "FAIL") + f": {label}")
    if not cond:
        raise AssertionError(label)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        catalog = make_catalog(base)
        hub = SkillsHub(base / "installed", catalog)

        # Test 1: query matching the name ranks first with the highest score
        res = hub.search_catalog("pdf")
        scores = {r["name"]: r["score"] for r in res}
        print("scores:", scores)
        check("name-match skill present", "pdf-editor" in scores)
        check("name-match ranks first", res[0]["name"] == "pdf-editor")
        check("name-match has top score", scores["pdf-editor"] == max(scores.values()))
        check("name(3) > description(2) > content(1)",
              scores["pdf-editor"] == 3 and scores["doc-helper"] == 2
              and scores["text-tools"] == 1 and scores["repeat-tools"] == 1)
        check("no-match skill excluded", "image-tools" not in scores)
        check("preview is 120 chars", all(len(r["preview"]) <= 120 for r in res))

        # Test 2: multi-word query — both tokens beat one token
        res2 = hub.search_catalog("pdf edit")
        s2 = {r["name"]: r["score"] for r in res2}
        print("multi-word scores:", s2)
        # pdf-editor: "pdf" in name (+3), "edit" in name (+3) = 6 (plus any body hits)
        # text-tools: only "pdf" in body (+1)
        check("both-tokens outscores one-token", s2["pdf-editor"] > s2["text-tools"])

        # Test 3: query matching nothing -> []
        check("no-match query returns []", hub.search_catalog("zzzznothing") == [])

        # Test 4: limit respected
        res4 = hub.search_catalog("pdf", limit=2)
        check("limit=2 returns exactly 2", len(res4) == 2)
        check("limit keeps top-ranked", res4[0]["name"] == "pdf-editor")

        # Test 5: case-insensitive
        res5 = hub.search_catalog("PDF")
        check("uppercase matches lowercase",
              [r["name"] for r in res5] == [r["name"] for r in hub.search_catalog("pdf")])

        # Bonus: empty query keeps old behavior -> []
        check("empty query returns []", hub.search_catalog("") == [])
        check("whitespace query returns []", hub.search_catalog("   ") == [])

        print("\nAll tests passed.")


if __name__ == "__main__":
    main()
