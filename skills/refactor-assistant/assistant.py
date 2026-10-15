"""Wrapper thân thiện cho RefactorEngine (skill refactor-assistant).

RefactorEngine dùng AST thật (đổi tên theo vị trí token, không regex mù),
đã được vendor từ agent/src/autonomous/refactor.py vào file
refactor_engine.py cùng thư mục để skill độc lập với repo.

RefactorAssistant thêm:
  - preview mặc định True: mọi thao tác đều xem diff trước khi apply
  - API gọn: rename / extract / move
  - confirm flow: preview -> (user/agent OK) -> apply

Quy ước an toàn (kế thừa từ engine):
  - dry_run: chỉ trả diff, không ghi file
  - apply: backup .bak-<timestamp> trước, validate syntax sau khi ghi,
    lỗi -> rollback toàn bộ
"""

from __future__ import annotations

import sys
from pathlib import Path

# Cho phép import refactor_engine.py nằm cùng thư mục khi chạy trực tiếp
sys.path.insert(0, str(Path(__file__).resolve().parent))

from refactor_engine import RefactorEngine  # noqa: E402


class RefactorAssistant:
    """Trợ lý refactor: preview trước, apply sau khi xác nhận."""

    def __init__(self, root_dir="."):
        # Engine dùng AST thật, root_dir để resolve đường dẫn tương đối
        self.root = Path(root_dir).resolve()
        self.engine = RefactorEngine(root_dir=str(self.root))
        # Lưu preview gần nhất để apply đúng thứ đã xem
        self._last_preview = None

    # -- rename ------------------------------------------------------

    def rename(self, old_name, new_name, files, preview=True):
        """Đổi tên biến/hàm/class xuyên nhiều file.

        - files: list đường dẫn (tương đối so với root_dir hoặc tuyệt đối)
        - preview=True: chỉ trả diff, không ghi file
        - preview=False: ghi thật (backup + validate + rollback khi lỗi)
        """
        ket_qua = self.engine.rename_symbol(
            old_name, new_name, files, dry_run=preview)
        if preview and ket_qua.get("ok"):
            self._last_preview = ("rename", old_name, new_name,
                                  list(files), ket_qua)
        return ket_qua

    # -- extract -----------------------------------------------------

    def extract(self, path, start_line, end_line, new_name, preview=True):
        """Tách dòng [start_line, end_line] trong file thành hàm mới.

        Engine tự suy: params (biến dùng trong đoạn nhưng định nghĩa ngoài),
        returns (biến gán trong đoạn còn dùng sau đoạn).
        """
        ket_qua = self.engine.extract_function(
            path, start_line, end_line, new_name, dry_run=preview)
        if preview and ket_qua.get("ok"):
            self._last_preview = ("extract", path, start_line, end_line,
                                  new_name, ket_qua)
        return ket_qua

    # -- move --------------------------------------------------------

    def move(self, src, dst, symbol_name, preview=True):
        """Chuyển 1 hàm/class top-level từ file src sang file dst."""
        ket_qua = self.engine.move_symbol(src, dst, symbol_name,
                                          dry_run=preview)
        if preview and ket_qua.get("ok"):
            self._last_preview = ("move", src, dst, symbol_name, ket_qua)
        return ket_qua

    # -- confirm flow ------------------------------------------------

    def apply_last_preview(self):
        """Apply đúng thao tác đã preview gần nhất (sau khi user/agent OK).

        Trả về kết quả apply thật. Không có preview nào -> báo lỗi.
        """
        if self._last_preview is None:
            return {"ok": False,
                    "error": "chưa có preview nào — hãy preview trước khi apply"}
        kind = self._last_preview[0]
        if kind == "rename":
            _, old, new, files, _ = self._last_preview
            ket_qua = self.rename(old, new, files, preview=False)
        elif kind == "extract":
            _, path, s, e, name, _ = self._last_preview
            ket_qua = self.extract(path, s, e, name, preview=False)
        else:  # move
            _, src, dst, sym, _ = self._last_preview
            ket_qua = self.move(src, dst, sym, preview=False)
        self._last_preview = None  # apply 1 lần duy nhất, tránh apply nhầm 2 lần
        return ket_qua

    def rollback(self):
        """Khôi phục file từ backup của thao tác gần nhất."""
        return self.engine.rollback()


if __name__ == "__main__":
    # Demo nhanh: python3 assistant.py <root_dir>
    import sys
    import json
    ra = RefactorAssistant(sys.argv[1] if len(sys.argv) > 1 else ".")
    print(json.dumps({"root": str(ra.root),
                      "preview_pending": ra._last_preview is not None},
                     ensure_ascii=False))
