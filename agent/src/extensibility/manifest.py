"""Manifest validation cho Boom plugins.

Đọc và validate plugin.json thành đối tượng PluginManifest.
Chỉ dùng stdlib + pydantic.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from pydantic import BaseModel, field_validator

# Tên plugin: chữ thường, số, gạch ngang/gạch dưới, không bắt đầu bằng dấu gạch
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Semver X.Y.Z, cho phép prerelease như 1.0.0-beta, 1.0.0-rc.1
# (build metadata +... không chấp nhận, giữ format gọn)
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

# Quyền plugin được phép xin
VALID_PERMISSIONS = {"network", "filesystem", "shell", "env"}

# Các pattern path traversal trong entry — chặn đường dẫn trèo ra ngoài thư mục plugin
TRAVERSAL_RE = re.compile(r"(^|[\\/])\.\.(?:[\\/]|$)")


class PluginDependency(BaseModel):
    """Một dependency của plugin: tên + ràng buộc version."""

    name: str
    version: str

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not v or not NAME_RE.match(v):
            raise ValueError(
                f"tên dependency '{v}' không hợp lệ "
                "(chỉ gồm chữ thường, số, '-', '_' và không bắt đầu bằng dấu gạch)"
            )
        return v

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("dependency thiếu ràng buộc version")
        return v.strip()


class PluginManifest(BaseModel):
    """Manifest đã validate của một Boom plugin (nội dung plugin.json)."""

    name: str
    version: str
    description: str
    author: str = ""
    dependencies: List[PluginDependency] = []
    permissions: List[str] = []
    entry: str = "entry.py"

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not v or not NAME_RE.match(v):
            raise ValueError(
                f"tên plugin '{v}' không hợp lệ "
                "(chỉ gồm chữ thường, số, '-', '_' và không bắt đầu bằng dấu gạch)"
            )
        return v

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        if not v or not SEMVER_RE.match(v):
            raise ValueError(
                f"version '{v}' không đúng định dạng semver (ví dụ: 1.2.3, 1.0.0-beta)"
            )
        return v

    @field_validator("description")
    @classmethod
    def _check_description(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("description không được để trống")
        return v

    @field_validator("permissions")
    @classmethod
    def _check_permissions(cls, v: List[str]) -> List[str]:
        for p in v:
            if p not in VALID_PERMISSIONS:
                raise ValueError(
                    f"permission '{p}' không hợp lệ "
                    f"(giá trị cho phép: {', '.join(sorted(VALID_PERMISSIONS))})"
                )
        return v

    @field_validator("entry")
    @classmethod
    def _check_entry(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("entry không được để trống")
        v = v.strip()
        # Chặn path traversal: "../x", "..\\x", "a/../b", entry tuyệt đối, drive Windows
        if (
            TRAVERSAL_RE.search(v)
            or v.startswith(("/", "\\"))
            or re.match(r"^[A-Za-z]:[\\/]", v)
        ):
            raise ValueError(
                f"entry '{v}' không hợp lệ (không cho phép path traversal "
                "hoặc đường dẫn tuyệt đối)"
            )
        return v


def _friendly_error(e: Exception, path: Path) -> str:
    """Biến pydantic ValidationError thành câu tiếng Việt ngắn gọn."""
    from pydantic import ValidationError

    if isinstance(e, ValidationError):
        parts = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"]) if err["loc"] else ""
            msg = err["msg"]
            # pydantic v2 prefix "Value error, " — bỏ đi cho gọn
            msg = re.sub(r"^Value error,\s*", "", msg)
            parts.append(f"{loc}: {msg}" if loc else msg)
        return f"{path}: " + "; ".join(parts)
    return f"{path}: {e}"


def load(path: Path) -> PluginManifest:
    """Đọc plugin.json từ thư mục plugin (hoặc file trực tiếp) và validate.

    Trả về PluginManifest. Lỗi luôn bằng tiếng Việt.
    """
    path = Path(path)
    manifest_file = path / "plugin.json" if path.is_dir() else path

    if not manifest_file.exists():
        raise ValueError(f"không tìm thấy plugin.json tại: {manifest_file}")

    try:
        raw_text = manifest_file.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"không đọc được plugin.json: {e}")

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"plugin.json không phải JSON hợp lệ: {e}")

    if not isinstance(data, dict):
        raise ValueError("plugin.json phải là một JSON object")

    # Báo thiếu trường bắt buộc bằng câu rõ ràng trước khi vào pydantic
    for field in ("name", "version", "description"):
        if field not in data or data[field] in (None, ""):
            raise ValueError(f"plugin.json thiếu trường bắt buộc: {field}")

    try:
        return PluginManifest(**data)
    except Exception as e:
        raise ValueError(_friendly_error(e, manifest_file)) from e


# ---------------------------------------------------------------------------
# Version constraint: satisfies(version, constraint)
# ---------------------------------------------------------------------------

# Kiểu constraint được hỗ trợ:
#   ^1.2.3   tương thích:        >=1.2.3, <2.0.0   (theo quy tắc npm cho 0.x)
#   ~1.2.3   patch-level:        >=1.2.3, <1.3.0
#   >=1.0.0 / <=2.0.0 / >1.0.0 / <2.0.0
#   =1.2.3 hoặc 1.2.3   khớp chính xác
CONSTRAINT_RE = re.compile(
    r"^\s*(?P<op>\^|~|>=|<=|>|<|=)?\s*(?P<ver>.+?)\s*$"
)


def _parse_version(ver: str) -> Tuple[int, int, int, Tuple]:
    """Parse semver thành tuple so sánh được.

    Prerelease (1.0.0-beta) < bản release cùng số (1.0.0): dùng cờ
    (1, ()) cho release và (0, tuple(identifiers)) cho prerelease,
    nên release luôn lớn hơn prerelease cùng core.
    """
    m = SEMVER_RE.match(ver.strip())
    if not m:
        raise ValueError(f"version '{ver}' không đúng định dạng semver")
    major, minor, patch, pre = m.group(1), m.group(2), m.group(3), m.group(4)
    if pre is None:
        pre_key: Tuple = (1,)  # release: lớn hơn mọi prerelease
    else:
        # Tách identifiers; số so với số, chuỗi so với chuỗi (quy tắc semver)
        ids = []
        for ident in pre.split("."):
            ids.append((0, int(ident)) if ident.isdigit() else (1, ident))
        pre_key = (0, tuple(ids))
    return int(major), int(minor), int(patch), pre_key


def _cmp(a: Tuple, b: Tuple) -> int:
    """So sánh 2 tuple version: -1 / 0 / 1."""
    return (a > b) - (a < b)


def satisfies(version: str, constraint: str) -> bool:
    """Kiểm tra version có thỏa constraint không.

    Ví dụ:
        satisfies("1.2.5", "^1.2.3")  -> True
        satisfies("1.3.0", "~1.2.3")  -> False
        satisfies("2.0.0", ">=1.0.0") -> True
    """
    m = CONSTRAINT_RE.match(constraint)
    if not m or not m.group("ver"):
        raise ValueError(f"constraint '{constraint}' không hợp lệ")

    op = m.group("op") or ""
    base = _parse_version(m.group("ver"))
    ver = _parse_version(version)
    core = base[:3]

    if op == "^":
        # Tương thích: giữ nguyên số trái nhất khác 0 (quy tắc npm)
        major, minor, patch = core
        if major > 0:
            upper = (major + 1, 0, 0, (1,))
        elif minor > 0:
            upper = (0, minor + 1, 0, (1,))
        else:
            upper = (0, 0, patch + 1, (1,))
        return _cmp(ver, base) >= 0 and _cmp(ver, upper) < 0

    if op == "~":
        # Patch-level: cùng major.minor, patch >=
        # (chỉ hỗ trợ dạng đầy đủ X.Y.Z — đơn giản, rõ ràng cho plugin)
        major, minor, _ = core
        upper = (major, minor + 1, 0, (1,))
        return _cmp(ver, base) >= 0 and _cmp(ver, upper) < 0

    if op == ">=":
        return _cmp(ver, base) >= 0
    if op == "<=":
        return _cmp(ver, base) <= 0
    if op == ">":
        return _cmp(ver, base) > 0
    if op == "<":
        return _cmp(ver, base) < 0

    # op "" hoặc "=": khớp chính xác (kể cả prerelease)
    return _cmp(ver, base) == 0


def check_dependencies(
    manifest: PluginManifest, installed: Dict[str, str]
) -> List[str]:
    """Kiểm tra dependencies của manifest so với các plugin đã cài.

    Trả về danh sách câu lỗi tiếng Việt (rỗng = mọi dependency thỏa).
    """
    problems: List[str] = []
    for dep in manifest.dependencies:
        if dep.name not in installed:
            problems.append(f"thiếu dependency: {dep.name} ({dep.version})")
            continue
        try:
            if not satisfies(installed[dep.name], dep.version):
                problems.append(
                    f"dependency {dep.name} version {installed[dep.name]} "
                    f"không thỏa ràng buộc '{dep.version}'"
                )
        except ValueError as e:
            problems.append(f"dependency {dep.name}: {e}")
    return problems
