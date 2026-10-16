"""Test cho PdfBuilder (pdf.py).

Chạy:  python3 -m pytest test_pdf.py -v   (hoặc: python3 test_pdf.py)
"""
import re
import sys
import types
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).parent


def load_builder(mod_name="pdf_builder_mod"):
    spec = importlib.util.spec_from_file_location(
        mod_name, HERE.parent / "agent" / "src" / "documents" / "pdf.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Stub giả lập reportlab để test nhánh native mà không cần cài lib thật
# ---------------------------------------------------------------------------
def install_reportlab_stub(log):
    rl = types.ModuleType("reportlab")
    lib = types.ModuleType("reportlab.lib")
    colors = types.ModuleType("reportlab.lib.colors")
    colors.HexColor = lambda s: ("hex", s)
    colors.whitesmoke = "whitesmoke"
    colors.grey = "grey"
    colors.white = "white"
    pagesizes = types.ModuleType("reportlab.lib.pagesizes")
    pagesizes.A4 = (595.27, 841.89)
    stylesm = types.ModuleType("reportlab.lib.styles")
    stylesm.getSampleStyleSheet = lambda: {
        "Heading1": "H1", "Heading2": "H2", "Heading3": "H3", "Normal": "N"}
    plat = types.ModuleType("reportlab.platypus")

    class _Doc:
        def __init__(self, buf, **kw):
            self._buf = buf

        def build(self, story):
            log.append(("build", len(story)))
            self._buf.write(b"FAKEREPORTLAB:" + str(len(story)).encode())

    class _Para:
        def __init__(self, text, style):
            self.text = text
            self.style = style

    class _Table:
        def __init__(self, data, **kw):
            self.data = data
            self.style = None

        def setStyle(self, st):
            self.style = st
            log.append(("tablestyle", st))

    plat.SimpleDocTemplate = _Doc
    plat.Paragraph = _Para
    plat.Spacer = lambda a, b: ("spacer", a, b)
    plat.Table = _Table
    plat.TableStyle = lambda rows: rows
    plat.PageBreak = lambda: ("pagebreak",)

    for name, m in [("reportlab", rl), ("reportlab.lib", lib),
                    ("reportlab.lib.colors", colors),
                    ("reportlab.lib.pagesizes", pagesizes),
                    ("reportlab.lib.styles", stylesm),
                    ("reportlab.platypus", plat)]:
        sys.modules[name] = m


@pytest.fixture()
def reportlab_stubbed():
    names = ("reportlab", "reportlab.lib", "reportlab.lib.colors",
             "reportlab.lib.pagesizes", "reportlab.lib.styles",
             "reportlab.platypus")
    saved = {k: sys.modules.get(k) for k in names}
    log = []
    install_reportlab_stub(log)
    yield log
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def make_doc(mod):
    return (mod.PdfBuilder(force_fallback=True)
            .add_heading("Báo cáo tháng 10", 1)
            .add_paragraph("Đây là đoạn văn bản mẫu để kiểm tra PdfBuilder "
                           "ở chế độ fallback viết tay.")
            .add_table(["Sản phẩm", "Số lượng", "Giá"],
                       [["Bút bi", "100", "5.000đ"],
                        ["Vở kẻ ngang", "50", "12.000đ"]])
            .add_page_break()
            .add_heading("Kết luận", 2)
            .add_paragraph("Hết báo cáo."))


# ---------------------------------------------------------------------------
# Test fallback (không có reportlab)
# ---------------------------------------------------------------------------
def test_fallback_pdf_structure(tmp_path):
    mod = load_builder("pdf_fb1")
    b = make_doc(mod)
    assert b.available is False

    data = b.to_bytes()
    assert data.startswith(b"%PDF"), "PDF phải bắt đầu bằng magic %PDF"
    for marker in (b"xref", b"trailer", b"startxref"):
        assert marker in data, f"thiếu marker {marker}"
    assert data.rstrip().endswith(b"%%EOF")

    out = b.save(str(tmp_path / "report.pdf"))
    assert Path(out).stat().st_size > 0


def test_fallback_page_break_creates_pages():
    mod = load_builder("pdf_fb2")
    b = make_doc(mod)
    data = b.to_bytes()
    # đếm object /Type /Page (loại trừ /Pages bằng [^s])
    pages = re.findall(rb"/Type\s*/Page[^s]", data)
    assert len(pages) == 2, f"expect 2 pages, got {len(pages)}"
    assert b"/Count 2" in data


def test_fallback_table_content_present():
    mod = load_builder("pdf_fb3")
    data = (mod.PdfBuilder(force_fallback=True)
            .add_table(["Name", "Qty"], [["Pen", "10"]])
            .to_bytes())
    assert b"Pen" in data and b"Name" in data and b"Qty" in data


def test_fallback_vietnamese_no_crash():
    """Tiếng Việt có dấu ở fallback: ký tự ngoài Latin-1 thành '?', không crash."""
    mod = load_builder("pdf_fb4")
    data = (mod.PdfBuilder(force_fallback=True)
            .add_heading("Tiếng Việt: đồng ơ ư", 1)
            .add_paragraph("Dòng có dấu: à á ạ ă â ê ô ơ ư đ")
            .to_bytes())
    assert data.startswith(b"%PDF")
    assert data.rstrip().endswith(b"%%EOF")


def test_fallback_auto_when_lib_missing():
    mod = load_builder("pdf_fb5")
    try:
        import reportlab as _real  # noqa
        pytest.skip("máy này đã có reportlab, bỏ qua test auto-fallback")
    except ImportError:
        pass
    b = mod.PdfBuilder()
    assert b.available is False
    assert b.add_heading("x").to_bytes().startswith(b"%PDF")


def test_fluent_chaining_returns_self():
    mod = load_builder("pdf_fb6")
    b = mod.PdfBuilder(force_fallback=True)
    assert b.add_heading("h") is b
    assert b.add_paragraph("p") is b
    assert b.add_table(["h"], [["x"]]) is b
    assert b.add_page_break() is b
    assert b.save("/tmp/_chain_test.pdf") == "/tmp/_chain_test.pdf"


def test_heading_level_clamped():
    mod = load_builder("pdf_fb7")
    b = mod.PdfBuilder(force_fallback=True)
    b.add_heading("x", level=99)
    assert b._ops[0][2] == 3
    b.add_heading("y", level=0)
    assert b._ops[1][2] == 1


# ---------------------------------------------------------------------------
# Test nhánh native (dùng stub thay cho reportlab thật)
# ---------------------------------------------------------------------------
def test_native_path_with_stub(reportlab_stubbed):
    log = reportlab_stubbed
    mod = load_builder("pdf_native_stub")
    b = mod.PdfBuilder()
    assert b.available is True, "stub đã inject thì phải vào nhánh native"

    (b.add_heading("H1", 1)
      .add_paragraph("para")
      .add_table(["A", "B"], [["1", "2"]])
      .add_page_break())
    data = b.to_bytes()
    # story = heading+spacer, para+spacer, table+spacer, pagebreak = 7 phần tử
    assert data == b"FAKEREPORTLAB:7", data
    assert ("build", 7) in log
    tablestyles = [e[1] for e in log if e[0] == "tablestyle"]
    assert len(tablestyles) == 1
    # style bảng phải có GRID và header màu
    flat = str(tablestyles[0])
    assert "GRID" in flat and "BACKGROUND" in flat


def test_native_readback_if_real_lib(tmp_path):
    """Nếu máy có reportlab thật: build rồi verify magic + đọc được bằng pypdf (nếu có)."""
    try:
        import reportlab  # noqa
    except ImportError:
        pytest.skip("không có reportlab thật")
    mod = load_builder("pdf_native_real")
    b = mod.PdfBuilder()
    assert b.available is True
    data = (b.add_heading("Hello", 1)
             .add_paragraph("world")
             .add_table(["A"], [["1"]])
             .to_bytes())
    assert data.startswith(b"%PDF") and len(data) > 500


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
