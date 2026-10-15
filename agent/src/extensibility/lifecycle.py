"""
Plugin Lifecycle Manager cho Boom.

Mở rộng ý tưởng của ``plugin_registry.py`` (scan_and_load load tất cả
không thứ tự) bằng vòng đời đầy đủ: install -> enable -> disable ->
uninstall, trong đó enable đi theo thứ tự dependency (topological sort).

Một plugin là thư mục chứa:
    plugin.json   - manifest: {"name", "version", "entry", "dependencies"}
    entry.py      - entry module, có hàm register(ctx) (kiểu cũ) và/hoặc
                    các hook lifecycle: on_enable(ctx), on_disable(),
                    on_uninstall() (không bắt buộc)

Chỉ dùng stdlib.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Validate manifest (tối giản, tự chứa — không phụ thuộc module khác)
# ---------------------------------------------------------------------------

def validate_manifest(data: Any) -> Dict[str, str]:
    """Kiểm tra một manifest dict vừa parse từ plugin.json.

    Trả về {"name": ..., "version": ..., "entry": ..., "dependencies": [...]}
    đã chuẩn hoá, hoặc raise ValueError kèm thông báo tiếng Việt.
    """
    if not isinstance(data, dict):
        raise ValueError("plugin.json không hợp lệ: nội dung phải là object JSON")

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("plugin.json không hợp lệ: thiếu trường 'name'")
    name = name.strip()
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError(f"plugin.json không hợp lệ: tên plugin không hợp lệ: {name!r}")

    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"plugin '{name}': thiếu trường 'version' hợp lệ")

    entry = data.get("entry", "entry.py")
    if not isinstance(entry, str) or not entry.strip():
        raise ValueError(f"plugin '{name}': trường 'entry' không hợp lệ")
    entry = entry.strip()
    if entry.startswith("/") or ".." in Path(entry).parts:
        raise ValueError(f"plugin '{name}': 'entry' không được trỏ ra ngoài thư mục plugin")

    dependencies = data.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(
        isinstance(d, str) and d.strip() for d in dependencies
    ):
        raise ValueError(f"plugin '{name}': 'dependencies' phải là danh sách chuỗi")
    dependencies = [d.strip() for d in dependencies]
    if name in dependencies:
        raise ValueError(f"plugin '{name}': không được phụ thuộc vào chính nó")

    return {"name": name, "version": version.strip(), "entry": entry,
            "dependencies": dependencies}


def load_manifest(path: Path) -> Dict[str, Any]:
    """Đọc + parse plugin.json, raise ValueError (tiếng Việt) nếu sai."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"không đọc được plugin.json: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"plugin.json không phải JSON hợp lệ: {exc}") from exc
    return validate_manifest(data)


# ---------------------------------------------------------------------------
# Topological sort theo dependency
# ---------------------------------------------------------------------------

def topo_sort(nodes: Dict[str, List[str]]) -> List[str]:
    """Sắp xếp topological: node nào cũng đứng SAU các dependency của nó.

    ``nodes``: dict {name: [danh sách tên dependency]}. Dependency không có
    trong ``nodes`` được bỏ qua (giả định đã được kiểm tra tồn tại trước đó).

    Dùng thuật toán Kahn: lặp đi lặp lại việc lấy các node không còn
    dependency chưa xử lý. Nếu sau một vòng không lấy được node nào mà vẫn
    còn node chưa xếp -> đồ thị có chu trình -> raise ValueError tiếng Việt.
    """
    # Sao chép để không làm bẩn dữ liệu gốc
    remaining: Dict[str, set] = {
        name: {d for d in deps if d in nodes} for name, deps in nodes.items()
    }
    order: List[str] = []

    while remaining:
        # Các node đã "sẵn sàng": không còn dependency nào chưa được xếp
        ready = sorted(name for name, deps in remaining.items() if not deps)
        if not ready:
            cycle = ", ".join(sorted(remaining))
            raise ValueError(
                f"phát hiện dependency vòng tròn giữa các plugin: {cycle}"
            )
        for name in ready:
            order.append(name)
            del remaining[name]
        # Xoá các node vừa xếp khỏi tập dependency của node còn lại
        done = set(ready)
        for deps in remaining.values():
            deps -= done

    return order


