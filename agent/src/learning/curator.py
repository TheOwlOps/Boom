"""Curator: chay consolidation dinh ky tren LearningStore.

run_curation(store, llm, max_items=50):
  1. Lay learnings non-superseded moi nhat (sort updated_at desc,
     toi da max_items).
  2. Goi consolidate().
  3. Moi merged moi: store.add() lay id that -> voi moi cap
     (old_id, new_id-placeholder): store.supersede(old_id, real_new_id).
     Cap contradiction dung san id that cua winner.
  4. Tra report {"merged": int, "superseded": int, "kept": int,
     "errors": [...]}.

Khong crash khi llm/store loi tung ca: ghi vao errors va tiep tuc.
Khong co gi de consolidate -> {"merged": 0, "superseded": 0, "kept": n}.

Chi stdlib.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from learning.models import Learning
from learning.store import LearningStore
from learning.consolidator import consolidate


def run_curation(
    store: LearningStore, llm: Callable[[str], str], max_items: int = 50
) -> Dict[str, object]:
    """Chay 1 vong curation tren store. llm la callable(prompt) -> str."""
    learnings: List[Learning] = store.list(include_superseded=False)
    learnings.sort(key=lambda l: l.updated_at or "", reverse=True)
    learnings = learnings[:max_items]
    n = len(learnings)
    if n < 2:
        return {"merged": 0, "superseded": 0, "kept": n, "errors": []}

    result = consolidate(learnings, llm)
    errors: List[str] = list(result.get("errors", []))

    # merged: placeholder id -> id that sau store.add()
    pending: Dict[str, str] = {}
    merged_ok = 0
    for m in result.get("merged", []):
        placeholder = m.id
        m.id = ""  # de store.add() sinh id that
        try:
            real_id = store.add(m)
        except Exception as exc:
            errors.append("store.add failed for merged %r: %r" % (placeholder, exc))
            continue
        pending[placeholder] = real_id
        merged_ok += 1

    superseded_ok = 0
    superseded_olds = set()
    for old_id, new_ref in result.get("superseded", []):
        real_new = pending.get(new_ref, new_ref)  # contradiction: new_ref da la id that
        try:
            store.supersede(old_id, real_new)
        except Exception as exc:
            errors.append(
                "store.supersede(%r -> %r) failed: %r" % (old_id, real_new, exc)
            )
            continue
        superseded_ok += 1
        superseded_olds.add(old_id)

    kept = n - len(superseded_olds)
    return {"merged": merged_ok, "superseded": superseded_ok, "kept": kept, "errors": errors}
