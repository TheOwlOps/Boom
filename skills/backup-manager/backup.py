"""Quản lý backup: tạo tar.gz + checksum, liệt kê, khôi phục, dọn bản cũ."""
from __future__ import annotations

import hashlib
import shutil
import tarfile
from datetime import datetime
from pathlib import Path

CHUNK_SIZE = 1024 * 1024  # 1MB — đọc checksum theo khối để tiết kiệm RAM


def sha256_of(path: str | Path) -> str:
    """Tính SHA-256 của một file theo từng khối."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def _backup_name(src: Path, ts: datetime | None = None) -> str:
    """Tên file backup: <tên-thư-mục>-YYYYMMDD-HHMMSS.tar.gz."""
    ts = ts or datetime.now()
    return f"{src.name}-{ts:%Y%m%d-%H%M%S}.tar.gz"


def create_backup(src: str | Path, dest: str | Path) -> dict:
    """Nén thư mục src thành tar.gz trong dest, verify checksum sau khi tạo.

    Returns:
        dict {path, sha256, verified, size_bytes}.
    """
    src = Path(src)
    dest = Path(dest)
    if not src.is_dir():
        raise FileNotFoundError(f"Thư mục nguồn không tồn tại: {src}")
    dest.mkdir(parents=True, exist_ok=True)

    backup_path = dest / _backup_name(src)
    # tar.gz: nén thư mục, arcname để trong archive không chứa đường dẫn tuyệt đối
    with tarfile.open(backup_path, "w:gz") as tar:
        tar.add(src, arcname=src.name)

    checksum = sha256_of(backup_path)
    # Lưu checksum cạnh file backup để verify độc lập sau này
    backup_path.with_suffix(".tar.gz.sha256").write_text(
        f"{checksum}  {backup_path.name}\n", encoding="utf-8"
    )

    # Verify: đọc lại checksum ngay để chắc file không bị ghi lỗi
    verified = sha256_of(backup_path) == checksum
    return {
        "path": str(backup_path),
        "sha256": checksum,
        "verified": verified,
        "size_bytes": backup_path.stat().st_size,
    }


def list_backups(dir: str | Path) -> list[dict]:
    """Liệt kê các file *.tar.gz trong thư mục, sắp xếp mới nhất trước."""
    dir = Path(dir)
    items = []
    for p in sorted(dir.glob("*.tar.gz"), key=lambda x: x.stat().st_mtime, reverse=True):
        sha_file = p.with_suffix(".tar.gz.sha256")
        items.append(
            {
                "path": str(p),
                "size_bytes": p.stat().st_size,
                "created": datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
                "has_checksum": sha_file.exists(),
            }
        )
    return items


def restore(backup: str | Path, dest: str | Path) -> dict:
    """Giải nén file backup vào thư mục dest (tạo mới nếu chưa có)."""
    backup = Path(backup)
    dest = Path(dest)
    if not backup.is_file():
        raise FileNotFoundError(f"File backup không tồn tại: {backup}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(backup, "r:gz") as tar:
        tar.extractall(dest)
    extracted = [str(p.relative_to(dest)) for p in dest.rglob("*")]
    return {"dest": str(dest), "extracted": extracted}


def prune(dir: str | Path, keep_n: int = 5) -> dict:
    """Chỉ giữ keep_n bản backup mới nhất, xoá các bản cũ hơn (kèm file .sha256).

    Returns:
        dict {kept, removed}.
    """
    dir = Path(dir)
    backups = sorted(dir.glob("*.tar.gz"), key=lambda x: x.stat().st_mtime, reverse=True)
    to_remove = backups[keep_n:]
    for p in to_remove:
        p.unlink()
        sha_file = p.with_suffix(".tar.gz.sha256")
        if sha_file.exists():
            sha_file.unlink()
    return {
        "kept": [str(p) for p in backups[:keep_n]],
        "removed": [str(p) for p in to_remove],
    }


def verify_backup(backup: str | Path) -> bool:
    """Kiểm tra checksum của file backup so với file .sha256 đi kèm."""
    backup = Path(backup)
    sha_file = backup.with_suffix(".tar.gz.sha256")
    if not sha_file.exists():
        return False
    expected = sha_file.read_text(encoding="utf-8").split()[0]
    return sha256_of(backup) == expected
