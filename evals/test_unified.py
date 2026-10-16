#!/usr/bin/env python3
"""Test hệ thống plugin thống nhất (unified) — chỉ stdlib.

Chạy:  python3 test_unified.py   (exit 0 = tất cả PASS)

Nội dung kiểm tra:
  1. plugin.json của hello-plugin đọc được và đủ trường bắt buộc.
  2. entry.py import được bằng importlib; on_enable(ctx) đăng ký tool "hello".
  3. Tool hello("Anh") trả về chuỗi chứa "Anh".
  4. on_disable() chạy không crash.
  5. SKILL.md tồn tại và không rỗng.
  6. __init__.py import an toàn kể cả khi lifecycle/manifest/sandbox chưa tồn tại
     (try/except ImportError), và import đúng khi các module đã có.
"""

import importlib.util
import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_SRC = HERE / "examples" / "hello-plugin"
# The unified plugin package under test is agent/src/extensibility: its
# __init__ re-exports PluginManager/PluginManifest/PluginSandbox with
# try/except imports so the package stays importable while components
# are still being written.
INIT_SRC = HERE.parent / "agent" / "src" / "extensibility" / "__init__.py"

passed = 0
failed = 0
failed_labels = []


def check(cond, label):
    """Một assertion: đếm PASS/FAIL, in kết quả ngay."""
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        failed_labels.append(label)
        print(f"  [FAIL] {label}")