# ---------------------------------------------------------------------------
# PluginManager
# ---------------------------------------------------------------------------

class PluginManager:
    """Quản lý vòng đời plugin: install / enable / disable / uninstall."""

    def __init__(self, plugins_dir: Path, ctx: Optional[Dict[str, Any]] = None):
        self.plugins_dir = Path(plugins_dir)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        # {name: {"manifest": dict, "enabled": bool, "module": module|None}}
        self.plugins: Dict[str, Dict[str, Any]] = {}
        self.ctx: Dict[str, Any] = ctx if ctx is not None else {"tools": {}}
        self.refresh()

    # -- nội bộ -----------------------------------------------------------

    def refresh(self) -> None:
        """Quét lại plugins_dir, nạp manifest của mọi plugin đã install."""
        self.plugins.clear()
        for p in sorted(self.plugins_dir.iterdir()):
            manifest_file = p / "plugin.json"
            if p.is_dir() and manifest_file.exists():
                try:
                    manifest = load_manifest(manifest_file)
                except ValueError:
                    continue  # manifest hỏng: bỏ qua, không crash manager
                self.plugins[manifest["name"]] = {
                    "manifest": manifest,
                    "enabled": False,
                    "module": None,
                    "dir": p,
                }

    def _load_module(self, name: str):
        manifest = self.plugins[name]["manifest"]
        entry_file = self.plugins[name]["dir"] / manifest["entry"]
        if not entry_file.exists():
            raise ValueError(
                f"plugin '{name}': không tìm thấy entry file '{manifest['entry']}'"
            )
        spec = importlib.util.spec_from_file_location(
            f"boom_plugin_{name}", entry_file)
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def _dependents_enabled(self, name: str) -> List[str]:
        """Danh sách plugin đang enabled mà phụ thuộc (trực tiếp) vào name."""
        result = []
        for other, info in self.plugins.items():
            if info["enabled"] and name in info["manifest"]["dependencies"]:
                result.append(other)
        return sorted(result)

    def _enable_order(self, name: str) -> List[str]:
        """Thứ tự enable cho name + toàn bộ dependency (transitive)."""
        if name not in self.plugins:
            raise ValueError(f"plugin '{name}' chưa được cài đặt")
        # Thu thập closure: name + mọi dependency đệ quy
        closure = set()

        def collect(n: str):
            if n in closure:
                return
            closure.add(n)
            for dep in self.plugins[n]["manifest"]["dependencies"]:
                if dep not in self.plugins:
                    raise ValueError(
                        f"plugin '{name}': thiếu dependency '{dep}' "
                        f"(cần cài đặt trước)"
                    )
                collect(dep)

        collect(name)
        graph = {n: self.plugins[n]["manifest"]["dependencies"] for n in closure}
        return topo_sort(graph)

    # -- public API -------------------------------------------------------

    def install(self, source_dir: Path) -> dict:
        """Cài plugin từ thư mục nguồn vào plugins_dir."""
        source_dir = Path(source_dir)
        manifest_file = source_dir / "plugin.json"
        if not source_dir.is_dir() or not manifest_file.exists():
            return {"installed": False,
                    "error": "thư mục nguồn không chứa plugin.json"}

        try:
            manifest = load_manifest(manifest_file)
        except ValueError as exc:
            return {"installed": False, "error": str(exc)}
        name = manifest["name"]

        if name in self.plugins:
            return {"installed": False,
                    "error": f"plugin '{name}' đã được cài đặt"}

        missing = [d for d in manifest["dependencies"] if d not in self.plugins]
        if missing:
            return {"installed": False,
                    "error": f"plugin '{name}' thiếu dependency chưa cài đặt: "
                             f"{', '.join(missing)}"}

        entry_src = source_dir / manifest["entry"]
        if not entry_src.exists():
            return {"installed": False,
                    "error": f"plugin '{name}': không tìm thấy entry file "
                             f"'{manifest['entry']}' trong thư mục nguồn"}

        dest = self.plugins_dir / name
        try:
            shutil.copytree(source_dir, dest)
        except OSError as exc:
            return {"installed": False, "error": f"không copy được plugin: {exc}"}

        self.plugins[name] = {"manifest": manifest, "enabled": False,
                              "module": None, "dir": dest}
        return {"installed": True, "name": name}

    def enable(self, name: str) -> dict:
        """Enable plugin: load dependency trước theo topological sort."""
        try:
            order = self._enable_order(name)
        except ValueError as exc:
            return {"enabled": False, "error": str(exc)}

        enabled_now: List[str] = []
        try:
            for pname in order:
                info = self.plugins[pname]
                if info["enabled"]:
                    continue
                module = self._load_module(pname)
                # Hook mới on_enable(ctx); nếu không có thì fallback
                # register(ctx) kiểu cũ để tương thích ngược.
                if hasattr(module, "on_enable"):
                    module.on_enable(self.ctx)
                elif hasattr(module, "register"):
                    module.register(self.ctx)
                info["module"] = module
                info["enabled"] = True
                enabled_now.append(pname)
        except Exception as exc:  # noqa: BLE001 - báo lỗi plugin ra ngoài
            return {"enabled": False,
                    "error": f"plugin '{pname}' enable thất bại: {exc}",
                    "enabled_order": enabled_now}

        return {"enabled": True, "name": name, "enabled_order": enabled_now}

    def disable(self, name: str) -> dict:
        """Disable plugin (giữ module trong memory để enable lại nhanh)."""
        if name not in self.plugins:
            return {"disabled": False,
                    "error": f"plugin '{name}' chưa được cài đặt"}
        info = self.plugins[name]
        if not info["enabled"]:
            return {"disabled": True, "name": name, "note": "đã disable từ trước"}

        dependents = self._dependents_enabled(name)
        if dependents:
            return {"disabled": False,
                    "error": f"không thể disable '{name}': các plugin đang "
                             f"bật còn phụ thuộc vào nó: {', '.join(dependents)}"}

        module = info["module"]
        if module is not None and hasattr(module, "on_disable"):
            try:
                module.on_disable()
            except Exception as exc:  # noqa: BLE001
                return {"disabled": False,
                        "error": f"plugin '{name}': on_disable lỗi: {exc}"}
        info["enabled"] = False
        return {"disabled": True, "name": name}

    def uninstall(self, name: str) -> dict:
        """Gỡ cài đặt: disable trước, gọi on_uninstall nếu có, rồi xoá thư mục."""
        if name not in self.plugins:
            return {"uninstalled": False,
                    "error": f"plugin '{name}' chưa được cài đặt"}

        result = self.disable(name)
        if not result.get("disabled"):
            return {"uninstalled": False, "error": result.get("error", "")}

        module = self.plugins[name]["module"]
        if module is not None and hasattr(module, "on_uninstall"):
            try:
                module.on_uninstall()
            except Exception as exc:  # noqa: BLE001
                return {"uninstalled": False,
                        "error": f"plugin '{name}': on_uninstall lỗi: {exc}"}

        try:
            shutil.rmtree(self.plugins[name]["dir"])
        except OSError as exc:
            return {"uninstalled": False,
                    "error": f"không xoá được thư mục plugin '{name}': {exc}"}
        del self.plugins[name]
        return {"uninstalled": True, "name": name}

    def list(self) -> list:
        """Liệt kê mọi plugin đã install."""
        return [
            {"name": name,
             "version": info["manifest"]["version"],
             "enabled": info["enabled"],
             "dependencies": list(info["manifest"]["dependencies"])}
            for name, info in sorted(self.plugins.items())
        ]
