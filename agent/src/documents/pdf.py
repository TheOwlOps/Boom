"""PdfBuilder — tạo file PDF bằng reportlab nếu có,
fallback sang writer PDF tối giản viết tay (pure stdlib) nếu thiếu lib.

Fallback đủ để mọi PDF reader mở được: text (heading/paragraph/table/pagebreak),
không đẹp bằng reportlab nhưng không bao giờ crash vì thiếu lib.
"""
from __future__ import annotations

import io
from typing import List
from xml.sax.saxutils import escape as _html_escape

# ---------------------------------------------------------------------------
# Fallback writer PDF tối giản (viết tay, chỉ dùng stdlib).
#
# Cấu trúc 1 file PDF hợp lệ, tối thiểu:
#   %PDF-1.4 header
#   các object:  Catalog -> Pages -> Page -> Contents (stream) + Font
#   bảng xref: offset (byte) của từng object — reader dùng để nhảy tới object
#   trailer + startxref + %%EOF
#
# Giới hạn của fallback (ghi rõ để không ai bất ngờ):
#   - Chỉ dùng font Helvetica chuẩn của PDF (không embed font) -> ký tự ngoài
#     Latin-1 (vd: đ, ơ, ư, emoji) sẽ bị thay bằng '?'. Muốn tiếng Việt đẹp
#     thì cài reportlab.
#   - Bảng vẽ bằng text + đường kẻ đơn giản, không có style màu mè.
# ---------------------------------------------------------------------------

_PAGE_W, _PAGE_H = 595.0, 842.0          # A4, đơn vị point
_MARGIN = 50.0
_TOP_Y = _PAGE_H - _MARGIN
_BOTTOM_Y = _MARGIN

_SIZES = {"h1": 18.0, "h2": 15.0, "h3": 13.0, "p": 11.0, "table": 10.0}
_GAP = {"h1": 10.0, "h2": 8.0, "h3": 6.0, "p": 6.0, "table": 8.0}


def _pdf_escape(s: str) -> str:
    """Escape chuỗi cho PDF literal string: chỉ giữ được Latin-1, còn lại -> '?'."""
    out = []
    for ch in s:
        o = ord(ch)
        if o > 255:
            ch = "?"
        out.append(ch)
    s = "".join(out)
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap(text: str, max_chars: int) -> List[str]:
    """Ngắt dòng tham lam theo từ; từ quá dài thì cắt cứng."""
    words = str(text).split()
    lines: List[str] = []
    cur = ""
    for w in words:
        if len(cur) + (1 if cur else 0) + len(w) <= max_chars:
            cur = (cur + " " + w).strip()
        else:
            if cur:
                lines.append(cur)
            while len(w) > max_chars:
                lines.append(w[:max_chars])
                w = w[max_chars:]
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


class _MiniPdfPage:
    """1 trang PDF: danh sách các op vẽ ('text' hoặc 'line')."""

    def __init__(self):
        self.ops: list[tuple] = []  # ('text', x, y, size, str) | ('line', x1,y1,x2,y2,w)

    def text(self, x: float, y: float, size: float, s: str):
        self.ops.append(("text", x, y, size, s))

    def line(self, x1: float, y1: float, x2: float, y2: float, w: float = 0.5):
        self.ops.append(("line", x1, y1, x2, y2, w))

    def render_stream(self) -> bytes:
        parts = []
        for op in self.ops:
            if op[0] == "text":
                _, x, y, size, s = op
                parts.append(
                    f"BT /F1 {size:.1f} Tf {x:.1f} {y:.1f} Td ({_pdf_escape(s)}) Tj ET")
            else:
                _, x1, y1, x2, y2, w = op
                parts.append(
                    f"{w:.1f} w {x1:.1f} {y1:.1f} m {x2:.1f} {y2:.1f} l S")
        return ("\n".join(parts) + "\n").encode("latin-1")


