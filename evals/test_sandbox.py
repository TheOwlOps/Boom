#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test thật cho PluginSandbox. Chạy:  python3 test_sandbox.py

Mỗi check chạy code plugin thật trong sandbox và đếm PASS/FAIL.
Thoát code 1 nếu có FAIL nào.
"""
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sandbox import PluginSandbox  # noqa: E402

PASSED = 0
FAILED = 0
FAILURES: list[str] = []


def check(name: str, fn) -> None:
    """Chạy fn(); không raise + assert đúng là PASS."""
    global PASSED, FAILED
    try:
        fn()
    except Exception:
        FAILED += 1
        FAILURES.append(name)
        print(f"[FAIL] {name}")
        traceback.print_exc()
    else:
        PASSED += 1
        print(f"[PASS] {name}")


def check_raises(name: str, exc_types, fn) -> None:
    """PASS khi fn() raise đúng một trong exc_types."""
    global PASSED, FAILED
    try:
        fn()
    except exc_types as e:
        PASSED += 1
        print(f"[PASS] {name}  (raise {type(e).__name__}: {e})")
    except Exception:
        FAILED += 1
        FAILURES.append(name)
        print(f"[FAIL] {name} (sai loại exception)")
        traceback.print_exc()
    else:
        FAILED += 1
        FAILURES.append(name)
        print(f"[FAIL] {name} (không raise gì cả!)")


def write_plugin(plugin_dir: Path, code: str, name: str = "entry.py") -> Path:
    """Ghi source plugin vào plugin_dir, trả về entry_path."""
    entry = plugin_dir / name
    entry.write_text(code, encoding="utf-8")
    return entry


# ---------------------------------------------------------------------------
# 1. Plugin lành phải chạy được, kết quả đúng
# ---------------------------------------------------------------------------

def t_math_ok():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, (
            "import math\n"
            "def calc():\n"
            "    return math.floor(math.pi * 100) + math.gcd(12, 18)\n"
        ))
        sb = PluginSandbox(pdir, [])
        assert sb.run(entry, "calc") == 314 + 6, "kết quả math sai"


def t_string_ok():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, (
            "def shout(s):\n"
            "    return s.strip().upper()[::-1]\n"
        ))
        sb = PluginSandbox(pdir, [])
        assert sb.run(entry, "shout", "  hello ") == "OLLEH"


def t_json_ok():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, (
            "import json\n"
            "def pack():\n"
            "    return json.dumps({'a': [1, 2], 'b': 'x'}, sort_keys=True)\n"
        ))
        sb = PluginSandbox(pdir, [])
        assert sb.run(entry, "pack") == '{"a": [1, 2], "b": "x"}'


def t_from_import_ok():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, (
            "from collections import Counter\n"
            "def top():\n"
            "    return Counter('aabc').most_common(1)[0][0]\n"
        ))
        sb = PluginSandbox(pdir, [])
        assert sb.run(entry, "top") == "a"


def t_urllib_parse_ok():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, (
            "import urllib.parse\n"
            "def host():\n"
            "    return urllib.parse.urlparse('https://example.com/x').netloc\n"
        ))
        sb = PluginSandbox(pdir, [])
        assert sb.run(entry, "host") == "example.com"


def t_datetime_ok():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, (
            "import datetime\n"
            "def today():\n"
            "    return datetime.date(2026, 10, 16).isoformat()\n"
        ))
        sb = PluginSandbox(pdir, [])
        assert sb.run(entry, "today") == "2026-10-16"


def t_purepath_ok():
    # Không quyền filesystem: PurePath (thao tác chuỗi) vẫn dùng được
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, (
            "from pathlib import PurePosixPath\n"
            "def base():\n"
            "    return PurePosixPath('/a/b/c.txt').name\n"
        ))
        sb = PluginSandbox(pdir, [])
        assert sb.run(entry, "base") == "c.txt"


# ---------------------------------------------------------------------------
# 2. Import nguy hiểm bị chặn
# ---------------------------------------------------------------------------

def t_os_blocked():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, (
            "import os\n"
            "def evil():\n"
            "    return os.system('rm -rf /')\n"
        ))
        sb = PluginSandbox(pdir, [])
        try:
            sb.run(entry, "evil")
        except ImportError:
            return
        raise AssertionError("import os không bị chặn!")


def t_subprocess_blocked():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, (
            "import subprocess\n"
            "def evil():\n"
            "    return subprocess.run(['id'])\n"
        ))
        sb = PluginSandbox(pdir, [])
        try:
            sb.run(entry, "evil")
        except ImportError:
            return
        raise AssertionError("import subprocess không bị chặn!")


def t_sys_blocked():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, "import sys\ndef f():\n    return sys.version\n")
        sb = PluginSandbox(pdir, [])
        try:
            sb.run(entry, "f")
        except ImportError:
            return
        raise AssertionError("import sys không bị chặn!")


def t_socket_blocked_no_perm():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, "import socket\ndef f():\n    return socket.gethostname()\n")
        sb = PluginSandbox(pdir, [])
        try:
            sb.run(entry, "f")
        except ImportError:
            return
        raise AssertionError("import socket không bị chặn!")


def t_socket_blocked_even_with_network_perm():
    # Quyền "network" hiện chưa mở module network nào -> vẫn chặn
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, "import socket\ndef f():\n    return 1\n")
        sb = PluginSandbox(pdir, ["network"])
        try:
            sb.run(entry, "f")
        except ImportError:
            return
        raise AssertionError("import socket lọt dù có quyền network!")


def t_relative_import_blocked():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, "from . import helper\ndef f():\n    return 1\n")
        sb = PluginSandbox(pdir, [])
        try:
            sb.run(entry, "f")
        except ImportError:
            return
        raise AssertionError("import tương đối không bị chặn!")


def t_dunder_import_os_blocked():
    # Gọi trực tiếp __import__ custom với module ngoài whitelist
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, "def f():\n    m = __import__('os')\n    return m.name\n")
        sb = PluginSandbox(pdir, [])
        try:
            sb.run(entry, "f")
        except ImportError:
            return
        raise AssertionError("__import__('os') không bị chặn!")


# ---------------------------------------------------------------------------
# 3. Builtins nguy hiểm bị loại bỏ
# ---------------------------------------------------------------------------

def t_open_blocked_no_perm():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, "def f():\n    return open('/etc/passwd').read()\n")
        sb = PluginSandbox(pdir, [])
        try:
            sb.run(entry, "f")
        except NameError:
            return
        raise AssertionError("open() lọt khi không có quyền filesystem!")


def t_eval_blocked():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, "def f():\n    return eval('1+1')\n")
        sb = PluginSandbox(pdir, [])
        try:
            sb.run(entry, "f")
        except NameError:
            return
        raise AssertionError("eval() không bị chặn!")


def t_exec_blocked():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, "def f():\n    exec('x = 1')\n    return 1\n")
        sb = PluginSandbox(pdir, [])
        try:
            sb.run(entry, "f")
        except NameError:
            return
        raise AssertionError("exec() không bị chặn!")


def t_compile_blocked():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, "def f():\n    return compile('1', 'x', 'eval')\n")
        sb = PluginSandbox(pdir, [])
        try:
            sb.run(entry, "f")
        except NameError:
            return
        raise AssertionError("compile() không bị chặn!")


def t_getattr_blocked():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, "def f():\n    return getattr(1, 'real')\n")
        sb = PluginSandbox(pdir, [])
        try:
            sb.run(entry, "f")
        except NameError:
            return
        raise AssertionError("getattr() không bị chặn!")


# ---------------------------------------------------------------------------
# 4. Filesystem: có quyền -> giam trong plugin_dir
# ---------------------------------------------------------------------------

def t_fs_read_inside_ok():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        (pdir / "data.txt").write_text("secret-data-123", encoding="utf-8")
        entry = write_plugin(pdir, (
            "def read_it(name):\n"
            "    with open(name) as fh:\n"
            "        return fh.read()\n"
        ))
        sb = PluginSandbox(pdir, ["filesystem"])
        assert sb.run(entry, "read_it", "data.txt") == "secret-data-123"


def t_fs_write_inside_ok():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, (
            "def write_it():\n"
            "    with open('created.txt', 'w') as fh:\n"
            "        fh.write('ok')\n"
            "    return 'done'\n"
        ))
        sb = PluginSandbox(pdir, ["filesystem"])
        assert sb.run(entry, "write_it") == "done"
        assert (pdir / "created.txt").read_text(encoding="utf-8") == "ok"


def t_fs_escape_dotdot_blocked():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, (
            "def f():\n"
            "    return open('../../../etc/passwd').read()\n"
        ))
        sb = PluginSandbox(pdir, ["filesystem"])
        try:
            sb.run(entry, "f")
        except PermissionError:
            return
        raise AssertionError(".. thoát plugin_dir không bị chặn!")


def t_fs_absolute_outside_blocked():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, (
            "def f():\n"
            "    return open('/etc/hostname').read()\n"
        ))
        sb = PluginSandbox(pdir, ["filesystem"])
        try:
            sb.run(entry, "f")
        except PermissionError:
            return
        raise AssertionError("đường dẫn tuyệt đối ngoài plugin_dir không bị chặn!")


def t_fs_symlink_escape_blocked():
    # Symlink trong plugin_dir trỏ ra ngoài -> resolve() lật tẩy -> chặn
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        (pdir / "link_to_etc").symlink_to("/etc")
        entry = write_plugin(pdir, (
            "def f():\n"
            "    return open('link_to_etc/passwd').read()\n"
        ))
        sb = PluginSandbox(pdir, ["filesystem"])
        try:
            sb.run(entry, "f")
        except PermissionError:
            return
        raise AssertionError("symlink thoát plugin_dir không bị chặn!")


def t_pathlib_nofs_blocked():
    # Không quyền filesystem: pathlib.Path.read_text bị chặn
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, (
            "from pathlib import Path\n"
            "def f():\n"
            "    return Path('/etc/hostname').read_text()\n"
        ))
        sb = PluginSandbox(pdir, [])
        try:
            sb.run(entry, "f")
        except PermissionError:
            return
        raise AssertionError("pathlib đọc file lọt khi không có quyền filesystem!")


# ---------------------------------------------------------------------------
# 5. Ràng buộc run()
# ---------------------------------------------------------------------------

def t_missing_func():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d)
        entry = write_plugin(pdir, "def real():\n    return 1\n")
        sb = PluginSandbox(pdir, [])
        try:
            sb.run(entry, "khong_co_ham_nay")
        except AttributeError:
            return
        raise AssertionError("gọi hàm không tồn tại không báo lỗi!")


def t_entry_outside_dir():
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        pdir = Path(d1)
        entry = write_plugin(Path(d2), "def f():\n    return 1\n")
        sb = PluginSandbox(pdir, [])
        try:
            sb.run(entry, "f")
        except ValueError:
            return
        raise AssertionError("entry_path ngoài plugin_dir không bị chặn!")


TESTS = [
    ("plugin lành: math", t_math_ok),
    ("plugin lành: xử lý string", t_string_ok),
    ("plugin lành: json", t_json_ok),
    ("plugin lành: from-import collections", t_from_import_ok),
    ("plugin lành: urllib.parse", t_urllib_parse_ok),
    ("plugin lành: datetime", t_datetime_ok),
    ("plugin lành: PurePosixPath (không cần quyền fs)", t_purepath_ok),
    ("chặn: import os (os.system)", t_os_blocked),
    ("chặn: import subprocess", t_subprocess_blocked),
    ("chặn: import sys", t_sys_blocked),
    ("chặn: import socket (không quyền network)", t_socket_blocked_no_perm),
    ("chặn: import socket (kể cả có quyền network)", t_socket_blocked_even_with_network_perm),
    ("chặn: import tương đối", t_relative_import_blocked),
    ("chặn: __import__('os') trực tiếp", t_dunder_import_os_blocked),
    ("chặn: open() khi không có quyền filesystem", t_open_blocked_no_perm),
    ("chặn: eval()", t_eval_blocked),
    ("chặn: exec()", t_exec_blocked),
    ("chặn: compile()", t_compile_blocked),
    ("chặn: getattr()", t_getattr_blocked),
    ("fs: đọc file trong plugin_dir", t_fs_read_inside_ok),
    ("fs: ghi file trong plugin_dir", t_fs_write_inside_ok),
    ("fs: chặn .. thoát plugin_dir", t_fs_escape_dotdot_blocked),
    ("fs: chặn đường dẫn tuyệt đối ngoài plugin_dir", t_fs_absolute_outside_blocked),
    ("fs: chặn symlink trỏ ra ngoài", t_fs_symlink_escape_blocked),
    ("fs: chặn pathlib.Path.read_text khi không có quyền", t_pathlib_nofs_blocked),
    ("run: hàm không tồn tại -> AttributeError", t_missing_func),
    ("run: entry ngoài plugin_dir -> ValueError", t_entry_outside_dir),
]

if __name__ == "__main__":
    for name, fn in TESTS:
        check(name, fn)
    print(f"\nKẾT QUẢ: {PASSED} PASS, {FAILED} FAIL trên tổng {PASSED + FAILED} assertions")
    if FAILURES:
        print("Các test FAIL:", FAILURES)
    sys.exit(1 if FAILED else 0)
