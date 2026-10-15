"""Runner chay dinh ky cho trigger: check -> action -> mark_fired.

Vong lap cua 1 lan chay (VD: duoc goi tu cron hoac daemon):

1. Duyet store.list(enabled_only=True); moi trigger goi check_trigger
   (khong bao gio raise — checkers.py dam bao).
2. Trigger fired nhung chua qua cooldown -> skip (khong notify, khong mark).
3. Trigger fired + qua cooldown -> chay action:
   - {"type": "notify", ...}: format template roi goi notify_fn(channel,
     target, text). notify_fn raise -> bat, ghi errors, KHONG mark_fired
     (de lan sau thu lai).
   - {"type": "log"}: chi mark_fired.
   - type la -> ghi errors, khong mark.
4. Action thanh cong -> store.mark_fired(trigger.id, at=<newest item id>),
   neu khong trich duoc id thi mark_fired mac dinh (timestamp hien tai).

Format detail tu checkers.py (de trich newest item id):
  gmail_new (first):  "first check: %d message(s), newest id=%s"
  gmail_new (lan sau): "new message id=%s (prev=%s), %d message(s), subject=%r"
  keyword_news (first): "first check: %d headline(s), newest: %r"   (KHONG co id)
  keyword_news (lan sau): "new headline (prev=%s): %r"              (chi co prev id)
  price_below: "price %s < threshold %s: %s"                         (khong co item id)
  webhook: "webhook invoked"                                         (khong co item id)

-> Chi gmail_new mang newest id trong detail. keyword_news/price_below/webhook
   fallback ve mark_fired(trigger.id) voi timestamp mac dinh.

COOLDOWN — limitation da biet:
  `last_fired_at` dang giu item id (phuc vu newness check kieu "moi = id khac"),
  nen KHONG the dung no de do cooldown. Cooldown tracking la in-memory,
  per-process: dict module-level _LAST_TS {trigger_id: last_fire_timestamp}.
  He qua:
  - Restart process -> mat lich su cooldown -> trigger co the fire ngay lan
    chay dau tien sau restart (check_trigger van bao ve bang newness check).
  - Moi process co bang cooldown rieng (neu chay nhieu process song song).
  - De test duoc voi thoi gian gia, run_checks nhan `clock` callable
    (mac dinh time.time).

Chi stdlib. Khong raise ra ngoai.
"""

import re
import time
from typing import Any, Callable, Dict, List

from triggers.checkers import check_trigger

#: Cooldown tracking in-memory per-process: {trigger_id: last_fire_epoch}.
#: Xem docstring module ve limitation.
_LAST_TS: Dict[str, float] = {}


def _extract_newest_id(trigger, detail: str):
    """Trich newest item id tu detail cua checkers.py. None neu khong co."""
    if not detail or trigger.kind not in ("gmail_new", "keyword_news"):
        return None
    m = re.search(r"\bid=(\S+)", detail)
    return m.group(1) if m else None


def run_checks(store, ctx: Dict[str, Any], notify_fn: Callable,
               clock: Callable[[], float] = None) -> Dict[str, Any]:
    """Chay 1 vong check cho tat ca trigger dang bat.

    Args:
        store: TriggerStore.
        ctx: dict callables cho check_trigger (gmail_search, get_price,
            news_search...).
        notify_fn: callable (channel, target, text) -> None, runner tu cung cap.
        clock: callable tra ve epoch seconds; mac dinh time.time.
            Dung de test voi thoi gian gia.

    Returns:
        {"checked": int, "fired": [ids], "notified": [ids],
         "skipped_cooldown": [ids], "errors": [str]}.
        - checked: so trigger enabled da duyet.
        - fired: id cua trigger ma checker bao fired (ke ca bi skip cooldown
          hay action loi).
        - notified: id cua trigger ma action hoan thanh (notify da gui hoac
          action log da mark_fired).
        - skipped_cooldown: fired nhung chua qua cooldown -> bo qua hoan toan.
        - errors: chuoi mo ta loi.
    """
    if clock is None:
        clock = time.time
    report: Dict[str, Any] = {
        "checked": 0,
        "fired": [],
        "notified": [],
        "skipped_cooldown": [],
        "errors": [],
    }
    try:
        triggers = store.list(enabled_only=True)
    except Exception as exc:
        report["errors"].append("could not list triggers: %s" % (exc,))
        return report
    report["checked"] = len(triggers)
    now = clock()
    for trigger in triggers:
        tid = getattr(trigger, "id", "?")
        try:
            res = check_trigger(trigger, ctx)
        except Exception as exc:  # phong thu; checkers.py cam ket khong raise
            report["errors"].append(
                "trigger %s: checker raised (unexpected): %s" % (tid, exc))
            continue
        if not res.get("fired"):
            continue
        report["fired"].append(tid)
        cooldown = getattr(trigger, "cooldown_sec", 3600) or 0
        if now - _LAST_TS.get(tid, 0) < cooldown:
            report["skipped_cooldown"].append(tid)
            continue
        action = getattr(trigger, "action", None) or {}
        atype = action.get("type")
        if atype == "notify":
            channel = action.get("channel")
            target = action.get("target")
            template = action.get("template")
            if not channel or not target or not template:
                report["errors"].append(
                    "trigger %s: notify action missing channel/target/template"
                    % (tid,))
                continue
            try:
                text = template.format(
                    detail=res.get("detail", ""),
                    name=getattr(trigger, "name", ""))
            except Exception:
                text = template  # format loi -> dung template nguyen
            try:
                notify_fn(channel, target, text)
            except Exception as exc:
                report["errors"].append(
                    "trigger %s: notify failed: %s" % (tid, exc))
                continue  # KHONG mark_fired — de lan sau thu lai
        elif atype == "log":
            pass  # chi mark_fired ben duoi
        else:
            report["errors"].append(
                "trigger %s: unknown action type %r" % (tid, atype))
            continue
        newest_id = _extract_newest_id(trigger, res.get("detail", ""))
        try:
            ok = (store.mark_fired(tid, at=newest_id) if newest_id is not None
                  else store.mark_fired(tid))
        except Exception as exc:
            report["errors"].append(
                "trigger %s: mark_fired failed: %s" % (tid, exc))
            continue
        if not ok:
            report["errors"].append(
                "trigger %s: mark_fired: trigger not found" % (tid,))
            continue
        _LAST_TS[tid] = now
        report["notified"].append(tid)
    return report


def reset_cooldown_state() -> None:
    """Xoa bang cooldown in-memory. Chu yeu dung cho test."""
    _LAST_TS.clear()
