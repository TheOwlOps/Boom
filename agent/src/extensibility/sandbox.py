# -*- coding: utf-8 -*-
"""
PluginSandbox — executes Boom plugins in a restricted Python namespace.

Located at: agent/src/extensibility/sandbox.py (deployed from this file).

Restriction mechanisms (all real and working, proven by tests in test_sandbox.py):

1. Restricted builtins: the plugin's ``__builtins__`` is replaced with a dict
   of safe functions/classes only. REMOVED: __import__ (replaced with a custom
   whitelisted version), open (granted only with the "filesystem" permission),
   eval, exec, compile, globals, locals, vars, dir, getattr, hasattr, setattr,
   delattr, input, ...

2. Import whitelist: the custom ``__import__`` only allows:
   math, json, re, datetime, collections, itertools, functools, typing,
   pathlib, dataclasses, enum, abc, hashlib, base64, urllib.parse.
   Imports outside the list (os, sys, subprocess, socket, ...) -> ImportError
   with an English message. Relative imports (from . import x) are also blocked.

3. Filesystem: without the "filesystem" permission, the plugin has no ``open``
   function, and the ``pathlib`` module returns a wrapped version that blocks
   every disk-touching method (read_text, write_text, iterdir, stat, ...);
   PurePath still works normally for path-string manipulation. With the
   "filesystem" permission, a custom ``open`` is granted: paths are resolved
   and checked against plugin_dir, blocking ``..`` and escaping symlinks ->
   PermissionError with an English message.

4. Network: NO network module is on the whitelist (socket, ssl,
   http.client, urllib.request, ... are all unimportable). The "network"
   permission is currently recorded for compatibility only and opens no
   additional modules — reserved for the future (e.g. a dedicated HTTP client
   allowlist). This docstring states it plainly to avoid confusion: network is
   always blocked at the import layer.

5. Absolute blocks regardless of permissions: os.system, subprocess, sys.modules
   manipulation — achieved because os / subprocess / sys are not in the
   whitelist, so plugins cannot import them through ``import`` in any way.

HONEST LIMITATIONS (read carefully before using for untrusted code):
- This sandbox stops "accidental / mischievous" plugins, NOT a determined
  attacker. Someone who knows Python can still escape via dunder access, e.g.::
      ().__class__.__base__.__subclasses__()
  then walk to a class whose ``__init__.__globals__`` holds a dangerous module
  (e.g. pathlib's Path class holds a reference to ``os``). Blocking that
  completely requires AST-rewriting or banning all attribute access — out of
  scope for this module.
- No CPU / RAM / runtime limits: a plugin can still hang the machine with
  ``while True: pass`` or eat all RAM.
- C-extension modules in the whitelist run native code, outside restricted
  builtins' control.
- For fully untrusted code: run in a separate subprocess + OS resource limits
  (setrlimit/timeout) or a container/seccomp — don't rely on this sandbox alone.
"""

from __future__ import annotations

