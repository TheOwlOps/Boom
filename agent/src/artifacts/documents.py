"""DocumentBuilder: build finished documents under <root_dir>/artifacts/.

Every builder writes a real file and returns {"path", "size"}; anything it cannot
do comes back as {"error": ...}, never an exception and never a faked success.
Filenames are sanitized so a model-supplied name can never escape the artifacts
directory (path traversal is rejected, not cleaned up after).
"""

import csv
import html as html_lib
import io
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


def _slugify(text: str) -> str:
    """A filesystem-safe stem from a title: lowercase, runs of junk become one dash."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower())
    slug = slug.strip("-")
    return slug or "document"


class DocumentBuilder:
    """Writes Markdown / HTML / CSV / JSON artifacts below <root_dir>/artifacts/."""

    def __init__(self, root_dir):
        self.root_dir = Path(root_dir) if root_dir is not None else None
        self.out_dir = (self.root_dir / "artifacts") if self.root_dir is not None else None

    # ------------------------------------------------------------------ internals

    def _resolve(self, filename: str, default_suffix: str = "") -> Path:
        """A safe output path for `filename`, or ValueError when it is unusable.

        Only a bare filename is accepted: separators, absolute paths and ".."
        are rejected outright so output can never land outside artifacts/.
        """
        if self.out_dir is None:
            raise ValueError("no root_dir: nowhere to keep artifacts")
        name = (filename or "").strip()
        if not name:
            raise ValueError("filename is empty")
        norm = os.path.normpath(name)
        if (os.path.isabs(norm) or norm.startswith("..")
                or "/" in norm or "\\" in norm or norm in (".",)):
            raise ValueError(f"filename {name!r} is not allowed: must be a bare filename")
        if default_suffix and not norm.lower().endswith(default_suffix.lower()):
            norm += default_suffix
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = (self.out_dir / norm).resolve()
        if path.parent != self.out_dir.resolve():
            raise ValueError(f"filename {name!r} escapes the artifacts directory")
        return path

    def _write(self, path: Path, text: str, overwrite: bool) -> Dict[str, Any]:
        if path.exists() and not overwrite:
            return {"error": f"{path.name} already exists: pass overwrite=true to replace it"}
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as e:
            return {"error": f"could not write {path.name}: {e}"}
        return {"path": str(path), "size": path.stat().st_size}

    @staticmethod
    def _clean_title(title: str) -> str:
        """One line, no control characters: a title must not break the document head."""
        return re.sub(r"\s+", " ", (title or "").strip()) or "Untitled"

    # ------------------------------------------------------------------ builders

    def build_markdown(self, title: str, body_md: str, filename: Optional[str] = None,
                       overwrite: bool = False) -> Dict[str, Any]:
        """A Markdown report: '# title' header followed by the body."""
        try:
            path = self._resolve(filename or _slugify(title), ".md")
        except ValueError as e:
            return {"error": str(e)}
        title = self._clean_title(title)
        text = f"# {title}\n\n{(body_md or '').strip()}\n"
        return self._write(path, text, overwrite)

    def build_html(self, title: str, body_html: str, filename: Optional[str] = None,
                   overwrite: bool = False) -> Dict[str, Any]:
        """A complete standalone HTML page. The title is escaped; the body is the
        agent's HTML fragment, embedded as-is inside a minimal valid document."""
        try:
            path = self._resolve(filename or _slugify(title), ".html")
        except ValueError as e:
            return {"error": str(e)}
        title = self._clean_title(title)
        text = (
            "<!DOCTYPE html>\n"
            "<html lang=\"en\">\n<head>\n"
            "<meta charset=\"utf-8\">\n"
            f"<title>{html_lib.escape(title)}</title>\n"
            "</head>\n<body>\n"
            f"{(body_html or '').strip()}\n"
            "</body>\n</html>\n"
        )
        return self._write(path, text, overwrite)

    def build_csv(self, filename: str, rows: List[Dict[str, Any]],
                  overwrite: bool = False) -> Dict[str, Any]:
        """A CSV table from a list of dicts. The header is the ordered union of the
        row keys; missing cells are empty."""
        if not isinstance(rows, list) or not rows:
            return {"error": "rows must be a non-empty list of dicts"}
        if any(not isinstance(r, dict) for r in rows):
            return {"error": "every row must be a dict"}
        try:
            path = self._resolve(filename, ".csv")
        except ValueError as e:
            return {"error": str(e)}
        header: List[str] = []
        for row in rows:
            for key in row:
                if key not in header:
                    header.append(str(key))
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in header})
        return self._write(path, buf.getvalue(), overwrite)

    def build_json(self, filename: str, data: Any, overwrite: bool = False) -> Dict[str, Any]:
        """Pretty-printed JSON (UTF-8 kept as-is, not \\u-escaped)."""
        try:
            path = self._resolve(filename, ".json")
        except ValueError as e:
            return {"error": str(e)}
        try:
            text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        except (TypeError, ValueError) as e:
            return {"error": f"data is not JSON-serializable: {e}"}
        return self._write(path, text, overwrite)
