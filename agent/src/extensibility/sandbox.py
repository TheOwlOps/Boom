# -*- coding: utf-8 -*-
"""
PluginSandbox — thực thi plugin Boom trong namespace Python bị giới hạn.

Đặt tại: agent/src/extensibility/sandbox.py (deploy từ file này).

Cơ chế giới hạn (đều hoạt động thật, có test chứng minh trong test_sandbox.py):

1. Restricted builtins: ``__builtins__`` của plugin bị thay bằng dict chỉ chứa
   các hàm/lớp an toàn. LOẠI BỎ: __import__ (thay bằng bản custom có whitelist),
   open (chỉ cấp khi có quyền "filesystem"), eval, exec, compile, globals,
   locals, vars, dir, getattr, hasattr, setattr, delattr, input, ...

2. Import whitelist: hàm ``__import__`` custom chỉ cho phép:
   math, json, re, datetime, collections, itertools, functools, typing,
   pathlib, dataclasses, enum, abc, hashlib, base64, urllib.parse.
   Import ngoài danh sách (os, sys, subprocess, socket, ...) -> ImportError
   với message tiếng Việt. Import tương đối (from . import x) cũng bị chặn.

3. Filesystem: không có quyền "filesystem" -> plugin không có hàm ``open``,
   và module ``pathlib`` trả về bản bọc (wrapper) chặn mọi method chạm tới
   ổ đĩa (read_text, write_text, iterdir, stat, ...); PurePath vẫn dùng
   bình thường để thao tác chuỗi đường dẫn. Có quyền "filesystem" -> cấp hàm
   ``open`` custom: resolve() đường dẫn rồi đối chiếu với plugin_dir, chặn
   ``..`` và symlink thoát ra ngoài -> PermissionError tiếng Việt.

4. Network: KHÔNG module network nào nằm trong whitelist (socket, ssl,
   http.client, urllib.request, ... đều không import được). Quyền "network"
   hiện chỉ được ghi nhận để tương thích, chưa mở thêm module nào — dành cho
   tương lai (ví dụ HTTP client allowlist riêng). Docstring này ghi rõ để
   khỏi hiểu lầm: network luôn bị chặn ở tầng import.

5. Chặn tuyệt đối bất kể permissions: os.system, subprocess, thao tác
   sys.modules — đạt được vì os / subprocess / sys không có trong whitelist
   nên plugin không thể import chúng bằng bất kỳ cách nào qua ``import``.

GIỚI HẠN TRUNG THỰC (đọc kỹ trước khi dùng cho code không tin cậy):
- Sandbox này chặn được plugin "vô tình / nghịch ngợm", KHÔNG phải security
  boundary chống attacker có chủ đích. Kẻ tấn công biết Python vẫn có thể
  thoát qua truy cập dunder, ví dụ::
      ().__class__.__base__.__subclasses__()
  rồi lần tới class có ``__init__.__globals__`` chứa module nguy hiểm
  (vd. lớp Path của pathlib giữ tham chiếu tới ``os``). Chặn triệt để kiểu
  này cần AST-rewriting hoặc cấm toàn bộ attribute access — ngoài phạm vi
  module này.
- Không giới hạn CPU / RAM / thời gian chạy: plugin vẫn có thể treo máy
  bằng ``while True: pass`` hoặc ăn hết RAM.
- Module C-extension trong whitelist chạy code native, nằm ngoài tầm kiểm
  soát của restricted builtins.
- Với code hoàn toàn không tin cậy: chạy trong subprocess riêng + giới hạn
  tài nguyên OS (setrlimit/timeout) hoặc container/seccomp, đừng chỉ dựa
  vào sandbox này.
"""

from __future__ import annotations

import builtins as _builtins  # builtins gốc, giữ tham chiếu trước khi giới hạn
import types
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Whitelist import: chỉ các module này được plugin import
# ---------------------------------------------------------------------------
_IMPORT_WHITELIST = frozenset({
    "math",
    "json",
    "re",
    "datetime",
    "collections",
    "itertools",
    "functools",
    "typing",
    "pathlib",
    "dataclasses",
    "enum",
    "abc",
    "hashlib",
    "base64",
    "urllib.parse",
})

