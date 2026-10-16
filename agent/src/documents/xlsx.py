#!/usr/bin/env python3
"""XlsxBuilder — fluent API for creating XLSX files straight from Python.

openpyxl is OPTIONAL:
  - With openpyxl  -> a real workbook: sheets, formulas, styles, charts.
  - Without         -> no crash. Data is still kept in memory
                    (CSV fallback mode, saved as CSV with a warning);
                    clearly reports "needs pip install openpyxl" for the
                    advanced features (formulas, styles, charts).

Example:
    b = XlsxBuilder()
    (b.add_sheet("Sales")
      .set_cell("Sales", 1, 1, "Month")
      .set_cell("Sales", 1, 2, "Revenue")
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

# openpyxl OPTIONAL — nested import in try so this module stays importable
# even on machines without the lib.
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
    """Logs each warning once per msg (avoids log spam)."""
    if msg not in _WARN_ONCE:
        _WARN_ONCE.add(msg)
        warnings.warn(msg, UserWarning, stacklevel=3)


class XlsxBuilder:
    """Fluent builder for XLSX files. Doesn't crash without openpyxl."""

    def __init__(self):
        self._has_openpyxl = _HAS_OPENPYXL
        if self._has_openpyxl:
            # Drop the default sheet so the user's first sheet comes first.
            self._wb = Workbook()
            self._wb.remove(self._wb.active)
            self._charts: Dict[str, list] = {}
        else:
            # Fallback: stores data as {sheet: {(row, col): value}}
            # so CSV export / reports still work without crashing.
            self._cells: Dict[str, Dict[tuple, Any]] = {}
            self._sheet_order: List[str] = []
            _warn(
                "openpyxl is not installed — XlsxBuilder is running in CSV "
                "fallback mode (no formula/style/chart support). "
                "Install with: pip install openpyxl"
            )

    # ---------- internal helpers ----------
    def _ensure_sheet(self, name: str):
        """Gets a sheet, creating it when missing (defaults to the workbook sheet)."""
        if self._has_openpyxl:
            if name not in self._wb.sheetnames:
                self._wb.create_sheet(name)
                self._charts.setdefault(name, [])
            return self._wb[name]
        # fallback: just remember the sheet order
        if name not in self._sheet_order:
            self._sheet_order.append(name)
            self._cells[name] = {}
        return None

    # ---------- main API (fluent, returns self) ----------
    def add_sheet(self, name: str) -> "XlsxBuilder":
        self._ensure_sheet(name)
        return self

    def set_cell(self, sheet: str, row: int, col: int, value: Any) -> "XlsxBuilder":
        """Sets a cell value (row and col are both 1-based)."""
        if self._has_openpyxl:
            self._ensure_sheet(sheet).cell(row=row, column=col, value=value)
        else:
            self._ensure_sheet(sheet)
            self._cells[sheet][(row, col)] = value
        return self

    def set_formula(self, sheet: str, row: int, col: int, formula: str) -> "XlsxBuilder":
        """Sets a formula, e.g. '=SUM(A1:A10)'. Native with openpyxl."""
        if not formula.startswith("="):
            formula = "=" + formula
        if self._has_openpyxl:
            # openpyxl stores the formula string verbatim; Excel evaluates on open.
            self._ensure_sheet(sheet).cell(row=row, column=col, value=formula)
        else:
            self._ensure_sheet(sheet)
            self._cells[sheet][(row, col)] = formula
            _warn("set_formula needs openpyxl for Excel to understand the formula — "
                  "currently stored as plain text.")
        return self

    def style_cell(self, sheet: str, row: int, col: int,
                   bold: bool = False, color: Optional[str] = None,
                   border: bool = False) -> "XlsxBuilder":
        """Styles a cell: bold / font color (hex RGB, e.g. 'FF0000') / thin border."""
        if self._has_openpyxl:
            cell = self._ensure_sheet(sheet).cell(row=row, column=col)
            cell.font = Font(bold=bold, color=color) if (bold or color) else cell.font
            if border:
                side = Side(style="thin")
                cell.border = Border(left=side, right=side, top=side, bottom=side)
        else:
            _warn("style_cell needs openpyxl — style skipped in fallback.")
        return self

    def auto_width(self, sheet: str) -> "XlsxBuilder":
        """Auto-sizes column widths to each column's longest content."""
        if self._has_openpyxl:
            ws = self._ensure_sheet(sheet)
            for col in ws.columns:
                max_len = 0
                letter = get_column_letter(col[0].column)
                for cell in col:
                    if cell.value is not None:
                        max_len = max(max_len, len(str(cell.value)))
                # +2 padding so text doesn't touch the edge; capped at 50.
                ws.column_dimensions[letter].width = min(max_len + 2, 50)
        else:
            _warn("auto_width needs openpyxl — skipped in fallback.")
        return self

    def add_chart(self, sheet: str, chart_type: str = "bar",
                  data_range: str = "", title: str = "") -> "XlsxBuilder":
        """Adds a chart. Without openpyxl: skipped + warning."""
        if not self._has_openpyxl:
            _warn("add_chart needs openpyxl — chart skipped.")
            return self
        ws = self._ensure_sheet(sheet)
        chart_cls = {"bar": BarChart, "line": LineChart, "pie": PieChart}.get(
            chart_type.lower())
        if chart_cls is None:
            raise ValueError(f"invalid chart_type: {chart_type} "
                             "(bar | line | pie)")
        chart = chart_cls()
        if title:
            chart.title = title
        if data_range:
            # data_range like "B2:B10": builds a Reference from the range text.
            from openpyxl.utils import range_boundaries
            min_col, min_row, max_col, max_row = range_boundaries(data_range)
            ref = Reference(ws, min_col=min_col, min_row=min_row,
                            max_col=max_col, max_row=max_row)
            chart.add_data(ref, titles_from_data=True)
        # Anchors the chart at H2 so it doesn't cover the data.
        ws.add_chart(chart, "H2")
        return self

    # ---------- export ----------
    def save(self, path: str) -> str:
        """Saves the workbook. Returns the saved path."""
        if self._has_openpyxl:
            self._wb.save(path)
        else:
            # CSV fallback: one file per sheet, "<path>_sheetN.csv".
            # Single sheet: save straight to path with a .csv extension.
            sheets = self._sheet_order or ["Sheet1"]
            if len(sheets) == 1 and path.lower().endswith((".xlsx", ".xls")):
                self._save_csv(sheets[0], path.rsplit(".", 1)[0] + ".csv")
                return path.rsplit(".", 1)[0] + ".csv"
            for i, name in enumerate(sheets):
                self._save_csv(name, f"{path}_sheet{i + 1}.csv")
            _warn("Saved in CSV fallback mode (openpyxl missing).")
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
        """Returns the XLSX as bytes (for sending via Telegram/API)."""
        if self._has_openpyxl:
            buf = io.BytesIO()
            self._wb.save(buf)
            return buf.getvalue()
        # Fallback: CSV of the first sheet.
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
    """Quick CLI demo: creates a sample file."""
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
