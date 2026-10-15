#!/usr/bin/env python3
"""XlsxBuilder — fluent API tạo file XLSX dùng trực tiếp từ Python.

openpyxl là OPTIONAL:
  - Có openpyxl  -> workbook thật: sheet, công thức, style, chart.
  - Không có      -> không crash. Dữ liệu vẫn được giữ trong bộ nhớ
                    (chế độ CSV fallback, lưu thành CSV kèm warning);
                    báo rõ "cần pip install openpyxl" cho các tính năng
                    nâng cao (công thức, style, chart).

Ví dụ:
    b = XlsxBuilder()
    (b.add_sheet("Sales")
      .set_cell("Sales", 1, 1, "Tháng")
      .set_cell("Sales", 1, 2, "Doanh thu")
      .set_cell("Sales", 2, 2, 1200)
      .set_formula("Sales", 4, 2, "=SUM(B2:B3)")
      .style_cell("Sales", 1, 1, bold=True, color="FF0000", border=True)
      .auto_width("Sales")
      .save("report.xlsx"))
"""
from __future__ import annotations

import csv
import io
import sys
import warnings
from typing import Any, Dict, List, Optional

# openpyxl OPTIONAL — import lồng trong try để module này import được
# kể cả trên máy chưa cài lib.
try:
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference
    from openpyxl.styles import Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    _HAS_OPENPYXL = True
except ImportError:
    _HAS_OPENPYXL = False

_WARN_ONCE = set()


def _warn(msg: str) -> None:
    """Ghi warning 1 lần duy nhất cho mỗi msg (tránh spam log)."""
    if msg not in _WARN_ONCE:
        _WARN_ONCE.add(msg)
        warnings.warn(msg, UserWarning, stacklevel=3)


