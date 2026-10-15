"""filesystem.ops — thao tác file/thư mục hợp nhất cho Boom.

Gộp từ hai module cũ (loại bỏ trùng lặp):
- agent/src/tools/file_ops.py  (read_file, write_file, patch_file fuzzy, search_files)
- agent/src/tools/fileops.py    (edit_file strict, append_file, file_info)

Quyết định khi gộp:
- Giữ MỘT cơ chế an toàn duy nhất: mọi path đều resolve và phải nằm trong
  thư mục làm việc hiện tại (chặn path traversal).
- Bỏ patch_file fuzzy (dễ sửa nhầm vị trí "gần đúng", không backup) — chỉ giữ
  edit() strict: đúng 1 match mới sửa, luôn backup sang <path>.bak.
- Bỏ search_files (tìm kiếm nội dung là việc của research tool, không phải fs core).
- Chuẩn hóa: mọi hàm trả về dict {"ok": True, ...} hoặc {"ok": False, "error": ...},
  KHÔNG raise exception ra ngoài.

Chỉ dùng stdlib.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_CHUNK = 1024  # số byte đầu file dùng để phát hiện file nhị phân
_MAX_READ_LINES = 10000  # giới hạn limit của read()


# ---------------------------------------------------------------------------
# helper nội bộ
# ---------------------------------------------------------------------------

def _resolve_safe(path: str) -> tuple[Optional[Path], Optional[str]]:
    """Resolve *path* và kiểm tra nó nằm trong thư mục làm việc hiện tại.

    Trả về (resolved_path, None) nếu hợp lệ, ngược lại (None, error_message).
    """
    if not isinstance(path, str) or not path.strip():
        return None, "path phải là chuỗi non-empty"
    try:
        root = Path.cwd().resolve()
        target = (root / path).resolve()
    except (OSError, ValueError) as exc:  # path không resolve được
        return None, f"path không hợp lệ: {exc}"
    try:
        target.relative_to(root)
    except ValueError:
        return None, "path traversal blocked: path nằm ngoài thư mục làm việc"
    return target, None


def _is_binary(path: Path) -> bool:
    """True nếu 1KB đầu file chứa null byte (dấu hiệu file nhị phân)."""
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(_CHUNK)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def read(path: str, offset: int = 1, limit: int = 2000) -> Dict[str, Any]:
    """Đọc file text theo trang, kèm số dòng.

    Mô tả:
        Đọc *limit* dòng bắt đầu từ dòng *offset* (1-based). Mỗi dòng trả về
        có tiền tố "<số dòng>|" để agent dễ tham chiếu khi edit.
    Args:
        path: đường dẫn file (tương đối so với cwd).
        offset: dòng bắt đầu (1-based), phải >= 1.
        limit: số dòng tối đa, 1..10000.
    Returns:
        {"ok": True, "content": str, "total_lines": int, "offset": int, "limit": int}
        hoặc {"ok": False, "error": str}.
    Raises:
        Không raise — mọi lỗi trả về dict.
    """
    target, err = _resolve_safe(path)
    if err:
        return {"ok": False, "error": err}
    assert target is not None
    if not isinstance(offset, int) or offset < 1:
        return {"ok": False, "error": "offset phải là số nguyên >= 1"}
    if not isinstance(limit, int) or not 1 <= limit <= _MAX_READ_LINES:
        return {"ok": False, "error": f"limit phải trong khoảng 1..{_MAX_READ_LINES}"}
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": f"file không tồn tại: {path}"}
    if _is_binary(target):
        return {"ok": False, "error": "binary file refused: không đọc file nhị phân"}
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return {"ok": False, "error": f"không đọc được file: {exc}"}
    total = len(lines)
    start = offset - 1
    selected = lines[start:start + limit]
    formatted = "\n".join(f"{start + i + 1}|{line}" for i, line in enumerate(selected))
    return {
        "ok": True,
        "content": formatted,
        "total_lines": total,
        "offset": offset,
        "limit": limit,
    }


def write(path: str, content: str) -> Dict[str, Any]:
    """Ghi đè (hoặc tạo mới) file text.

    Mô tả:
        Ghi toàn bộ *content* vào file. Tự tạo thư mục cha nếu chưa có.
    Args:
        path: đường dẫn file (tương đối so với cwd).
        content: nội dung text cần ghi.
    Returns:
        {"ok": True, "path": str, "bytes_written": int}
        hoặc {"ok": False, "error": str}.
    Raises:
        Không raise — mọi lỗi trả về dict.
    """
    target, err = _resolve_safe(path)
    if err:
        return {"ok": False, "error": err}
    assert target is not None
    if not isinstance(content, str):
        return {"ok": False, "error": "content phải là chuỗi"}
    if target.exists() and not target.is_file():
        return {"ok": False, "error": "path là thư mục, không ghi đè được"}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"không ghi được file: {exc}"}
    return {"ok": True, "path": str(target),
            "bytes_written": len(content.encode("utf-8"))}


def edit(path: str, old: str, new: str) -> Dict[str, Any]:
    """Sửa file: thay đúng 1 vị trí khớp tuyệt đối (exact match).

    Mô tả:
        Tìm chuỗi *old* trong file. Chỉ sửa khi có đúng 1 match:
        - 0 match  -> lỗi "not found"
        - >1 match -> lỗi "ambiguous", yêu cầu chuỗi cụ thể hơn
        Trước khi ghi, backup bản gốc sang <path>.bak.
    Args:
        path: đường dẫn file.
        old: chuỗi cần thay (phải non-empty, khớp tuyệt đối).
        new: chuỗi thay thế.
    Returns:
        {"ok": True, "path": str, "backup": str}
        hoặc {"ok": False, "error": str}.
    Raises:
        Không raise — mọi lỗi trả về dict.
    """
    target, err = _resolve_safe(path)
    if err:
        return {"ok": False, "error": err}
    assert target is not None
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": "not found"}
    if _is_binary(target):
        return {"ok": False, "error": "binary file refused"}
    if not old:
        return {"ok": False, "error": "old phải non-empty"}
    if not isinstance(new, str):
        return {"ok": False, "error": "new phải là chuỗi"}
    try:
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"ok": False, "error": f"không đọc được file: {exc}"}
    count = content.count(old)
    if count == 0:
        return {"ok": False, "error": "not found"}
    if count > 1:
        return {"ok": False, "error": f"ambiguous: {count} matches, hãy cụ thể hơn"}
    backup = target.with_name(target.name + ".bak")
    try:
        shutil.copy2(target, backup)
        target.write_text(content.replace(old, new, 1), encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"không ghi được file: {exc}"}
    return {"ok": True, "path": str(target), "backup": str(backup)}


def append(path: str, text: str, newline: bool = True) -> Dict[str, Any]:
    """Nối text vào cuối file (tạo mới nếu chưa có).

    Mô tả:
        Nối *text* vào cuối file. Nếu newline=True và file chưa kết thúc
        bằng newline thì tự chèn một newline trước.
    Args:
        path: đường dẫn file.
        text: text cần nối.
        newline: tự xuống dòng trước khi nối nếu cần.
    Returns:
        {"ok": True, "path": str, "bytes_added": int}
        hoặc {"ok": False, "error": str}.
    Raises:
        Không raise — mọi lỗi trả về dict.
    """
    target, err = _resolve_safe(path)
    if err:
        return {"ok": False, "error": err}
    assert target is not None
    if not isinstance(text, str):
        return {"ok": False, "error": "text phải là chuỗi"}
    if target.exists() and not target.is_file():
        return {"ok": False, "error": "path là thư mục"}
    if target.is_file() and _is_binary(target):
        return {"ok": False, "error": "binary file refused"}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = text
        if newline and target.exists() and target.stat().st_size > 0:
            with open(target, "rb") as fh:
                fh.seek(-1, 2)
                if fh.read(1) not in (b"\n", b"\r"):
                    payload = "\n" + text
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(payload)
    except OSError as exc:
        return {"ok": False, "error": f"không append được: {exc}"}
    return {"ok": True, "path": str(target),
            "bytes_added": len(payload.encode("utf-8"))}


def delete(path: str) -> Dict[str, Any]:
    """Xóa file hoặc thư mục RỖNG.

    Mô tả:
        Xóa file thường. Thư mục chỉ xóa khi rỗng. Từ chối: symlink,
        thư mục con của cwd, thư mục không rỗng, và chính cwd.
    Args:
        path: đường dẫn cần xóa.
    Returns:
        {"ok": True, "path": str} hoặc {"ok": False, "error": str}.
    Raises:
        Không raise — mọi lỗi trả về dict.
    """
    target, err = _resolve_safe(path)
    if err:
        return {"ok": False, "error": err}
    assert target is not None
    root = Path.cwd().resolve()
    if target == root:
        return {"ok": False, "error": "từ chối xóa thư mục làm việc hiện tại"}
    if not target.exists() and not target.is_symlink():
        return {"ok": False, "error": f"không tồn tại: {path}"}
    if target.is_symlink():
        return {"ok": False, "error": "từ chối xóa symlink (nguy cơ trỏ ra ngoài)"}
    try:
        if target.is_dir():
            if any(target.iterdir()):
                return {"ok": False, "error": "từ chối xóa thư mục không rỗng"}
            target.rmdir()
        else:
            target.unlink()
    except OSError as exc:
        return {"ok": False, "error": f"không xóa được: {exc}"}
    return {"ok": True, "path": str(target)}


def list_dir(path: str = ".", recursive: bool = False,
             limit: int = 200) -> Dict[str, Any]:
    """Liệt kê nội dung thư mục.

    Mô tả:
        Liệt kê file/thư mục con (sắp xếp theo tên). Hỗ trợ liệt kê đệ quy.
    Args:
        path: thư mục cần liệt kê (mặc định ".").
        recursive: True để liệt kê đệ quy.
        limit: số entry tối đa (1..5000).
    Returns:
        {"ok": True, "path": str, "entries": [{"name","type","size"}], "truncated": bool}
        hoặc {"ok": False, "error": str}.
    Raises:
        Không raise — mọi lỗi trả về dict.
    """
    target, err = _resolve_safe(path)
    if err:
        return {"ok": False, "error": err}
    assert target is not None
    if not target.exists():
        return {"ok": False, "error": f"không tồn tại: {path}"}
    if not target.is_dir():
        return {"ok": False, "error": f"không phải thư mục: {path}"}
    if not isinstance(limit, int) or not 1 <= limit <= 5000:
        return {"ok": False, "error": "limit phải trong khoảng 1..5000"}
    try:
        items = target.rglob("*") if recursive else target.iterdir()
        entries: List[Dict[str, Any]] = []
        truncated = False
        for p in sorted(items, key=lambda x: x.name):
            if len(entries) >= limit:
                truncated = True
                break
            try:
                is_dir = p.is_dir()
                entries.append({
                    "name": str(p.relative_to(target)),
                    "type": "dir" if is_dir else ("file" if p.is_file() else "other"),
                    "size": 0 if is_dir else p.stat().st_size,
                })
            except OSError:
                continue
    except OSError as exc:
        return {"ok": False, "error": f"không liệt kê được: {exc}"}
    return {"ok": True, "path": str(target), "entries": entries,
            "truncated": truncated}


def info(path: str) -> Dict[str, Any]:
    """Lấy metadata của path (tồn tại, kích thước, mtime, số dòng...).

    Mô tả:
        Tương đương stat gọn nhẹ: tồn tại hay không, size (bytes),
        mtime (ISO 8601 UTC), số dòng (file text), có phải thư mục không.
    Args:
        path: đường dẫn cần xem.
    Returns:
        {"ok": True, "exists": bool, "size": int, "mtime": str,
         "lines": int, "is_dir": bool}
        hoặc {"ok": False, "error": str} khi path không hợp lệ.
        Path không tồn tại -> {"ok": True, "exists": False}.
    Raises:
        Không raise — mọi lỗi trả về dict.
    """
    target, err = _resolve_safe(path)
    if err:
        return {"ok": False, "error": err}
    assert target is not None
    if not target.exists():
        return {"ok": True, "exists": False}
    is_dir = target.is_dir()
    try:
        stat = target.stat()
    except OSError as exc:
        return {"ok": False, "error": f"không stat được: {exc}"}
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    lines = 0
    if not is_dir and not _is_binary(target):
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as fh:
                lines = sum(1 for _ in fh)
        except OSError:
            lines = 0
    return {
        "ok": True,
        "exists": True,
        "path": str(target),
        "size": stat.st_size,
        "mtime": mtime,
        "lines": lines,
        "is_dir": is_dir,
    }


__all__ = ["read", "write", "edit", "append", "delete", "list_dir", "info"]
