"""Manifest validation for Boom plugins.

Reads and validates plugin.json into a PluginManifest object.
Stdlib + pydantic only.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from pydantic import BaseModel, field_validator

# Plugin name: lowercase, digits, dashes/underscores, no leading dash
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Semver X.Y.Z, allows prerelease like 1.0.0-beta, 1.0.0-rc.1
# (build metadata +... not accepted, keeps the format tight)
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

# Permissions a plugin may request
VALID_PERMISSIONS = {"network", "filesystem", "shell", "env"}

# Path traversal patterns in entry — blocks paths escaping the plugin dir
TRAVERSAL_RE = re.compile(r"(^|[\\/])\.\.(?:[\\/]|$)")


class PluginDependency(BaseModel):
    """A plugin dependency: name + version constraint."""

    name: str
    version: str

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not v or not NAME_RE.match(v):
            # NOTE: "không hợp lệ" is pinned by evals/test_manifest.py:82
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
    """Validated manifest of a Boom plugin (the plugin.json contents)."""

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
            # NOTE: "không hợp lệ" is pinned by evals/test_manifest.py:82
            raise ValueError(
                f"tên plugin '{v}' không hợp lệ "
                "(chỉ gồm chữ thường, số, '-', '_' và không bắt đầu bằng dấu gạch)"
            )
        return v

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        if not v or not SEMVER_RE.match(v):
            # NOTE: "semver" substring is pinned by evals/test_manifest.py:90
            raise ValueError(
                f"version '{v}' không đúng định dạng semver (ví dụ: 1.2.3, 1.0.0-beta)"
            )
        return v

    @field_validator("description")
    @classmethod
    def _check_description(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("description must not be empty")
        return v

    @field_validator("permissions")
    @classmethod
    def _check_permissions(cls, v: List[str]) -> List[str]:
        for p in v:
            if p not in VALID_PERMISSIONS:
                # NOTE: "permission '{p}' không hợp lệ" pinned by
                # evals/test_manifest.py:98
                raise ValueError(
                    f"permission '{p}' không hợp lệ "
                    f"(giá trị cho phép: {', '.join(sorted(VALID_PERMISSIONS))})"
                )
        return v

    @field_validator("entry")
    @classmethod
    def _check_entry(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("entry must not be empty")
        v = v.strip()
        # Block path traversal: "../x", "..\\x", "a/../b", absolute entries, Windows drives
        if (
            TRAVERSAL_RE.search(v)
            or v.startswith(("/", "\\"))
            or re.match(r"^[A-Za-z]:[\\/]", v)
        ):
            raise ValueError(
                f"invalid entry '{v}' (path traversal and absolute "
                "paths are not allowed)"
            )
        return v


def _friendly_error(e: Exception, path: Path) -> str:
    """Turns a pydantic ValidationError into a short one-line error."""
    from pydantic import ValidationError

    if isinstance(e, ValidationError):
        parts = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"]) if err["loc"] else ""
            msg = err["msg"]
            # pydantic v2 prefixes "Value error, " — strip it for brevity
            msg = re.sub(r"^Value error,\s*", "", msg)
            parts.append(f"{loc}: {msg}" if loc else msg)
        return f"{path}: " + "; ".join(parts)
    return f"{path}: {e}"


def load(path: Path) -> PluginManifest:
    """Reads plugin.json from a plugin dir (or a file directly) and validates it.

    Returns a PluginManifest.
    NOTE: error messages are pinned in Vietnamese by evals/test_manifest.py.
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

    # Report missing required fields clearly before pydantic gets involved
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

# Supported constraint kinds:
#   ^1.2.3   compatible:        >=1.2.3, <2.0.0   (npm rules for 0.x)
#   ~1.2.3   patch-level:       >=1.2.3, <1.3.0
#   >=1.0.0 / <=2.0.0 / >1.0.0 / <2.0.0
#   =1.2.3 or 1.2.3   exact match
CONSTRAINT_RE = re.compile(
    r"^\s*(?P<op>\^|~|>=|<=|>|<|=)?\s*(?P<ver>.+?)\s*$"
)


def _parse_version(ver: str) -> Tuple[int, int, int, Tuple]:
    """Parses semver into a comparable tuple.

    Prerelease (1.0.0-beta) < the release with the same numbers (1.0.0): uses
    (1, ()) for releases and (0, tuple(identifiers)) for prereleases, so a
    release always sorts above prereleases of the same core.
    """
    m = SEMVER_RE.match(ver.strip())
    if not m:
        raise ValueError(f"version '{ver}' is not valid semver")
    major, minor, patch, pre = m.group(1), m.group(2), m.group(3), m.group(4)
    if pre is None:
        pre_key: Tuple = (1,)  # release: above every prerelease
    else:
        # Split identifiers; numbers compare as numbers, strings as strings (semver rules)
        ids = []
        for ident in pre.split("."):
            ids.append((0, int(ident)) if ident.isdigit() else (1, ident))
        pre_key = (0, tuple(ids))
    return int(major), int(minor), int(patch), pre_key


def _cmp(a: Tuple, b: Tuple) -> int:
    """Compares 2 version tuples: -1 / 0 / 1."""
    return (a > b) - (a < b)


def satisfies(version: str, constraint: str) -> bool:
    """Checks whether a version satisfies a constraint.

    Examples:
        satisfies("1.2.5", "^1.2.3")  -> True
        satisfies("1.3.0", "~1.2.3")  -> False
        satisfies("2.0.0", ">=1.0.0") -> True
    """
    m = CONSTRAINT_RE.match(constraint)
    if not m or not m.group("ver"):
        raise ValueError(f"invalid constraint '{constraint}'")

    op = m.group("op") or ""
    base = _parse_version(m.group("ver"))
    ver = _parse_version(version)
    core = base[:3]

    if op == "^":
        # Compatible: keep the leftmost nonzero number (npm rules)
        major, minor, patch = core
        if major > 0:
            upper = (major + 1, 0, 0, (1,))
        elif minor > 0:
            upper = (0, minor + 1, 0, (1,))
        else:
            upper = (0, 0, patch + 1, (1,))
        return _cmp(ver, base) >= 0 and _cmp(ver, upper) < 0

    if op == "~":
        # Patch-level: same major.minor, patch >=
        # (only full X.Y.Z supported — simple and unambiguous for plugins)
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

    # op "" or "=": exact match (prereleases included)
    return _cmp(ver, base) == 0


def check_dependencies(
    manifest: PluginManifest, installed: Dict[str, str]
) -> List[str]:
    """Checks the manifest's dependencies against installed plugins.

    Returns a list of problem strings (empty = every dependency satisfied).
    NOTE: message text pinned in Vietnamese by evals/test_manifest.py:155-158.
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