# ---------------------------------------------------------------------------
# Builtins an toàn. Cố tình LOẠI BỎ:
#   __import__ (thay bằng bản custom), open (cấp theo quyền),
#   eval, exec, compile, globals, locals, vars, dir,
#   getattr, hasattr, setattr, delattr (kênh escape qua attribute),
#   input, help, exit, quit, breakpoint, memoryview...
# ---------------------------------------------------------------------------
_SAFE_BUILTINS: dict[str, Any] = {
    # In ấn
    "print": print,
    # Kiểu dữ liệu
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    "frozenset": frozenset,
    "bytes": bytes,
    "bytearray": bytearray,
    # Số học
    "abs": abs,
    "round": round,
    "pow": pow,
    "divmod": divmod,
    "min": min,
    "max": max,
    "sum": sum,
    # Sequence / iterable
    "len": len,
    "range": range,
    "enumerate": enumerate,
    "zip": zip,
    "sorted": sorted,
    "reversed": reversed,
    "all": all,
    "any": any,
    "map": map,
    "filter": filter,
    "iter": iter,
    "next": next,
    "slice": slice,
    "chr": chr,
    "ord": ord,
    "hex": hex,
    "oct": oct,
    "bin": bin,
    "repr": repr,
    "format": format,
    "hash": hash,
    # Introspection an toàn (không có getattr/hasattr/setattr/delattr/dir/vars)
    "isinstance": isinstance,
    "issubclass": issubclass,
    "type": type,
    "callable": callable,
    "id": id,
    # OOP cơ bản
    "object": object,
    "super": super,
    "property": property,
    "staticmethod": staticmethod,
    "classmethod": classmethod,
    # Exception cho try/except
    "Exception": Exception,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "KeyError": KeyError,
    "IndexError": IndexError,
    "AttributeError": AttributeError,
    "ImportError": ImportError,
    "RuntimeError": RuntimeError,
    "StopIteration": StopIteration,
    "OSError": OSError,
    "PermissionError": PermissionError,
    "ZeroDivisionError": ZeroDivisionError,
    "NotImplementedError": NotImplementedError,
    "LookupError": LookupError,
}

# Các method của pathlib.Path chạm tới filesystem thật -> chặn khi plugin
# không có quyền "filesystem". PurePath/PurePosixPath không nằm trong diện này.
_PATH_FS_METHODS = frozenset({
    "open", "read_text", "read_bytes", "write_text", "write_bytes",
    "mkdir", "unlink", "rmdir", "rename", "replace",
    "symlink_to", "hardlink_to", "link_to", "touch",
    "chmod", "lchmod", "stat", "lstat",
    "exists", "is_file", "is_dir", "is_symlink", "is_mount",
    "is_junction", "is_fifo", "is_socket", "is_block_device", "is_char_device",
    "iterdir", "glob", "rglob", "walk",
    "owner", "group", "expanduser", "resolve", "absolute", "samefile",
    "cwd", "home",
})


def _nofs_path_subclass(base_cls: type, reason: str) -> type:
    """Tạo subclass của Path chặn mọi method chạm filesystem (raise PermissionError)."""
    namespace: dict[str, Any] = {}
    for meth in _PATH_FS_METHODS:
        if hasattr(base_cls, meth):
            def _blocked(self, *args, _meth: str = meth, **kwargs):  # noqa: B023
                raise PermissionError(reason + f" (Path.{_meth}() bị chặn)")
            namespace[meth] = _blocked
    return type(base_cls.__name__, (base_cls,), namespace)


def _wrap_pathlib_nofs(real_pathlib: types.ModuleType) -> types.ModuleType:
    """Bọc module pathlib: Path/PosixPath/WindowsPath bị chặn I/O, Pure* giữ nguyên."""
    reason = "Plugin không có quyền 'filesystem'"
    wrapped = types.ModuleType("pathlib")
    wrapped.__dict__.update(real_pathlib.__dict__)
    for cls_name in ("Path", "PosixPath", "WindowsPath"):
        base = getattr(real_pathlib, cls_name, None)
        if isinstance(base, type):
            setattr(wrapped, cls_name, _nofs_path_subclass(base, reason))
    return wrapped


