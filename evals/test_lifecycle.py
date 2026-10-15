"""Test thật cho PluginManager — chạy: python3 test_lifecycle.py"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lifecycle import PluginManager, topo_sort, validate_manifest  # noqa: E402

passed = 0
failed = 0

def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"FAIL: {label}")


def make_plugin(src_root: Path, name, deps=(), version="1.0.0",
                hooks=("on_enable", "on_disable", "on_uninstall"),
                legacy_register=False):
    """Tạo thư mục plugin giả với plugin.json + entry.py ghi log thứ tự."""
    d = src_root / name
    d.mkdir(parents=True)
    manifest = {"name": name, "version": version, "entry": "entry.py",
                "dependencies": list(deps)}
    (d / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    lines = []
    if "on_enable" in hooks:
        lines.append("def on_enable(ctx):\n"
                     "    ctx.setdefault('order', []).append('" + name + "')\n"
                     "    ctx['tools']['" + name + "_tool'] = True\n")
    if "on_disable" in hooks:
        lines.append("def on_disable():\n"
                     "    pass\n")
    if "on_uninstall" in hooks:
        lines.append("def on_uninstall():\n"
                     "    pass\n")
    if legacy_register:
        lines.append("def register(ctx):\n"
                     "    ctx.setdefault('order', []).append('legacy-" + name + "')\n")
    (d / "entry.py").write_text("\n".join(lines), encoding="utf-8")
    return d


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    plugins_dir = tmp / "plugins"
    src = tmp / "src"
    src.mkdir()

    mgr = PluginManager(plugins_dir)

    # --- install A, B(dep A), C(dep B) ---
    a = make_plugin(src, "A")
    b = make_plugin(src, "B", deps=["A"])
    c = make_plugin(src, "C", deps=["B"])
    r = mgr.install(a); check(r["installed"] is True and r["name"] == "A", "install A")
    r = mgr.install(b); check(r["installed"] is True, "install B")
    r = mgr.install(c); check(r["installed"] is True, "install C")

    # --- install trùng tên ---
    r = mgr.install(a)
    check(r["installed"] is False and "đã được cài đặt" in r["error"], "install trùng tên bị từ chối")

    # --- install thiếu dependency ---
    d_src = make_plugin(src, "D", deps=["Z-missing"])
    r = mgr.install(d_src)
    check(r["installed"] is False and "Z-missing" in r["error"], "install thiếu dep báo lỗi liệt kê")

    # --- install manifest hỏng ---
    bad = src / "Bad"; bad.mkdir()
    (bad / "plugin.json").write_text("{not json", encoding="utf-8")
    r = mgr.install(bad)
    check(r["installed"] is False and "error" in r, "install manifest hỏng bị từ chối")

    # --- enable C -> A, B enable trước đúng thứ tự ---
    mgr.ctx.clear(); mgr.ctx["tools"] = {}
    r = mgr.enable("C")
    check(r["enabled"] is True, "enable C thành công")
    check(r["enabled_order"] == ["A", "B", "C"], "thứ tự enable A,B,C")
    check(mgr.ctx.get("order") == ["A", "B", "C"], "on_enable log đúng thứ tự")
    check(all(mgr.plugins[n]["enabled"] for n in "ABC"), "A,B,C đều enabled")
    check(mgr.ctx["tools"].get("A_tool") is True, "tool của A đã đăng ký vào ctx")

    # --- disable A khi B đang enabled -> bị từ chối ---
    r = mgr.disable("A")
    check(r["disabled"] is False and "B" in r["error"], "disable A bị từ chối vì B còn bật")

    # --- disable C, B rồi A -> OK ---
    r = mgr.disable("C"); check(r["disabled"] is True, "disable C ok")
    r = mgr.disable("B"); check(r["disabled"] is True, "disable B ok")
    r = mgr.disable("A"); check(r["disabled"] is True, "disable A ok sau khi B,C tắt")
    check(all(not mgr.plugins[n]["enabled"] for n in "ABC"), "A,B,C đều disabled")
    check(mgr.plugins["A"]["module"] is not None, "module giữ trong memory sau disable")

    # --- enable lại nhanh (module đã load) ---
    r = mgr.enable("A"); check(r["enabled"] is True, "enable lại A ok")

    # --- plugin legacy chỉ có register(ctx) ---
    leg = make_plugin(src, "Legacy", hooks=(), legacy_register=True)
    r = mgr.install(leg); check(r["installed"] is True, "install plugin legacy")
    mgr.ctx.clear(); mgr.ctx["tools"] = {}
    r = mgr.enable("Legacy")
    check(r["enabled"] is True and mgr.ctx.get("order") == ["legacy-Legacy"],
          "fallback register(ctx) kiểu cũ vẫn chạy")

    # --- list() ---
    names = {p["name"] for p in mgr.list()}
    check(names == {"A", "B", "C", "Legacy"}, "list() đủ 4 plugin")
    entry_c = next(p for p in mgr.list() if p["name"] == "C")
    check(entry_c["version"] == "1.0.0" and entry_c["dependencies"] == ["B"],
          "list() trả đúng version/dependencies")

    # --- uninstall A: phải disable trước, xoá thư mục ---
    mgr.disable("Legacy")
    r = mgr.uninstall("A")
    check(r["uninstalled"] is True, "uninstall A ok")
    check(not (plugins_dir / "A").exists(), "thư mục A biến mất")
    check("A" not in {p["name"] for p in mgr.list()}, "list() không còn A")
    r = mgr.uninstall("A")
    check(r["uninstalled"] is False and "chưa được cài đặt" in r["error"],
          "uninstall plugin không tồn tại báo lỗi")

    # --- circular dependency: X dep Y, Y dep X ---
    check(mgr.install(make_plugin(src, "Y2"))["installed"] is True, "install Y2 (chưa dep)")
    # tạo X dep Y2 rồi sửa manifest Y2 dep X để thành vòng tròn
    x_src = make_plugin(src, "X", deps=["Y2"])
    check(mgr.install(x_src)["installed"] is True, "install X dep Y2")
    y2_manifest = plugins_dir / "Y2" / "plugin.json"
    m = json.loads(y2_manifest.read_text(encoding="utf-8"))
    m["dependencies"] = ["X"]
    y2_manifest.write_text(json.dumps(m), encoding="utf-8")
    mgr.refresh()
    r = mgr.enable("X")
    check(r["enabled"] is False and "vòng tròn" in r["error"],
          "circular dependency X<->Y2 báo lỗi tiếng Việt")

    # --- topo_sort đơn vị ---
    check(topo_sort({"a": [], "b": ["a"], "c": ["b"]}) == ["a", "b", "c"],
          "topo_sort chuỗi tuyến tính")
    try:
        topo_sort({"x": ["y"], "y": ["x"]})
        check(False, "topo_sort phát hiện cycle")
    except ValueError as e:
        check("vòng tròn" in str(e), "topo_sort raise lỗi vòng tròn tiếng Việt")

    # --- validate_manifest đơn vị ---
    try:
        validate_manifest({"name": "", "version": "1"})
        check(False, "validate từ chối name rỗng")
    except ValueError:
        check(True, "validate từ chối name rỗng")
    v = validate_manifest({"name": "P", "version": "2.0"})
    check(v["entry"] == "entry.py" and v["dependencies"] == [],
          "validate mặc định entry/dependencies")

print(f"\nRESULT: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
