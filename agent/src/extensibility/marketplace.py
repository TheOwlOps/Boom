"""Plugin marketplace/registry for Boom.

Packages, stores, lists, searches, and installs plugins from a local registry
(on-disk directory). Stdlib + pydantic only.

On-disk registry layout::

    <registry_path>/
        index.json                 # index: name -> list of versions + metadata
        <name>-<version>.zip       # packaged plugin

Each plugin (source directory) follows the Boom standard: ``plugin.json``
(manifest) + ``entry.py`` (defines ``register(ctx)``).

All raised errors are ``ValueError`` with English messages.
"""
from __future__ import annotations

import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, field_validator

# --- Naming/version rules (kept in sync with extensibility/manifest.py) ---

# Plugin name: lowercase, digits, '-'/'_', must not start with a dash
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Semver X.Y.Z, prereleases allowed like 1.0.0-beta
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

# The Boom version this marketplace runs against
# (used to check min_boom_version at install time)
CURRENT_BOOM_VERSION = "0.1.0"

# Directories/files skipped when packaging (excluded from the .zip)
_SKIP_NAMES = {".git", "__pycache__", ".DS_Store", ".venv", "venv"}


# ---------------------------------------------------------------------------
# PluginMetadata
# ---------------------------------------------------------------------------


class PluginMetadata(BaseModel):
    """Registration metadata of a plugin in the marketplace."""

    name: str                     # unique name, e.g. hello-world
    version: str                  # semver, e.g. 1.2.0
    description: str              # short description
    author: str = ""              # author
    tags: List[str] = []          # search keywords, e.g. ["weather", "api"]
    min_boom_version: str = "0.0.0"  # minimum Boom version required to run the plugin

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not v or not _NAME_RE.match(v):
            raise ValueError(
                f"invalid plugin name '{v}' "
                "(lowercase, digits, '-', '_' only, must not start with a dash)"
            )
        return v

    @field_validator("version", "min_boom_version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        if not v or not _SEMVER_RE.match(v):
            raise ValueError(
                f"version '{v}' is not valid semver (e.g. 1.2.3)"
            )
        return v

    @field_validator("description")
    @classmethod
    def _check_description(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("description must not be empty")
        return v


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _version_tuple(ver: str) -> tuple:
    """Convert semver to (major, minor, patch, prerelease_key) for comparison.

    A release (1.0.0) always ranks above a prerelease with the same numbers
    (1.0.0-beta).
    """
    m = _SEMVER_RE.match(ver.strip())
    assert m, ver  # already validated by pydantic before being called
    major, minor, patch, pre = m.group(1), m.group(2), m.group(3), m.group(4)
    if pre is None:
        pre_key: tuple = (1,)
    else:
        ids = [(0, int(i)) if i.isdigit() else (1, i) for i in pre.split(".")]
        pre_key = (0, tuple(ids))
    return (int(major), int(minor), int(patch), pre_key)


def _metadata_from_plugin_json(plugin_dir: Path) -> PluginMetadata:
    """Read metadata from plugin.json inside the plugin directory."""
    manifest_file = plugin_dir / "plugin.json"
    if not manifest_file.exists():
        raise ValueError(f"plugin.json not found in: {plugin_dir}")
    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"plugin.json is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise ValueError("plugin.json must be a JSON object")
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
        raise ValueError(f"plugin.json is missing a required field: {e.args[0]}")
    except Exception as e:  # pydantic validation errors
        raise ValueError(f"invalid metadata: {e}")


def _zip_name(name: str, version: str) -> str:
    """Standard .zip filename of a package: <name>-<version>.zip."""
    return f"{name}-{version}.zip"


def _safe_member_name(raw: str) -> str:
    """Validate a filename inside a zip — blocks zip-slip (../ or absolute paths)."""
    if raw.startswith(("/", "\\")) or ".." in Path(raw).parts:
        raise ValueError(f"unsafe filename inside package: {raw!r}")
    return raw


# ---------------------------------------------------------------------------
# PluginMarketplace
# ---------------------------------------------------------------------------


class PluginMarketplace:
    """Boom's local plugin marketplace/registry.

    ``registry_path`` can be passed at construction or per method (the method
    argument wins).
    """

    def __init__(self, registry_path: Optional[Path | str] = None) -> None:
        self.registry_path = Path(registry_path) if registry_path else None

    # -- internal -----------------------------------------------------------

    def _registry(self, registry_path: Optional[Path | str]) -> Path:
        """Resolve the registry path: argument > constructor > error."""
        if registry_path is not None:
            return Path(registry_path)
        if self.registry_path is not None:
            return self.registry_path
        raise ValueError("registry_path not specified")

    def _read_index(self, registry: Path) -> Dict[str, List[dict]]:
        """Read index.json; a missing registry counts as empty."""
        index_file = registry / "index.json"
        if not index_file.exists():
            return {}
        try:
            data = json.loads(index_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise ValueError(f"index.json is corrupted, cannot read: {e}")
        if not isinstance(data, dict):
            raise ValueError("index.json must be a JSON object")
        return data

    def _write_index(self, registry: Path, index: Dict[str, List[dict]]) -> None:
        """Write index.json (write-then-rename to avoid mid-write corruption)."""
        registry.mkdir(parents=True, exist_ok=True)
        tmp = registry / "index.json.tmp"
        tmp.write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(registry / "index.json")

    # -- public -------------------------------------------------------------

    def publish(
        self,
        plugin_dir: Path | str,
        registry_path: Optional[Path | str] = None,
    ) -> PluginMetadata:
        """Package a plugin directory into .zip + update registry metadata.

        Process:
          1. Read + validate metadata from ``plugin_dir/plugin.json``.
          2. Verify ``entry.py`` exists (Boom plugin standard).
          3. Zip every file (skipping .git, __pycache__, ...) into
             ``<name>-<version>.zip`` inside the registry.
          4. Add the metadata to ``index.json``.

        Version conflicts: if (name, version) already exists in the registry,
        raises ``ValueError`` — bump the version to publish a new release.

        Returns the published PluginMetadata.
        """
        plugin_dir = Path(plugin_dir)
        registry = self._registry(registry_path)

        if not plugin_dir.is_dir():
            raise ValueError(f"plugin directory does not exist: {plugin_dir}")

        meta = _metadata_from_plugin_json(plugin_dir)

        if not (plugin_dir / "entry.py").exists():
            raise ValueError(
                f"plugin '{meta.name}' is missing entry.py — not a valid Boom plugin"
            )

        index = self._read_index(registry)
        existing = [e for e in index.get(meta.name, [])
                    if e.get("version") == meta.version]
        if existing:
            raise ValueError(
                f"version conflict: plugin '{meta.name}' version "
                f"{meta.version} already exists in the registry — bump the version"
            )

        registry.mkdir(parents=True, exist_ok=True)
        zip_path = registry / _zip_name(meta.name, meta.version)

        # Package: keep relative directory structure, skip junk files/dirs
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for src in sorted(plugin_dir.rglob("*")):
                if src.is_dir() or any(part in _SKIP_NAMES for part in src.parts):
                    continue
                arcname = src.relative_to(plugin_dir).as_posix()
                zf.write(src, arcname)

        # Update index: add the new version, sorted by semver ascending
        entries = index.setdefault(meta.name, [])
        entries.append(meta.model_dump())
        entries.sort(key=lambda e: _version_tuple(e["version"]))
        self._write_index(registry, index)

        return meta

    def list_available(
        self, registry_path: Optional[Path | str] = None
    ) -> List[PluginMetadata]:
        """List every plugin in the registry (all versions), sorted by name."""
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
        """Search plugins by name, description, author, or tags (case-insensitive).

        Returns the matches, sorted by name.
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
        """Install a plugin from the registry into Boom's plugins directory.

        Process:
          1. Find the ``<name>-<version>.zip`` package in the registry.
          2. Check the plugin's ``min_boom_version`` against ``boom_version``.
          3. Safely extract (zip-slip blocked) into ``plugins_dir/<name>``.

        Conflict handling:
          - Installing the exact version already installed → ``ValueError``
            (no pointless reinstall).
          - Installing a different version → overwrites the old directory
            (upgrade/downgrade).

        Returns the installed plugin's directory path.
        """
        registry = self._registry(registry_path)
        if plugins_dir is None:
            raise ValueError("plugins_dir not specified")

        index = self._read_index(registry)
        entries = index.get(name, [])
        match = next((e for e in entries if e.get("version") == version), None)
        if match is None:
            available = [e.get("version") for e in entries]
            hint = f" (available versions: {', '.join(available)})" if available else ""
            raise ValueError(
                f"plugin '{name}' version {version} not found"
                f" in the registry{hint}"
            )

        meta = PluginMetadata(**match)
        if _version_tuple(boom_version) < _version_tuple(meta.min_boom_version):
            raise ValueError(
                f"plugin '{name}' {version} requires Boom "
                f"{meta.min_boom_version} minimum, current is {boom_version}"
            )

        zip_path = registry / _zip_name(name, version)
        if not zip_path.exists():
            raise ValueError(
                f"package {_zip_name(name, version)} is listed in the index but "
                f"its file is missing from the registry"
            )

        plugins_dir = Path(plugins_dir)
        dest = plugins_dir / name

        # Version conflict: already installed at this exact version → skip
        existing_manifest = dest / "plugin.json"
        if existing_manifest.exists():
            try:
                installed_meta = _metadata_from_plugin_json(dest)
            except ValueError:
                # broken old manifest → treat as not installed, allow overwrite to fix it
                installed_meta = None
            if installed_meta is not None and installed_meta.version == version:
                raise ValueError(
                    f"version conflict: plugin '{name}' version {version} "
                    "is already installed — nothing to do"
                )

        # Safe extraction: validate ALL filenames before extracting any
        plugins_dir.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)

        with zipfile.ZipFile(zip_path, "r") as zf:
            members = zf.infolist()
            # Validate ALL filenames before extracting any file
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

        # Verify: the package must contain a plugin.json matching the requested name/version
        installed = _metadata_from_plugin_json(dest)
        if installed.name != name or installed.version != version:
            shutil.rmtree(dest, ignore_errors=True)
            raise ValueError(
                f"registry package mismatch: expected {name} {version}, "
                f"got {installed.name} {installed.version}"
            )

        return dest