class _MiniPdfDoc:
    """Layout engine tối giản: xếp text/bảng xuống các trang A4."""

    def __init__(self):
        self.pages: List[_MiniPdfPage] = []
        self._new_page()

    def _new_page(self):
        self.pages.append(_MiniPdfPage())
        self.y = _TOP_Y

    def _need(self, h: float):
        if self.y - h < _BOTTOM_Y:
            self._new_page()

    @staticmethod
    def _max_chars(size: float) -> int:
        usable = _PAGE_W - 2 * _MARGIN
        return max(10, int(usable / (size * 0.5)))  # Helvetica rộng TB ~0.5 * size

    def add_text(self, s: str, size: float, gap: float):
        self._need(size * 1.35 + gap)
        self.y -= gap * 0.5
        for line in _wrap(s, self._max_chars(size)):
            self._need(size * 1.35)
            self.pages[-1].text(_MARGIN, self.y, size, line)
            self.y -= size * 1.35
        self.y -= gap * 0.5

    def add_table(self, headers: List[str], rows: List[List]):
        size = _SIZES["table"]
        cols = len(headers)
        if cols == 0:
            return
        usable = _PAGE_W - 2 * _MARGIN
        # Độ rộng mỗi cột theo nội dung dài nhất, co lại nếu vượt trang
        widths = []
        for j in range(cols):
            mx = len(str(headers[j]))
            for r in rows:
                if j < len(r):
                    mx = max(mx, len(str(r[j])))
            widths.append(mx * size * 0.5 + 12)
        total = sum(widths)
        if total > usable:
            widths = [w * usable / total for w in widths]
        row_h = size * 1.35 + 6
        xs = [_MARGIN]
        for w in widths:
            xs.append(xs[-1] + w)

        def draw_row(cells, y_top, header=False):
            y_base = y_top - size - 3
            for j in range(cols):
                val = str(cells[j]) if j < len(cells) else ""
                # cắt text vừa khít cột
                maxc = max(4, int(widths[j] / (size * 0.5)) - 2)
                if len(val) > maxc:
                    val = val[:maxc - 1] + "…"
                self.pages[-1].text(xs[j] + 4, y_base, size, val)
            # kẻ ngang dưới mỗi hàng; header kẻ đậm hơn
            self.pages[-1].line(_MARGIN, y_top - row_h, xs[-1], y_top - row_h,
                                1.0 if header else 0.5)
            # kẻ dọc
            for x in xs:
                self.pages[-1].line(x, y_top, x, y_top - row_h, 0.5)

        self._need(row_h * 2 + _GAP["table"])
        self.y -= _GAP["table"] * 0.5
        # đường kẻ trên cùng của bảng
        self.pages[-1].line(_MARGIN, self.y, xs[-1], self.y, 1.0)
        draw_row(headers, self.y, header=True)
        self.y -= row_h
        for r in rows:
            self._need(row_h)
            draw_row(r, self.y)
            self.y -= row_h
        self.y -= _GAP["table"] * 0.5

    def serialize(self) -> bytes:
        """Dựng bytes PDF hoàn chỉnh: header + objects + xref + trailer."""
        n = len(self.pages)
        # Đánh số object: 1=catalog, 2=pages, 3=font, rồi mỗi trang 2 object
        # (page, contents). Tổng = 3 + 2*n.
        total_objs = 3 + 2 * n
        kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))

        contents = [p.render_stream() for p in self.pages]
        objs: dict[int, bytes] = {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode("latin-1"),
            3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        }
        for i, stream in enumerate(contents):
            objs[4 + 2 * i] = (
                f"<< /Type /Page /Parent 2 0 R "
                f"/MediaBox [0 0 {_PAGE_W:.0f} {_PAGE_H:.0f}] "
                f"/Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {5 + 2 * i} 0 R >>".encode("latin-1"))
            objs[5 + 2 * i] = b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream"

        out = bytearray()
        out += b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"  # dòng binary comment theo chuẩn
        offsets = [0] * (total_objs + 1)
        for num in range(1, total_objs + 1):
            offsets[num] = len(out)
            out += f"{num} 0 obj\n".encode("latin-1") + objs[num] + b"\nendobj\n"
        xref_pos = len(out)
        out += f"xref\n0 {total_objs + 1}\n".encode("latin-1")
        out += b"0000000000 65535 f \n"
        for num in range(1, total_objs + 1):
            out += f"{offsets[num]:010d} 00000 n \n".encode("latin-1")
        out += (f"trailer\n<< /Size {total_objs + 1} /Root 1 0 R >>\n"
                f"startxref\n{xref_pos}\n%%EOF\n").encode("latin-1")
        return bytes(out)


