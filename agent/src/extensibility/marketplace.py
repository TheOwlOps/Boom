"""Plugin marketplace/registry cho Boom.

Đóng gói, lưu trữ, liệt kê, tìm kiếm và cài đặt plugin từ một
registry local (thư mục trên đĩa). Chỉ dùng stdlib + pydantic.

Bố cục registry trên đĩa::

    <registry_path>/
        index.json                 # chỉ mục: name -> danh sách version + metadata
        <name>-<version>.zip       # gói plugin đã đóng gói

Mỗi plugin (thư mục nguồn) tuân thủ chuẩn Boom: ``plugin.json`` (manifest)
+ ``entry.py`` (định nghĩa hàm ``register(ctx)``).

Toàn bộ lỗi ném ra đều là ``ValueError`` với thông điệp tiếng Việt.
"""
from __future__ import annotations

import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, field_validator

# --- Quy tắc đặt tên/version (giữ đồng bộ với extensibility/manifest.py) ---

# Tên plugin: chữ thường, số, '-'/'_', không bắt đầu bằng dấu gạch
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Semver X.Y.Z, cho phép prerelease như 1.0.0-beta
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

# Phiên bản Boom mà marketplace này đang chạy cùng
# (dùng để kiểm tra min_boom_version khi cài đặt)
CURRENT_BOOM_VERSION = "0.1.0"

# Thư mục/file bỏ qua khi đóng gói (không đưa vào .zip)
_SKIP_NAMES = {".git", "__pycache__", ".DS_Store", ".venv", "venv"}


# ---------------------------------------------------------------------------
# PluginMetadata
# ---------------------------------------------------------------------------


