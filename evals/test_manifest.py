"""Test thật cho manifest.py — chạy: python3 test_manifest.py"""
import sys
import tempfile
from pathlib import Path

# Import root is agent/src (repo convention); the modules under test live
# in subpackages there (they previously lived next to this file).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))
from extensibility.manifest import (
    PluginManifest,
    load,
    satisfies,
    check_dependencies,
)

PASS = 0


def ok(cond, label):
    global PASS
    assert cond, f"FAIL: {label}"
    PASS += 1
    print(f"  PASS: {label}")


def write_plugin(tmpdir, data: dict) -> Path:
    import json

    d = Path(tmpdir) / "my-plugin"
    d.mkdir(exist_ok=True)
    (d / "plugin.json").write_text(json.dumps(data), encoding="utf-8")
    return d


# --- 1. Manifest hợp lệ đầy đủ fields ---
m = PluginManifest(
    name="my-plugin",
    version="1.2.3",
    description="Plugin demo",
    author="Son",
    dependencies=[{"name": "core-utils", "version": "^1.0.0"}],
    permissions=["network", "filesystem"],
    entry="main.py",
)
ok(m.name == "my-plugin" and m.version == "1.2.3", "manifest hợp lệ đầy đủ fields")
ok(m.dependencies[0].name == "core-utils", "dependency parse đúng name/version")
ok(m.author == "Son" and m.entry == "main.py", "author + entry giữ nguyên")

# version prerelease hợp lệ
m2 = PluginManifest(name="a", version="1.0.0-beta", description="x")
ok(m2.version == "1.0.0-beta", "version prerelease 1.0.0-beta hợp lệ")

# defaults
m3 = PluginManifest(name="a", version="1.0.0", description="x")
ok(m3.entry == "entry.py" and m3.dependencies == [] and m3.permissions == [],
   "defaults: entry=entry.py, dependencies=[], permissions=[]")

# load() từ thư mục plugin thật
with tempfile.TemporaryDirectory() as t:
    d = write_plugin(t, {
        "name": "hello",
        "version": "2.0.0",
        "description": "Xin chào",
        "permissions": ["shell"],
    })
    lm = load(d)
    ok(lm.name == "hello" and lm.permissions == ["shell"], "load() đọc plugin.json từ thư mục")

# --- 2. Thiếu name → lỗi tiếng Việt ---
try:
    load_data = {"version": "1.0.0", "description": "x"}
    with tempfile.TemporaryDirectory() as t:
        d = write_plugin(t, load_data)
        load(d)
    ok(False, "thiếu name phải raise")
except ValueError as e:
    ok("thiếu trường bắt buộc: name" in str(e), f"lỗi thiếu name tiếng Việt: {e}")

# name sai regex
try:
    PluginManifest(name="Bad Name!", version="1.0.0", description="x")
    ok(False, "name sai regex phải raise")
except ValueError as e:
    ok("không hợp lệ" in str(e), f"lỗi name sai regex: {e}")

# --- 3. Version sai format ---
for bad in ("abc", "1.2", "1.2.3.4", "v1.2.3", "1.02.3"):
    try:
        PluginManifest(name="a", version=bad, description="x")
        ok(False, f"version '{bad}' phải raise")
    except ValueError as e:
        ok("semver" in str(e), f"lỗi version '{bad}': {e}")

# --- 4. Permission lạ → lỗi ---
try:
    PluginManifest(name="a", version="1.0.0", description="x",
                   permissions=["network", "hack"])
    ok(False, "permission lạ phải raise")
except ValueError as e:
    ok("permission 'hack' không hợp lệ" in str(e), f"lỗi permission lạ: {e}")

# permission hợp lệ hết
mp = PluginManifest(name="a", version="1.0.0", description="x",
                    permissions=["network", "filesystem", "shell", "env"])
ok(len(mp.permissions) == 4, "4 permission hợp lệ đều pass")

# --- 5. satisfies: ^ ---
ok(satisfies("1.2.5", "^1.2.3"), "^ đúng: 1.2.5 thỏa ^1.2.3")
ok(satisfies("1.9.9", "^1.2.3"), "^ đúng: 1.9.9 thỏa ^1.2.3")
ok(not satisfies("2.0.0", "^1.2.3"), "^ sai: 2.0.0 không thỏa ^1.2.3")
ok(not satisfies("1.2.2", "^1.2.3"), "^ sai: 1.2.2 không thỏa ^1.2.3")
ok(satisfies("0.2.5", "^0.2.3"), "^ 0.x: 0.2.5 thỏa ^0.2.3")
ok(not satisfies("0.3.0", "^0.2.3"), "^ 0.x: 0.3.0 không thỏa ^0.2.3")