class PdfBuilder:
    """Fluent builder tạo file PDF.

    Dùng reportlab khi có sẵn; nếu không, fallback sang writer PDF tối giản
    viết tay (pure stdlib). Không bao giờ crash vì thiếu lib.
    """

    def __init__(self, force_fallback: bool = False):
        self._ops: list[tuple] = []
        self._native = False
        if not force_fallback:
            try:
                import reportlab  # noqa: F401
                self._native = True
            except ImportError:
                self._native = False

    @property
    def available(self) -> bool:
        """True nếu đang dùng reportlab (native), False nếu fallback viết tay."""
        return self._native

    # -- fluent API ---------------------------------------------------------
    def add_heading(self, text: str, level: int = 1) -> "PdfBuilder":
        self._ops.append(("heading", text, max(1, min(3, level))))
        return self

    def add_paragraph(self, text: str) -> "PdfBuilder":
        self._ops.append(("paragraph", text))
        return self

    def add_table(self, headers: List[str], rows: List[List]) -> "PdfBuilder":
        self._ops.append(("table", list(headers), [list(r) for r in rows]))
        return self

    def add_page_break(self) -> "PdfBuilder":
        self._ops.append(("pagebreak",))
        return self

    # -- output -------------------------------------------------------------
    def to_bytes(self) -> bytes:
        if self._native:
            return self._build_native()
        return self._build_fallback()

    def save(self, path: str) -> str:
        data = self.to_bytes()
        with open(path, "wb") as f:
            f.write(data)
        return path

    # -- native build (reportlab) -------------------------------------------
    def _build_native(self) -> bytes:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import (
            PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
        )

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4)
        styles = getSampleStyleSheet()
        story = []
        for op in self._ops:
            kind = op[0]
            if kind == "heading":
                _, text, level = op
                story.append(Paragraph(_html_escape(text).replace("\n", "<br/>"),
                                       styles[f"Heading{level}"]))
                story.append(Spacer(1, 6))
            elif kind == "paragraph":
                _, text = op
                story.append(Paragraph(_html_escape(text).replace("\n", "<br/>"),
                                       styles["Normal"]))
                story.append(Spacer(1, 6))
            elif kind == "table":
                _, headers, rows = op
                data = [[_html_escape(str(c)) for c in headers]]
                data += [[_html_escape(str(c)) for c in r] for r in rows]
                t = Table(data, repeatRows=1)
                t.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4472C4")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 10),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                     [colors.white, colors.HexColor("#F2F2F2")]),
                ]))
                story.append(t)
                story.append(Spacer(1, 8))
            elif kind == "pagebreak":
                story.append(PageBreak())
        doc.build(story)
        return buf.getvalue()

    # -- fallback build (viết tay) ------------------------------------------
    def _build_fallback(self) -> bytes:
        doc = _MiniPdfDoc()
        for op in self._ops:
            kind = op[0]
            if kind == "heading":
                _, text, level = op
                key = f"h{level}"
                doc.add_text(text, _SIZES[key], _GAP[key])
            elif kind == "paragraph":
                _, text = op
                doc.add_text(text, _SIZES["p"], _GAP["p"])
            elif kind == "table":
                _, headers, rows = op
                doc.add_table(headers, rows)
            elif kind == "pagebreak":
                doc._new_page()
        return doc.serialize()