def load_entry(entry_path: Path, module_name: str):
    """Load entry.py bằng importlib, trả về module."""
    spec = importlib.util.spec_from_file_location(module_name, entry_path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    print("== Test 1: manifest (plugin.json) ==")
    with tempfile.TemporaryDirectory(prefix="boom-plugin-test-") as tmp:
        # Mô phỏng bước install: copy plugin vào thư mục tạm
        installed = Path(tmp) / "hello-plugin"
        shutil.copytree(PLUGIN_SRC, installed)

        manifest_path = installed / "plugin.json"
        check(manifest_path.exists(), "plugin.json tồn tại sau khi install (tempdir)")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        check(isinstance(manifest, dict), "plugin.json parse được thành dict JSON")

        # Các trường bắt buộc theo schema hệ thống mới
        for field in ("name", "version", "description", "author", "permissions", "entry"):
            check(field in manifest, f"trường bắt buộc '{field}' tồn tại trong manifest")

        check(manifest.get("name") == "hello-plugin",
              "manifest['name'] == 'hello-plugin'")
        check(manifest.get("version") == "1.0.0",
              "manifest['version'] == '1.0.0'")
        check(isinstance(manifest.get("description"), str)
              and manifest["description"].strip(),
              "manifest['description'] là chuỗi không rỗng")
        check(isinstance(manifest.get("author"), str)
              and manifest["author"].strip(),
              "manifest['author'] là chuỗi không rỗng")
        check(isinstance(manifest.get("permissions"), list),
              "manifest['permissions'] là list")
        check(manifest.get("entry") == "entry.py",
              "manifest['entry'] == 'entry.py'")

        print("== Test 2: entry.py — import + vòng đời ==")
        entry_path = installed / manifest["entry"]
        check(entry_path.exists(), "file entry.py tồn tại")

        mod = load_entry(entry_path, "hello_plugin_test")
        check(mod is not None, "importlib load được entry.py")
        check(hasattr(mod, "on_enable"), "entry.py có hook on_enable")
        check(hasattr(mod, "on_disable"), "entry.py có hook on_disable")

        ctx = {"tools": {}}
        try:
            mod.on_enable(ctx)
            enable_ok = True
        except Exception:
            enable_ok = False
            traceback.print_exc()
        check(enable_ok, "on_enable(ctx) chạy không raise")

        check("hello" in ctx["tools"], 'tool "hello" được đăng ký vào ctx["tools"]')
        check(callable(ctx["tools"].get("hello")), 'tool "hello" là callable')

        print("== Test 3: gọi tool hello ==")
        try:
            result = ctx["tools"]["hello"]("Anh")
            call_ok = True
        except Exception:
            result = ""
            call_ok = False
            traceback.print_exc()
        check(call_ok, 'gọi hello("Anh") không raise')
        check(isinstance(result, str) and "Anh" in result,
              'kết quả hello("Anh") chứa "Anh"')
        check(result.startswith("Xin chào"),
              'kết quả có format chào tiếng Việt ("Xin chào, ...!")')

        print("== Test 4: on_disable ==")
        try:
            mod.on_disable()
            disable_ok = True
        except Exception:
            disable_ok = False
            traceback.print_exc()
        check(disable_ok, "on_disable() chạy không crash")

        print("== Test 5: SKILL.md ==")
        skill_md = installed / "SKILL.md"
        check(skill_md.exists(), "SKILL.md tồn tại")
        check(skill_md.stat().st_size > 0, "SKILL.md không rỗng")
        content = skill_md.read_text(encoding="utf-8")
        check("hello" in content.lower(), 'SKILL.md nhắc tới tool "hello"')

    print("== Test 6: __init__.py an toàn khi module thành phần chưa có ==")
    with tempfile.TemporaryDirectory(prefix="boom-pkg-test-") as tmp:
        # Gói chỉ có __init__.py, KHÔNG có lifecycle/manifest/sandbox
        # (mô phỏng lúc 3 worker khác chưa xong việc)
        pkg = Path(tmp) / "unified_pkg"
        pkg.mkdir()
        shutil.copy(INIT_SRC, pkg / "__init__.py")

        sys.path.insert(0, tmp)
        try:
            import unified_pkg
            check(True, "import package không crash khi 3 module chưa tồn tại")
            check(unified_pkg.__all__ == ["PluginManager", "PluginManifest", "PluginSandbox"],
                  "__all__ đúng 3 export yêu cầu")
            check(unified_pkg.PluginManager is None
                  and unified_pkg.PluginManifest is None
                  and unified_pkg.PluginSandbox is None,
                  "export thiếu module có giá trị None (không crash)")
            check(unified_pkg.available() == {
                "PluginManager": False, "PluginManifest": False, "PluginSandbox": False},
                "available() báo cả 3 module chưa sẵn sàng")
        except Exception:
            traceback.print_exc()
            check(False, "import package không crash khi 3 module chưa tồn tại")
        finally:
            sys.path.remove(tmp)
            sys.modules.pop("unified_pkg", None)

        # Gói đầy đủ: 3 module tồn tại -> import thật
        pkg2 = Path(tmp) / "unified_pkg2"
        pkg2.mkdir()
        shutil.copy(INIT_SRC, pkg2 / "__init__.py")
        for name in ("lifecycle", "manifest", "sandbox"):
            cls = {"lifecycle": "PluginManager",
                   "manifest": "PluginManifest",
                   "sandbox": "PluginSandbox"}[name]
            (pkg2 / f"{name}.py").write_text(
                f"class {cls}:\n    pass\n", encoding="utf-8")

        sys.path.insert(0, tmp)
        try:
            import unified_pkg2
            check(unified_pkg2.PluginManager.__name__ == "PluginManager",
                  "có lifecycle.py -> PluginManager import được class thật")
            check(unified_pkg2.PluginManifest.__name__ == "PluginManifest",
                  "có manifest.py -> PluginManifest import được class thật")
            check(unified_pkg2.PluginSandbox.__name__ == "PluginSandbox",
                  "có sandbox.py -> PluginSandbox import được class thật")
            check(all(unified_pkg2.available().values()),
                  "available() báo cả 3 module đã sẵn sàng")
        except Exception:
            traceback.print_exc()
            check(False, "import đầy đủ 3 module không crash")
        finally:
            sys.path.remove(tmp)
            sys.modules.pop("unified_pkg2", None)

    print()
    print(f"Kết quả: {passed} PASS, {failed} FAIL (tổng {passed + failed} assertions)")
    if failed_labels:
        print("Các case FAIL:")
        for label in failed_labels:
            print(f"  - {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
