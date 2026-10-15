"""Fuzzy find-and-replace for agent file modifications.

Permissive strategies ensure whitespace, indentation, line endings, and Unicode
drift in LLM tool calls do not cause edit failures.
"""

from difflib import SequenceMatcher
from typing import Optional, Tuple

UNICODE_MAP = {
    "\u201c": '"', "\u201d": '"',
    "\u2018": "'", "\u2019": "'",
    "\u2014": "--", "\u2013": "-",
    "\u2026": "...", "\u00a0": " ",
    "\u2212": "-",
}

def unicode_normalize(text: str) -> str:
    for k, v in UNICODE_MAP.items():
        text = text.replace(k, v)
    return text

def fuzzy_find_and_replace(
    content: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False
) -> Tuple[str, int, str, Optional[str]]:
    """Applies find-and-replace with fallback fuzzy matching strategies.

    Returns: (new_content, match_count, strategy_used, error_message)
    """
    if old_string == new_string:
        return content, 0, "none", "old_string and new_string are identical."

    # Strategy 1: Exact Match
    if old_string in content:
        if replace_all:
            count = content.count(old_string)
            return content.replace(old_string, new_string), count, "exact", None
        count = content.count(old_string)
        if count == 1:
            return content.replace(old_string, new_string, 1), 1, "exact", None
        return content, count, "exact", f"old_string matches {count} times (must be unique or use replace_all=True)."

    # Strategy 2: Normalized Line Endings (\r\n -> \n)
    content_lf = content.replace("\r\n", "\n")
    old_lf = old_string.replace("\r\n", "\n")
    new_lf = new_string.replace("\r\n", "\n")
    if old_lf in content_lf:
        if replace_all:
            count = content_lf.count(old_lf)
            return content_lf.replace(old_lf, new_lf), count, "crlf_normalized", None
        count = content_lf.count(old_lf)
        if count == 1:
            return content_lf.replace(old_lf, new_lf, 1), 1, "crlf_normalized", None

    # Strategy 3: Unicode Normalization
    content_norm = unicode_normalize(content_lf)
    old_norm = unicode_normalize(old_lf)
    if old_norm in content_norm:
        idx = content_norm.find(old_norm)
        new_c = content_lf[:idx] + new_lf + content_lf[idx + len(old_norm):]
        return new_c, 1, "unicode_normalized", None

    # Strategy 4: Trailing Whitespace Stripped per line
    content_lines = [l.rstrip() for l in content_lf.split("\n")]
    old_lines = [l.rstrip() for l in old_lf.split("\n")]
    joined_c = "\n".join(content_lines)
    joined_o = "\n".join(old_lines)
    if joined_o in joined_c:
        idx = joined_c.find(joined_o)
        new_c = joined_c[:idx] + new_lf + joined_c[idx + len(joined_o):]
        return new_c, 1, "trimmed_whitespace", None

    # Strategy 5: Approximate SequenceMatcher Substring Match (>0.85 similarity)
    old_len = len(old_lf)
    best_ratio = 0.0
    best_start = -1
    best_end = -1

    step = max(1, old_len // 5)
    for start in range(0, max(1, len(content_lf) - old_len + 1), step):
        window = content_lf[start:start + old_len]
        matcher = SequenceMatcher(None, window, old_lf)
        ratio = matcher.quick_ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = start
            best_end = start + old_len

    if best_ratio >= 0.85 and best_start != -1:
        new_c = content_lf[:best_start] + new_lf + content_lf[best_end:]
        return new_c, 1, f"fuzzy_similarity_{best_ratio:.2f}", None

    return content, 0, "none", "Could not locate old_string in file."