# --- 6. satisfies: ~ ---
ok(satisfies("1.2.9", "~1.2.3"), "~ đúng: 1.2.9 thỏa ~1.2.3")
ok(satisfies("1.2.3", "~1.2.3"), "~ đúng: 1.2.3 thỏa ~1.2.3 (biên dưới)")
ok(not satisfies("1.3.0", "~1.2.3"), "~ sai: 1.3.0 không thỏa ~1.2.3")
ok(not satisfies("1.2.2", "~1.2.3"), "~ sai: 1.2.2 không thỏa ~1.2.3")

# --- 7. satisfies: >=, <=, >, < ---
ok(satisfies("1.5.0", ">=1.0.0"), ">= đúng: 1.5.0 thỏa >=1.0.0")
ok(satisfies("1.0.0", ">=1.0.0"), ">= đúng biên: 1.0.0 thỏa >=1.0.0")
ok(not satisfies("0.9.9", ">=1.0.0"), ">= sai: 0.9.9 không thỏa >=1.0.0")
ok(satisfies("2.0.0", "<=2.0.0"), "<= đúng biên: 2.0.0 thỏa <=2.0.0")
ok(not satisfies("2.0.1", "<=2.0.0"), "<= sai: 2.0.1 không thỏa <=2.0.0")

# --- 8. satisfies: exact ---
ok(satisfies("1.2.3", "1.2.3"), "exact đúng: 1.2.3 thỏa 1.2.3")
ok(satisfies("1.2.3", "=1.2.3"), "exact đúng: 1.2.3 thỏa =1.2.3")
ok(not satisfies("1.2.4", "1.2.3"), "exact sai: 1.2.4 không thỏa 1.2.3")
ok(not satisfies("1.2.3-beta", "1.2.3"), "exact sai: prerelease ≠ release")

# prerelease so sánh: release > prerelease
ok(satisfies("1.0.0", ">=1.0.0-beta"), ">= prerelease: 1.0.0 thỏa >=1.0.0-beta")

# --- 9. entry có "../" → lỗi ---
for bad_entry in ("../x.py", "a/../../etc/passwd", "..\\win.py", "/etc/passwd"):
    try:
        PluginManifest(name="a", version="1.0.0", description="x", entry=bad_entry)
        ok(False, f"entry '{bad_entry}' phải raise")
    except ValueError as e:
        ok("entry" in str(e), f"lỗi entry traversal '{bad_entry}': {e}")

# entry con hợp lệ
me = PluginManifest(name="a", version="1.0.0", description="x", entry="sub/dir/entry.py")
ok(me.entry == "sub/dir/entry.py", "entry thư mục con hợp lệ được chấp nhận")

# --- 10. check_dependencies ---
md = PluginManifest(
    name="a", version="1.0.0", description="x",
    dependencies=[{"name": "lib-a", "version": "^1.0.0"},
                  {"name": "lib-b", "version": ">=2.0.0"}],
)
ok(check_dependencies(md, {"lib-a": "1.5.0", "lib-b": "2.1.0"}) == [],
   "check_dependencies: mọi dep thỏa → rỗng")
probs = check_dependencies(md, {"lib-a": "0.9.0"})
ok(len(probs) == 2 and any("không thỏa" in p for p in probs)
   and any("thiếu dependency: lib-b" in p for p in probs),
   f"check_dependencies: báo đúng version sai + thiếu dep: {probs}")

# --- 11. load() lỗi khác ---
with tempfile.TemporaryDirectory() as t:
    bad = Path(t) / "bad"
    bad.mkdir()
    (bad / "plugin.json").write_text("{not json", encoding="utf-8")
    try:
        load(bad)
        ok(False, "JSON hỏng phải raise")
    except ValueError as e:
        ok("không phải JSON hợp lệ" in str(e), f"lỗi JSON hỏng: {e}")

with tempfile.TemporaryDirectory() as t:
    try:
        load(Path(t) / "khong-ton-tai")
        ok(False, "thư mục không tồn tại phải raise")
    except ValueError as e:
        ok("không tìm thấy plugin.json" in str(e), f"lỗi thiếu plugin.json: {e}")

print(f"\nTỔNG: {PASS} assertions PASS")