class PluginSandbox:
    """Chạy plugin trong namespace bị giới hạn theo permissions.

    :param plugin_dir: thư mục plugin; file I/O (khi được cấp quyền) chỉ
        được phép trong thư mục này (kể cả khi dùng ``..`` hay symlink).
    :param permissions: ví dụ ["filesystem"]. Rỗng = không quyền gì.
        Quyền "network" được ghi nhận nhưng hiện chưa mở module network nào
        (socket/ssl/http... luôn bị chặn ở tầng import whitelist).
    """

    def __init__(self, plugin_dir: Path, permissions: list[str]) -> None:
        self.plugin_dir = Path(plugin_dir).resolve()
        self.permissions = [str(p).strip().lower() for p in permissions]
        # Builtins riêng cho sandbox này (mỗi instance một dict để gắn __import__/open)
        self._builtins: dict[str, Any] = dict(_SAFE_BUILTINS)
        self._builtins["__import__"] = self._sandbox_import
        if "filesystem" in self.permissions:
            self._builtins["open"] = self._sandbox_open
        self._pathlib_wrapped: types.ModuleType | None = None

    # ------------------------------------------------------------------
    # Import whitelist
    # ------------------------------------------------------------------
    def _sandbox_import(
        self,
        name: str,
        globals: dict | None = None,   # noqa: A002 - giữ đúng chữ ký __import__
        locals: dict | None = None,    # noqa: A002
        fromlist: tuple = (),
        level: int = 0,
    ) -> Any:
        """Hàm __import__ custom: chỉ cho import module trong whitelist."""
        if not isinstance(name, str) or not name:
            raise ImportError("Tên module import không hợp lệ.")
        if level != 0:
            raise ImportError(
                f"Plugin sandbox chặn import tương đối (module '{name}', level={level})."
            )
        # Cho phép đúng tên trong whitelist, hoặc submodule của mục whitelist
        # (vd. "urllib.parse" trong whitelist -> cho "urllib.parse", chặn "urllib.request").
        allowed = any(name == w or name.startswith(w + ".") for w in _IMPORT_WHITELIST)
        if not allowed:
            raise ImportError(
                "Plugin sandbox chặn import module "
                f"'{name}'. Chỉ được import: {', '.join(sorted(_IMPORT_WHITELIST))}."
            )
        module = _builtins.__import__(name, globals, locals, fromlist, level)
        # Không quyền filesystem -> trả về pathlib đã bọc, chặn mọi I/O đĩa
        if "filesystem" not in self.permissions and (
            name == "pathlib" or name.startswith("pathlib.")
        ):
            if self._pathlib_wrapped is None:
                self._pathlib_wrapped = _wrap_pathlib_nofs(module)
            module = self._pathlib_wrapped
        return module

    # ------------------------------------------------------------------
    # open() custom: giam trong plugin_dir
    # ------------------------------------------------------------------
    def _sandbox_open(
        self,
        file,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
        closefd: bool = True,
        opener=None,
    ):
        """Hàm open custom: chỉ mở file nằm trong plugin_dir (chặn .. và symlink)."""
        if opener is not None:
            raise PermissionError(
                "Plugin sandbox chặn dùng 'opener' tùy biến khi mở file."
            )
        target = Path(file)
        if not target.is_absolute():
            target = self.plugin_dir / target
        resolved = target.resolve()  # resolve() bám theo symlink -> chặn symlink thoát
        try:
            resolved.relative_to(self.plugin_dir)
        except ValueError:
            raise PermissionError(
                "Plugin sandbox chặn truy cập file ngoài thư mục plugin: "
                f"'{file}'. Chỉ được đọc/ghi trong: {self.plugin_dir}"
            )
        return _builtins.open(
            resolved,
            mode=mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
            closefd=closefd,
        )

    # ------------------------------------------------------------------
    # Thực thi
    # ------------------------------------------------------------------
    def run(self, entry_path: Path, func_name: str, *args, **kwargs) -> Any:
        """Đọc source entry.py, compile, exec trong namespace giới hạn,
        rồi gọi hàm ``func_name`` với args/kwargs và trả về kết quả.

        :raises ValueError: entry_path nằm ngoài plugin_dir.
        :raises FileNotFoundError: entry_path không tồn tại.
        :raises AttributeError: plugin không export hàm ``func_name``.
        :raises ImportError/NameError/PermissionError: plugin vi phạm sandbox.
        """
        entry = Path(entry_path).resolve()
        if entry != self.plugin_dir and self.plugin_dir not in entry.parents:
            raise ValueError(
                f"entry_path phải nằm trong plugin_dir ({self.plugin_dir}), "
                f"nhận được: {entry}"
            )
        source = entry.read_text(encoding="utf-8")
        code = _builtins.compile(source, str(entry), "exec")  # compile NGOÀI sandbox
        namespace: dict[str, Any] = {
            "__builtins__": self._builtins,  # namespace bị giới hạn nằm ở đây
            "__name__": f"boom_plugin_{entry.stem}",
            "__file__": str(entry),
        }
        _builtins.exec(code, namespace)
        func = namespace.get(func_name)
        if not callable(func):
            raise AttributeError(
                f"Plugin '{entry.name}' không export hàm callable '{func_name}'."
            )
        return func(*args, **kwargs)
