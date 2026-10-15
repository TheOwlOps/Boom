"""Test cho PluginMarketplace — chạy bằng:  python3 test_marketplace.py

Bao phủ: publish -> list -> install -> plugin chạy được, search,
xung đột version, kiểm tra min_boom_version, lỗi đầu vào.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src" / "extensibility"))

from marketplace import PluginMarketplace, PluginMetadata  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture: tạo một plugin mẫu trên đĩa
# ---------------------------------------------------------------------------

ENTRY_CODE = '''\
def register(ctx):
    """Đăng ký tool demo của plugin."""
    ctx.register_tool("hello_tool", lambda: "hello from plugin",
                      description="tool chào hỏi demo")
'''

PLUGIN_JSON = {
    "name": "hello-world",
    "version": "1.0.0",
    "description": "Plugin chào hỏi demo cho Boom",
    "author": "boom-test",
    "tags": ["demo", "greeting"],
    "min_boom_version": "0.1.0",
}


def make_plugin(base: Path, name: str = "hello-world",
                version: str = "1.0.0",
                description: str = "Plugin chào hỏi demo cho Boom") -> Path:
    """Tạo thư mục plugin mẫu: plugin.json + entry.py."""
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    manifest = dict(PLUGIN_JSON)
    manifest.update(name=name, version=version, description=description)
    (d / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (d / "entry.py").write_text(ENTRY_CODE, encoding="utf-8")
    return d


class FakeCtx:
    """Context giả mô phỏng PluginContext của Boom để kiểm tra plugin chạy được."""

    def __init__(self):
        self.tools = {}

    def register_tool(self, name, func, description="", schema=None):
        self.tools[name] = {"func": func, "description": description}


def load_entry(entry_path: Path):
    """Load entry.py bằng importlib (giống PluginRegistry của Boom)."""
    spec = importlib.util.spec_from_file_location("test_plugin_entry", entry_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

PASS = 0


def check(cond: bool, label: str) -> None:
    """Một assertion: PASS thì đếm, FAIL thì ném AssertionError ngay."""
    global PASS
    assert cond, f"FAIL: {label}"
    PASS += 1
    print(f"  PASS: {label}")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "src"            # nơi chứa plugin nguồn để publish
        registry = tmp / "registry"  # registry local
        plugins_dir = tmp / "plugins"  # thư mục plugin của Boom

        mp = PluginMarketplace(registry)

        # ---- 1. publish --------------------------------------------------
        print("[1] publish")
        plugin_dir = make_plugin(src)
        meta = mp.publish(plugin_dir)  # dùng registry_path từ constructor
        check(isinstance(meta, PluginMetadata), "publish trả về PluginMetadata")
        check(meta.name == "hello-world" and meta.version == "1.0.0",
              "metadata đúng name/version")
        check((registry / "hello-world-1.0.0.zip").exists(),
              "file zip đã được tạo trong registry")
        check((registry / "index.json").exists(),
              "index.json đã được tạo trong registry")
        # zip chứa đúng file, không lẫn thư mục rác
        with zipfile.ZipFile(registry / "hello-world-1.0.0.zip") as zf:
            names = zf.namelist()
        check("plugin.json" in names and "entry.py" in names,
              "zip chứa plugin.json và entry.py")

        # ---- 2. list_available -------------------------------------------
        print("[2] list_available")
        items = mp.list_available(registry)
        check(len(items) == 1 and items[0].name == "hello-world",
              "list thấy đúng 1 plugin đã publish")
        check(items[0].tags == ["demo", "greeting"],
              "metadata trong list giữ nguyên tags")

        # ---- 3. install_from_registry ------------------------------------
        print("[3] install_from_registry")
        dest = mp.install_from_registry("hello-world", "1.0.0",
                                        registry, plugins_dir)
        check(dest == plugins_dir / "hello-world",
              "install trả về đúng thư mục đích")
        check((dest / "entry.py").exists() and (dest / "plugin.json").exists(),
              "plugin đã cài có entry.py và plugin.json")

        # ---- 4. plugin cài xong chạy được --------------------------------
        print("[4] plugin chạy được")
        mod = load_entry(dest / "entry.py")
        check(hasattr(mod, "register"), "entry.py có hàm register")
        ctx = FakeCtx()
        mod.register(ctx)
        check("hello_tool" in ctx.tools,
              "register(ctx) đăng ký tool thành công")
        check(ctx.tools["hello_tool"]["func"]() == "hello from plugin",
              "tool của plugin chạy ra kết quả đúng")

        # ---- 5. search ---------------------------------------------------
        print("[5] search")
        # publish thêm 1 plugin khác để search có cái phân biệt
        make_plugin(src, name="weather-vn", version="2.1.0",
                    description="Dự báo thời tiết Việt Nam")
        mp.publish(src / "weather-vn", registry)
        check(len(mp.search("hello")) == 1
              and mp.search("hello")[0].name == "hello-world",
              "search theo tên tìm đúng plugin")
        check(any(m.name == "weather-vn" for m in mp.search("thời tiết")),
              "search theo mô tả (tiếng Việt) tìm đúng plugin")
        check(mp.search("không-tồn-tại-xyz") == [],
              "search không khớp trả về danh sách rỗng")

        # ---- 6. version conflict khi publish -----------------------------
        print("[6] version conflict (publish)")
        try:
            mp.publish(src / "hello-world", registry)  # cùng name+version
            check(False, "publish trùng version phải ném lỗi")
        except ValueError as e:
            check("version conflict" in str(e),
                  f"publish trùng version ném ValueError: {e}")
        # bản mới hơn vẫn publish được
        make_plugin(src, name="hello-world", version="1.1.0")
        mp.publish(src / "hello-world", registry)
        versions = [m.version for m in mp.list_available(registry)
                    if m.name == "hello-world"]
        check(versions == ["1.0.0", "1.1.0"],
              "publish version mới OK, list thấy cả 2 version")

        # ---- 7. version conflict khi install -----------------------------
        print("[7] version conflict (install)")
        try:
            mp.install_from_registry("hello-world", "1.0.0", registry, plugins_dir)
            check(False, "install đúng version đã cài phải ném lỗi")
        except ValueError as e:
            check("version conflict" in str(e),
                  f"install trùng version ném ValueError: {e}")
        # cài version khác = nâng cấp, ghi đè thư mục cũ
        dest2 = mp.install_from_registry("hello-world", "1.1.0",
                                         registry, plugins_dir)
        check(dest2 == plugins_dir / "hello-world",
              "install version mới (upgrade) thành công")
        installed_ver = json.loads(
            (plugins_dir / "hello-world" / "plugin.json").read_text("utf-8")
        )["version"]
        check(installed_ver == "1.1.0",
              "sau upgrade, plugin.json trên đĩa là version mới")

        # ---- 8. lỗi đầu vào ----------------------------------------------
        print("[8] lỗi đầu vào")
        try:
            mp.install_from_registry("ghost-plugin", "9.9.9", registry, plugins_dir)
            check(False, "install plugin không tồn tại phải ném lỗi")
        except ValueError as e:
            check("không tìm thấy" in str(e),
                  f"install plugin lạ ném ValueError: {e}")
        try:
            mp.install_from_registry("weather-vn", "9.9.9", registry, plugins_dir)
            check(False, "install version không tồn tại phải ném lỗi")
        except ValueError as e:
            check("không tìm thấy" in str(e),
                  f"install version lạ ném ValueError: {e}")
        # min_boom_version không thỏa
        make_plugin(src, name="future-plugin", version="1.0.0")
        fp = src / "future-plugin"
        manifest = json.loads((fp / "plugin.json").read_text("utf-8"))
        manifest["min_boom_version"] = "99.0.0"
        (fp / "plugin.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        mp.publish(fp, registry)
        try:
            mp.install_from_registry("future-plugin", "1.0.0",
                                     registry, plugins_dir, boom_version="0.1.0")
            check(False, "min_boom_version không thỏa phải ném lỗi")
        except ValueError as e:
            check("tối thiểu" in str(e),
                  f"min_boom_version không thỏa ném ValueError: {e}")

    print(f"\nTẤT CẢ {PASS} ASSERTIONS PASS ✔")


if __name__ == "__main__":
    main()