class PluginMetadata(BaseModel):
    """Metadata đăng ký của một plugin trong marketplace."""

    name: str                     # tên duy nhất, ví dụ: hello-world
    version: str                  # semver, ví dụ: 1.2.0
    description: str              # mô tả ngắn
    author: str = ""              # tác giả
    tags: List[str] = []          # từ khóa tìm kiếm, ví dụ: ["weather", "api"]
    min_boom_version: str = "0.0.0"  # Boom tối thiểu yêu cầu để chạy plugin

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not v or not _NAME_RE.match(v):
            raise ValueError(
                f"tên plugin '{v}' không hợp lệ "
                "(chỉ gồm chữ thường, số, '-', '_' và không bắt đầu bằng dấu gạch)"
            )
        return v

    @field_validator("version", "min_boom_version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        if not v or not _SEMVER_RE.match(v):
            raise ValueError(
                f"version '{v}' không đúng định dạng semver (ví dụ: 1.2.3)"
            )
        return v

    @field_validator("description")
    @classmethod
    def _check_description(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("description không được để trống")
        return v


# ---------------------------------------------------------------------------
# Hàm phụ trợ
# ---------------------------------------------------------------------------


def _version_tuple(ver: str) -> tuple:
    """Chuyển semver thành tuple (major, minor, patch, prerelease_key) để so sánh.

    Bản release (1.0.0) luôn lớn hơn prerelease cùng số (1.0.0-beta).
    """
    m = _SEMVER_RE.match(ver.strip())
    assert m, ver  # đã validate bằng pydantic trước khi gọi
    major, minor, patch, pre = m.group(1), m.group(2), m.group(3), m.group(4)
    if pre is None:
        pre_key: tuple = (1,)
    else:
        ids = [(0, int(i)) if i.isdigit() else (1, i) for i in pre.split(".")]
        pre_key = (0, tuple(ids))
    return (int(major), int(minor), int(patch), pre_key)


def _metadata_from_plugin_json(plugin_dir: Path) -> PluginMetadata:
    """Đọc metadata từ plugin.json trong thư mục plugin."""
    manifest_file = plugin_dir / "plugin.json"
    if not manifest_file.exists():
        raise ValueError(f"không tìm thấy plugin.json trong: {plugin_dir}")
    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"plugin.json không phải JSON hợp lệ: {e}")
    if not isinstance(data, dict):
        raise ValueError("plugin.json phải là một JSON object")
    try:
        return PluginMetadata(
            name=data["name"],
            version=data["version"],
            description=data["description"],
            author=data.get("author", ""),
            tags=data.get("tags", []),
            min_boom_version=data.get("min_boom_version", "0.0.0"),
        )
    except KeyError as e:
        raise ValueError(f"plugin.json thiếu trường bắt buộc: {e.args[0]}")
    except Exception as e:  # lỗi validate pydantic
        raise ValueError(f"metadata không hợp lệ: {e}")


def _zip_name(name: str, version: str) -> str:
    """Tên file .zip chuẩn của một gói: <name>-<version>.zip."""
    return f"{name}-{version}.zip"


def _safe_member_name(raw: str) -> str:
    """Kiểm tra tên file trong zip — chặn zip-slip (../ hoặc đường dẫn tuyệt đối)."""
    if raw.startswith(("/", "\\")) or ".." in Path(raw).parts:
        raise ValueError(f"tên file trong gói không an toàn: {raw!r}")
    return raw


# ---------------------------------------------------------------------------
# PluginMarketplace
# ---------------------------------------------------------------------------


class PluginMarketplace:
    """Marketplace/registry plugin local của Boom.

    ``registry_path`` có thể truyền lúc khởi tạo hoặc truyền riêng cho từng
    phương thức (ưu tiên tham số của phương thức).
    """

    def __init__(self, registry_path: Optional[Path | str] = None) -> None:
        self.registry_path = Path(registry_path) if registry_path else None

    # -- nội bộ -----------------------------------------------------------

    def _registry(self, registry_path: Optional[Path | str]) -> Path:
        """Lấy đường dẫn registry: tham số > constructor > lỗi."""
        if registry_path is not None:
            return Path(registry_path)
        if self.registry_path is not None:
            return self.registry_path
        raise ValueError("chưa chỉ định registry_path")

    def _read_index(self, registry: Path) -> Dict[str, List[dict]]:
        """Đọc index.json; registry chưa có thì coi như rỗng."""
        index_file = registry / "index.json"
        if not index_file.exists():
            return {}
        try:
            data = json.loads(index_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise ValueError(f"index.json hỏng, không đọc được: {e}")
        if not isinstance(data, dict):
            raise ValueError("index.json phải là một JSON object")
        return data

    def _write_index(self, registry: Path, index: Dict[str, List[dict]]) -> None:
        """Ghi index.json (ghi tạm rồi đổi tên để tránh file hỏng giữa chừng)."""
        registry.mkdir(parents=True, exist_ok=True)
        tmp = registry / "index.json.tmp"
        tmp.write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(registry / "index.json")

    # -- công khai --------------------------------------------------------

    def publish(
        self,
        plugin_dir: Path | str,
        registry_path: Optional[Path | str] = None,
    ) -> PluginMetadata:
        """Đóng gói một thư mục plugin thành .zip + cập nhật metadata vào registry.

        Quy trình:
          1. Đọc + validate metadata từ ``plugin_dir/plugin.json``.
          2. Kiểm tra ``entry.py`` tồn tại (chuẩn plugin Boom).
          3. Nén toàn bộ file (bỏ qua .git, __pycache__...) thành
             ``<name>-<version>.zip`` trong registry.
          4. Thêm metadata vào ``index.json``.

        Xung đột version: nếu cặp (name, version) đã tồn tại trong registry
        thì ném ``ValueError`` — muốn phát hành bản mới phải tăng version.

        Trả về PluginMetadata đã publish.
        """
        plugin_dir = Path(plugin_dir)
        registry = self._registry(registry_path)

        if not plugin_dir.is_dir():
            raise ValueError(f"thư mục plugin không tồn tại: {plugin_dir}")

        meta = _metadata_from_plugin_json(plugin_dir)

        if not (plugin_dir / "entry.py").exists():
            raise ValueError(
                f"plugin '{meta.name}' thiếu entry.py — không đúng chuẩn plugin Boom"
            )

        index = self._read_index(registry)
        existing = [e for e in index.get(meta.name, [])
                    if e.get("version") == meta.version]
        if existing:
            raise ValueError(
                f"version conflict: plugin '{meta.name}' version "
                f"{meta.version} đã tồn tại trong registry — hãy tăng version"
            )

        registry.mkdir(parents=True, exist_ok=True)
        zip_path = registry / _zip_name(meta.name, meta.version)

        # Đóng gói: giữ cấu trúc thư mục tương đối, bỏ qua file/thư mục rác
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for src in sorted(plugin_dir.rglob("*")):
                if src.is_dir() or any(part in _SKIP_NAMES for part in src.parts):
                    continue
                arcname = src.relative_to(plugin_dir).as_posix()
                zf.write(src, arcname)

        # Cập nhật index: thêm version mới, sắp xếp theo semver tăng dần
        entries = index.setdefault(meta.name, [])
        entries.append(meta.model_dump())
        entries.sort(key=lambda e: _version_tuple(e["version"]))
        self._write_index(registry, index)

        return meta

    def list_available(
        self, registry_path: Optional[Path | str] = None
    ) -> List[PluginMetadata]:
        """Liệt kê mọi plugin có trong registry (mọi version), sắp xếp theo tên."""
        registry = self._registry(registry_path)
        index = self._read_index(registry)
        result: List[PluginMetadata] = []
        for name in sorted(index):
            for entry in index[name]:
                result.append(PluginMetadata(**entry))
        return result

    def search(
        self, query: str, registry_path: Optional[Path | str] = None
    ) -> List[PluginMetadata]:
        """Tìm plugin theo tên, mô tả, tác giả hoặc tags (không phân biệt hoa/thường).

        Trả về danh sách khớp, sắp xếp theo tên.
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        hits: List[PluginMetadata] = []
        for meta in self.list_available(registry_path):
            haystack = " ".join(
                [meta.name, meta.description, meta.author, *meta.tags]
            ).lower()
            if q in haystack:
                hits.append(meta)
        return hits

    def install_from_registry(
        self,
        name: str,
        version: str,
        registry_path: Optional[Path | str] = None,
        plugins_dir: Optional[Path | str] = None,
        boom_version: str = CURRENT_BOOM_VERSION,
    ) -> Path:
        """Cài đặt một plugin từ registry vào thư mục plugins của Boom.

        Quy trình:
          1. Tìm gói ``<name>-<version>.zip`` trong registry.
          2. Kiểm tra ``min_boom_version`` của plugin so với ``boom_version``.
          3. Giải nén an toàn (chặn zip-slip) vào ``plugins_dir/<name>``.

        Xử lý xung đột:
          - Cài đúng version đã có sẵn → ``ValueError`` (không cài đè vô ích).
          - Cài version khác → ghi đè thư mục cũ (nâng cấp/hạ cấp).

        Trả về đường dẫn thư mục plugin đã cài.
        """
        registry = self._registry(registry_path)
        if plugins_dir is None:
            raise ValueError("chưa chỉ định plugins_dir")

        index = self._read_index(registry)
        entries = index.get(name, [])
        match = next((e for e in entries if e.get("version") == version), None)
        if match is None:
            available = [e.get("version") for e in entries]
            hint = f" (các version có sẵn: {', '.join(available)})" if available else ""
            raise ValueError(
                f"không tìm thấy plugin '{name}' version {version}"
                f" trong registry{hint}"
            )

        meta = PluginMetadata(**match)
        if _version_tuple(boom_version) < _version_tuple(meta.min_boom_version):
            raise ValueError(
                f"plugin '{name}' {version} yêu cầu Boom tối thiểu "
                f"{meta.min_boom_version}, hiện tại là {boom_version}"
            )

        zip_path = registry / _zip_name(name, version)
        if not zip_path.exists():
            raise ValueError(
                f"gói {_zip_name(name, version)} ghi trong index nhưng "
                f"không tìm thấy file trong registry"
            )

        plugins_dir = Path(plugins_dir)
        dest = plugins_dir / name

        # Xung đột version: đã cài đúng version này rồi thì không cài lại
        existing_manifest = dest / "plugin.json"
        if existing_manifest.exists():
            try:
                installed_meta = _metadata_from_plugin_json(dest)
            except ValueError:
                # manifest cũ hỏng → coi như chưa cài, cho phép ghi đè để sửa
                installed_meta = None
            if installed_meta is not None and installed_meta.version == version:
                raise ValueError(
                    f"version conflict: plugin '{name}' version {version} "
                    "đã được cài đặt — không cần cài lại"
                )

        # Giải nén an toàn: chặn zip-slip trước khi ghi file
        plugins_dir.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)

        with zipfile.ZipFile(zip_path, "r") as zf:
            members = zf.infolist()
            # Kiểm tra TẤT CẢ tên file trước khi giải nén bất kỳ file nào
            for m in members:
                _safe_member_name(m.filename)
            for m in members:
                target = dest / m.filename
                if m.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(m) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)

        # Kiểm tra lại: gói phải chứa plugin.json khớp name/version đã yêu cầu
        installed = _metadata_from_plugin_json(dest)
        if installed.name != name or installed.version != version:
            shutil.rmtree(dest, ignore_errors=True)
            raise ValueError(
                f"gói trong registry không khớp: mong đợi {name} {version}, "
                f"nhận được {installed.name} {installed.version}"
            )

        return dest