class XlsxBuilder:
    """Fluent builder tạo file XLSX. Không crash khi thiếu openpyxl."""

    def __init__(self):
        self._has_openpyxl = _HAS_OPENPYXL
        if self._has_openpyxl:
            # Bỏ sheet mặc định để sheet đầu tiên do user tạo.
            self._wb = Workbook()
            self._wb.remove(self._wb.active)
            self._charts: Dict[str, list] = {}
        else:
            # Fallback: lưu data dạng dict {sheet: {(row, col): value}}
            # để vẫn lưu được CSV / báo cáo mà không crash.
            self._cells: Dict[str, Dict[tuple, Any]] = {}
            self._sheet_order: List[str] = []
            _warn(
                "openpyxl chưa được cài — XlsxBuilder chạy ở chế độ fallback "
                "CSV (không hỗ trợ công thức/style/chart). "
                "Cài bằng: pip install openpyxl"
            )

    # ---------- tiện ích nội bộ ----------
    def _ensure_sheet(self, name: str):
        """Lấy sheet, tự tạo nếu chưa có (mặc định: workbook sheet)."""
        if self._has_openpyxl:
            if name not in self._wb.sheetnames:
                self._wb.create_sheet(name)
                self._charts.setdefault(name, [])
            return self._wb[name]
        # fallback: chỉ ghi nhớ thứ tự sheet
        if name not in self._sheet_order:
            self._sheet_order.append(name)
            self._cells[name] = {}
        return None

    # ---------- API chính (fluent, trả về self) ----------
    def add_sheet(self, name: str) -> "XlsxBuilder":
        self._ensure_sheet(name)
        return self

    def set_cell(self, sheet: str, row: int, col: int, value: Any) -> "XlsxBuilder":
        """Gán giá trị cho ô (row, col đều 1-based)."""
        if self._has_openpyxl:
            self._ensure_sheet(sheet).cell(row=row, column=col, value=value)
        else:
            self._ensure_sheet(sheet)
            self._cells[sheet][(row, col)] = value
        return self

    def set_formula(self, sheet: str, row: int, col: int, formula: str) -> "XlsxBuilder":
        """Gán công thức, vd: '=SUM(A1:A10)'. openpyxl hỗ trợ native."""
        if not formula.startswith("="):
            formula = "=" + formula
        if self._has_openpyxl:
            # openpyxl lưu chuỗi công thức nguyên vẹn; Excel tính khi mở.
            self._ensure_sheet(sheet).cell(row=row, column=col, value=formula)
        else:
            self._ensure_sheet(sheet)
            self._cells[sheet][(row, col)] = formula
            _warn("set_formula cần openpyxl để Excel hiểu công thức — "
                  "hiện chỉ lưu dạng text.")
        return self

    def style_cell(self, sheet: str, row: int, col: int,
                   bold: bool = False, color: Optional[str] = None,
                   border: bool = False) -> "XlsxBuilder":
        """Style ô: bold / màu chữ (hex RGB, vd 'FF0000') / viền mỏng."""
        if self._has_openpyxl:
            cell = self._ensure_sheet(sheet).cell(row=row, column=col)
            cell.font = Font(bold=bold, color=color) if (bold or color) else cell.font
            if border:
                side = Side(style="thin")
                cell.border = Border(left=side, right=side, top=side, bottom=side)
        else:
            _warn("style_cell cần openpyxl — fallback bỏ qua style.")
        return self

    def auto_width(self, sheet: str) -> "XlsxBuilder":
        """Tự chỉnh độ rộng cột theo độ dài nội dung dài nhất của cột đó."""
        if self._has_openpyxl:
            ws = self._ensure_sheet(sheet)
            for col in ws.columns:
                max_len = 0
                letter = get_column_letter(col[0].column)
                for cell in col:
                    if cell.value is not None:
                        max_len = max(max_len, len(str(cell.value)))
                # +2 padding để chữ không bị dính mép; giới hạn 50.
                ws.column_dimensions[letter].width = min(max_len + 2, 50)
        else:
            _warn("auto_width cần openpyxl — fallback bỏ qua.")
        return self

    def add_chart(self, sheet: str, chart_type: str = "bar",
                  data_range: str = "", title: str = "") -> "XlsxBuilder":
        """Thêm chart. Nếu thiếu openpyxl: bỏ qua + warning."""
        if not self._has_openpyxl:
            _warn("add_chart cần openpyxl — chart đã bị bỏ qua.")
            return self
        ws = self._ensure_sheet(sheet)
        chart_cls = {"bar": BarChart, "line": LineChart, "pie": PieChart}.get(
            chart_type.lower())
        if chart_cls is None:
            raise ValueError(f"chart_type không hợp lệ: {chart_type} "
                             "(bar | line | pie)")
        chart = chart_cls()
        if title:
            chart.title = title
        if data_range:
            # data_range dạng "B2:B10": tạo Reference từ range text.
            from openpyxl.utils import range_boundaries
            min_col, min_row, max_col, max_row = range_boundaries(data_range)
            ref = Reference(ws, min_col=min_col, min_row=min_row,
                            max_col=max_col, max_row=max_row)
            chart.add_data(ref, titles_from_data=True)
        # Neo chart ở góc H2 để không đè lên data.
        ws.add_chart(chart, "H2")
        return self

    # ---------- xuất ----------
    def save(self, path: str) -> str:
        """Lưu workbook. Trả về path đã lưu."""
        if self._has_openpyxl:
            self._wb.save(path)
        else:
            # Fallback CSV: mỗi sheet 1 file "<path>_sheetN.csv".
            # Nếu chỉ có 1 sheet, lưu thẳng vào path đổi đuôi .csv.
            sheets = self._sheet_order or ["Sheet1"]
            if len(sheets) == 1 and path.lower().endswith((".xlsx", ".xls")):
                self._save_csv(sheets[0], path.rsplit(".", 1)[0] + ".csv")
                return path.rsplit(".", 1)[0] + ".csv"
            for i, name in enumerate(sheets):
                self._save_csv(name, f"{path}_sheet{i + 1}.csv")
            _warn("Lưu ở chế độ CSV fallback (thiếu openpyxl).")
        return path

    def _save_csv(self, sheet: str, csv_path: str) -> None:
        cells = self._cells.get(sheet, {})
        if not cells:
            open(csv_path, "w").close()
            return
        max_row = max(r for r, _ in cells)
        max_col = max(c for _, c in cells)
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            for r in range(1, max_row + 1):
                w.writerow([cells.get((r, c), "") for c in range(1, max_col + 1)])

    def to_bytes(self) -> bytes:
        """Trả về binary XLSX (để gửi qua Telegram/API)."""
        if self._has_openpyxl:
            buf = io.BytesIO()
            self._wb.save(buf)
            return buf.getvalue()
        # Fallback: CSV của sheet đầu tiên.
        cells = self._cells.get(self._sheet_order[0], {}) if self._sheet_order else {}
        buf = io.StringIO()
        if cells:
            max_row = max(r for r, _ in cells)
            max_col = max(c for _, c in cells)
            w = csv.writer(buf)
            for r in range(1, max_row + 1):
                w.writerow([cells.get((r, c), "") for c in range(1, max_col + 1)])
        return buf.getvalue().encode("utf-8")


def main() -> int:
    """CLI demo nhanh: tạo 1 file mẫu."""
    out = sys.argv[1] if len(sys.argv) > 1 else "demo.xlsx"
    b = XlsxBuilder()
    (b.add_sheet("Data")
      .set_cell("Data", 1, 1, "Item").set_cell("Data", 1, 2, "Qty")
      .set_cell("Data", 2, 1, "A").set_cell("Data", 2, 2, 10)
      .set_cell("Data", 3, 1, "B").set_cell("Data", 3, 2, 20)
      .set_formula("Data", 4, 2, "=SUM(B2:B3)")
      .style_cell("Data", 1, 1, bold=True, color="FF0000", border=True)
      .auto_width("Data")
      .add_chart("Data", "bar", "B2:B3", "Qty"))
    print(b.save(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
