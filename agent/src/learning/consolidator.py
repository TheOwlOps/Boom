"""Consolidate learnings: gop trung lap, giai quyet mau thuan.

Sau khi extractor trich nhieu learning roi rac, module nay don dep:

  - Buoc 1 (heuristic, khong can llm): gom cac learning cung type co
    >= 3 tu khoa chung (tu dai >= 3 ky tu, bo stopwords don gian)
    thanh candidate group (union-find tren cap doi).
  - Buoc 2: moi group > 1 item -> hoi llm dung 1 turn, phan loai
    duplicate | contradiction | distinct.

Quy tac xu ly:
  - duplicate: tao 1 Learning moi gop (content do llm viet, confidence = max,
    type giu nguyen, id de placeholder "__pending_N__"); cac cu duoc danh dau
    superseded_by -> placeholder do. Curator se store.add() lay id that roi
    supersede tung cai cu.
  - contradiction: giu cai co (updated_at moi hon, roi confidence cao hon);
    cai thua superseded_by -> id that cua cai thang.
  - distinct / llm fail / JSON hong / relation la: giu nguyen tat ca,
    KHONG gop bua. Loi ghi vao errors, khong raise.

Chi stdlib.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set, Tuple

from learning.models import Learning

# Nguong heuristic buoc 1
_MIN_SHARED_WORDS = 3
_MIN_WORD_LEN = 3

# Stopwords don gian (khong phai danh sach day du, chi de loc tu vo nghia
# khoi phep giao tu khoa). Giua lai phu dinh ("khong"/"not") vi no mang nghia.
_STOPWORDS = frozenset(
    """
    anh chi em toi to minh ban no han ho chung ta
    nay kia do day ay cai nhung cac mot la thi ma
    va voi cua trong tren duoi moi moi tung cung da dang
    se rat qua lam thoi nhe nha a oi hay hoac nhung
    vi nen neu khi de co den tu cho ve lai nay
    """.split()
    + """
    the a an and or but for nor with from that this these those
    are was were has have had been will would can could should
    about into over after before when where which what who how why
    all any each few more most other some such only own same than
    too very just also like likes liked get got you your yours
    our ours his her its their them they he she it we not no
    do does did done is am be as at by in of on to up so if
    my me him us
    """.split()
)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _significant_words(text: str) -> Set[str]:
    """Tu khoa co nghia: tu dai >= 3 ky tu, bo stopwords."""
    words = re.findall(r"\w+", (text or "").lower(), flags=re.UNICODE)
    return {w for w in words if len(w) >= _MIN_WORD_LEN and w not in _STOPWORDS}


def _candidate_groups(learnings: List[Learning]) -> List[List[Learning]]:
    """Gom group bang union-find: edge neu cung type va >=3 tu khoa chung."""
    n = len(learnings)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    sigs = [_significant_words(l.content) for l in learnings]
    for i in range(n):
        for j in range(i + 1, n):
            if learnings[i].type != learnings[j].type:
                continue
            if len(sigs[i] & sigs[j]) >= _MIN_SHARED_WORDS:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    buckets: Dict[int, List[Learning]] = {}
    for i in range(n):
        buckets.setdefault(find(i), []).append(learnings[i])
    return list(buckets.values())


def _build_prompt(group: List[Learning]) -> str:
    lines = [
        "Ban la bo gop kien thuc. Duoi day la cac mau kien thuc cung loai "
        "'%s':" % group[0].type.value,
        "",
    ]
    for i, l in enumerate(group, 1):
        lines.append('%d. [id=%s, confidence=%.2f] "%s"' % (i, l.id, l.confidence, l.content))
    lines += [
        "",
        "May cau nay: TRUNG Y (duplicate), MAU THUAN (contradiction),",
        "hay KHAC NHAU HAN (distinct)?",
        "- duplicate: cung mot y, gop duoc thanh mot cau.",
        "- contradiction: hai y doi lap, khong the cung dung.",
        "- distinct: noi ve chuyen khac nhau, giu nguyen.",
        "",
        "Tra loi DUY NHAT mot JSON, khong them chu nao khac:",
        '{"relation": "duplicate|contradiction|distinct", "merged_content": "..."}',
        "merged_content chi can khi duplicate: mot cau gop ngan gon, giu day du y.",
    ]
    return "\n".join(lines)


def _extract_json(text: str) -> Optional[dict]:
    """Boc JSON tu text llm (chiu duoc code fence / chu thua xung quanh)."""
    if not text or not isinstance(text, str):
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.DOTALL)
    if m:
        t = m.group(1)
    else:
        m2 = re.search(r"\{.*\}", t, re.DOTALL)
        if m2:
            t = m2.group(0)
    try:
        obj = json.loads(t)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _merge_duplicate(group: List[Learning], merged_content: str, now: str) -> Learning:
    """Tao Learning gop tu group duplicate. id de trong -> curator gan sau."""
    content = (merged_content or "").strip()
    if not content:
        # llm khong dua content gop: dung content cua cai confidence cao nhat,
        # khong tu che ra cau moi.
        content = max(group, key=lambda l: l.confidence).content
    sources = [l.source for l in group if l.source]
    created = min((l.created_at for l in group if l.created_at), default="")
    return Learning(
        id="",
        type=group[0].type,
        content=content,
        confidence=max(l.confidence for l in group),
        source="; ".join(dict.fromkeys(sources)),
        created_at=created or now,
        updated_at=now,
        superseded_by=None,
    )


def _resolve_contradiction(group: List[Learning]) -> Tuple[Learning, List[Learning]]:
    """Tra ve (winner, losers). Winner = updated_at moi nhat, roi confidence cao nhat."""
    ordered = sorted(group, key=lambda l: (l.updated_at or "", l.confidence), reverse=True)
    return ordered[0], ordered[1:]


def consolidate(
    learnings: List[Learning], llm: Callable[[str], str]
) -> Dict[str, object]:
    """Consolidate list learnings (chua superseded).

    Tra ve {"merged": [Learning], "superseded": [(old_id, new_id)],
             "errors": [str]}.
      - merged: Learning moi (duplicate). id la placeholder "__pending_N__";
        curator store.add() se gan id that.
      - superseded: cap (old_id, new_id) trong do new_id la placeholder
        (duplicate) hoac id that cua winner (contradiction).
      - errors: cac ca llm fail / JSON hong / relation la - da giu nguyen,
        khong crash.
    """
    merged: List[Learning] = []
    superseded: List[Tuple[str, str]] = []
    errors: List[str] = []
    pending_idx = 0

    for group in _candidate_groups(learnings):
        if len(group) < 2:
            continue
        group_ids = [l.id for l in group]
        try:
            raw = llm(_build_prompt(group))
        except Exception as exc:  # llm fail -> giu nguyen, khong gop bua
            errors.append("llm failed for group %s: %r" % (group_ids, exc))
            continue

        obj = _extract_json(raw)
        norm = {str(k).lower(): v for k, v in obj.items()} if obj else {}
        relation = str(norm.get("relation") or "").strip().lower()
        if relation not in ("duplicate", "contradiction", "distinct"):
            errors.append(
                "llm returned unusable relation for group %s: %.200r" % (group_ids, raw)
            )
            continue
        if relation == "distinct":
            continue

        if relation == "duplicate":
            new = _merge_duplicate(group, norm.get("merged_content") or "", _now_iso())
            placeholder = "__pending_%d__" % pending_idx
            pending_idx += 1
            new.id = placeholder
            merged.append(new)
            for l in group:
                superseded.append((l.id, placeholder))
        else:  # contradiction
            winner, losers = _resolve_contradiction(group)
            for loser in losers:
                superseded.append((loser.id, winner.id))

    return {"merged": merged, "superseded": superseded, "errors": errors}