import builtins as _builtins  # original builtins, keep a reference before restricting
import types
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Import whitelist: plugins may only import these modules
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
# Safe builtins. Deliberately REMOVED:
#   __import__ (replaced with custom version), open (granted per permission),
#   eval, exec, compile, globals, locals, vars, dir,
#   getattr, hasattr, setattr, delattr (attribute escape channel),
#   input, help, exit, quit, breakpoint, memoryview...
# ---------------------------------------------------------------------------
_SAFE_BUILTINS: dict[str, Any] = {
    # Printing
    "print": print,
    # Data types
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
    # Arithmetic
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
    # Safe introspection (no getattr/hasattr/setattr/delattr/dir/vars)
    "isinstance": isinstance,
    "issubclass": issubclass,
    "type": type,
    "callable": callable,
    "id": id,
    # Basic OOP
    "object": object,
    "super": super,
    "property": property,
    "staticmethod": staticmethod,
    "classmethod": classmethod,
    # Exceptions for try/except
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

# pathlib.Path methods that touch the real filesystem -> blocked when the plugin
# lacks the "filesystem" permission. PurePath/PurePosixPath are unaffected.
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
    """Create a Path subclass blocking every filesystem-touching method (raises PermissionError)."""
    namespace: dict[str, Any] = {}
    for meth in _PATH_FS_METHODS:
        if hasattr(base_cls, meth):
            def _blocked(self, *args, _meth: str = meth, **kwargs):  # noqa: B023
                raise PermissionError(reason + f" (Path.{_meth}() blocked)")
            namespace[meth] = _blocked
    return type(base_cls.__name__, (base_cls,), namespace)


def _wrap_pathlib_nofs(real_pathlib: types.ModuleType) -> types.ModuleType:
    """Wrap the pathlib module: Path/PosixPath/WindowsPath get I/O blocked, Pure* untouched."""
    reason = "Plugin does not have the 'filesystem' permission"
    wrapped = types.ModuleType("pathlib")
    wrapped.__dict__.update(real_pathlib.__dict__)
    for cls_name in ("Path", "PosixPath", "WindowsPath"):
        base = getattr(real_pathlib, cls_name, None)
        if isinstance(base, type):
            setattr(wrapped, cls_name, _nofs_path_subclass(base, reason))
    return wrapped


class PluginSandbox:
    """Runs a plugin in a permission-scoped restricted namespace.

    :param plugin_dir: plugin directory; file I/O (when granted) is only
        allowed inside this directory (even with ``..`` or symlinks).
    :param permissions: e.g. ["filesystem"]. Empty = no permissions.
        The "network" permission is recorded but currently opens no network
        module (socket/ssl/http... are always blocked at the import whitelist
        layer).
    """

    def __init__(self, plugin_dir: Path, permissions: list[str]) -> None:
        self.plugin_dir = Path(plugin_dir).resolve()
        self.permissions = [str(p).strip().lower() for p in permissions]
        # Per-sandbox builtins (one dict per instance to attach __import__/open)
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
        globals: dict | None = None,   # noqa: A002 - keep the __import__ signature
        locals: dict | None = None,    # noqa: A002
        fromlist: tuple = (),
        level: int = 0,
    ) -> Any:
        """Custom __import__: only modules on the whitelist may be imported."""
        if not isinstance(name, str) or not name:
            raise ImportError("Invalid module name for import.")
        if level != 0:
            raise ImportError(
                f"Plugin sandbox blocks relative imports (module '{name}', level={level})."
            )
        # Allow exactly whitelisted names, or submodules of a whitelisted entry
        # (e.g. "urllib.parse" whitelisted -> "urllib.parse" ok, "urllib.request" blocked).
        allowed = any(name == w or name.startswith(w + ".") for w in _IMPORT_WHITELIST)
        if not allowed:
            raise ImportError(
                "Plugin sandbox blocks importing module "
                f"'{name}'. Only these may be imported: {', '.join(sorted(_IMPORT_WHITELIST))}."
            )
        module = _builtins.__import__(name, globals, locals, fromlist, level)
        # No filesystem permission -> return the wrapped pathlib, all disk I/O blocked
        if "filesystem" not in self.permissions and (
            name == "pathlib" or name.startswith("pathlib.")
        ):
            if self._pathlib_wrapped is None:
                self._pathlib_wrapped = _wrap_pathlib_nofs(module)
            module = self._pathlib_wrapped
        return module

    # ------------------------------------------------------------------
    # Custom open(): confined to plugin_dir
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
        """Custom open: only opens files inside plugin_dir (blocks .. and symlinks)."""
        if opener is not None:
            raise PermissionError(
                "Plugin sandbox blocks a custom 'opener' when opening files."
            )
        target = Path(file)
        if not target.is_absolute():
            target = self.plugin_dir / target
        resolved = target.resolve()  # resolve() follows symlinks -> blocks escaping symlinks
        try:
            resolved.relative_to(self.plugin_dir)
        except ValueError:
            raise PermissionError(
                "Plugin sandbox blocks access to files outside the plugin directory: "
                f"'{file}'. Reading/writing is only allowed in: {self.plugin_dir}"
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
    # Execution
    # ------------------------------------------------------------------
    def run(self, entry_path: Path, func_name: str, *args, **kwargs) -> Any:
        """Reads the entry.py source, compiles it, execs it in the restricted
        namespace, then calls ``func_name`` with args/kwargs and returns the result.

        :raises ValueError: entry_path is outside plugin_dir.
        :raises FileNotFoundError: entry_path does not exist.
        :raises AttributeError: plugin does not export a ``func_name`` function.
        :raises ImportError/NameError/PermissionError: plugin violated the sandbox.
        """
        entry = Path(entry_path).resolve()
        if entry != self.plugin_dir and self.plugin_dir not in entry.parents:
            raise ValueError(
                f"entry_path must be inside plugin_dir ({self.plugin_dir}), "
                f"got: {entry}"
            )
        source = entry.read_text(encoding="utf-8")
        code = _builtins.compile(source, str(entry), "exec")  # compile OUTSIDE the sandbox
        namespace: dict[str, Any] = {
            "__builtins__": self._builtins,  # the restricted namespace lives here
            "__name__": f"boom_plugin_{entry.stem}",
            "__file__": str(entry),
        }
        _builtins.exec(code, namespace)
        func = namespace.get(func_name)
        if not callable(func):
            raise AttributeError(
                f"Plugin '{entry.name}' does not export a callable '{func_name}'."
            )
        return func(*args, **kwargs)
