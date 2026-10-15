"""Render the proactive briefing as compact Vietnamese markdown.

`build_briefing` takes a mapping of section name -> list of strings and returns a
dated markdown digest. Missing keys are skipped, sections with no items produce no
header, and a mapping with nothing renderable falls back to `empty_briefing()`.

Canonical sections (rendered in this order):
    goals      🎯 Mục tiêu
    people     👥 Người cần nhớ
    reminders  ⏰ Nhắc việc
    notes      📝 Ghi chú
Any other key is rendered after them under its own name.
"""

from datetime import date
from typing import List, Mapping, Optional, Sequence, Union

# (key, Vietnamese title), in render order.
_SECTION_TITLES = (
    ("goals", "\U0001f3af Mục tiêu"),
    ("people", "\U0001f465 Người cần nhớ"),
    ("reminders", "⏰ Nhắc việc"),
    ("notes", "\U0001f4dd Ghi chú"),
)

_WEEKDAYS = (
    "Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật",
)

EMPTY_MESSAGE = "Hôm nay không có gì mới."


def empty_briefing() -> str:
    """The digest text when there is nothing to report."""
    return EMPTY_MESSAGE


def _weekday_vn(day: date) -> str:
    return _WEEKDAYS[day.weekday()]


def _clean_items(value: Union[str, Sequence[str], None]) -> List[str]:
    """Normalise one section's value to a list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    items = []
    for raw in value:
        text = str(raw).strip()
        if text:
            items.append(text)
    return items


def build_briefing(
    sections: Optional[Mapping[str, Union[str, Sequence[str], None]]],
    on_date: Optional[date] = None,
) -> str:
    """Build the briefing markdown for `sections`.

    `sections` maps a section name to its items (a list of strings; a bare
    string counts as one item). Keys that are missing or render to no items are
    skipped entirely — no empty headers. With nothing renderable, returns
    `empty_briefing()`.
    """
    day = on_date or date.today()
    rendered: List[str] = []

    ordered_keys = [key for key, _ in _SECTION_TITLES]
    extra_keys = [k for k in (sections or {}) if k not in ordered_keys]
    titles = dict(_SECTION_TITLES)

    for key in ordered_keys + extra_keys:
        items = _clean_items((sections or {}).get(key))
        if not items:
            continue
        title = titles.get(key, str(key))
        block = [f"### {title}"] + [f"- {item}" for item in items]
        rendered.append("\n".join(block))

    if not rendered:
        return empty_briefing()

    header = f"📰 Bản tin — {_weekday_vn(day)}, {day.strftime('%d/%m/%Y')}"
    return header + "\n\n" + "\n\n".join(rendered)
