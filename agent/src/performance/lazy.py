"""LazyLoader — lazily imports heavy modules, only loading them when really used.

The problem in Boom:
- ``agent/src/environment.py`` imports a pile of tool modules at the top,
  including heavy ones (ocr/tesseract, openpyxl, pptx...) that a session
  may never call. Slow startup, wasted RAM.
- ``skill_loader.py`` imports ``yaml`` at module level even though many
  skills have no YAML frontmatter.

LazyLoader wraps a module name in a proxy: ``importlib.import_module`` only
runs on the FIRST attribute/method access, after which every subsequent use
is as fast as a normal import (the module sits in ``sys.modules``).
"""

from __future__ import annotations

import importlib
import sys
import threading
from types import ModuleType
from typing import Any, Optional


class LazyLoader(ModuleType):
    """Proxy module: the real import is deferred until first use.

    Usage::

        tesseract = LazyLoader("pytesseract")   # nothing imported yet
        ...
        text = tesseract.image_to_string(img)   # the real import happens here

    After the first hit, ``is_loaded`` = True and later accesses are direct.
    """

    def __init__(self, module_name: str, *, package: Optional[str] = None):
        # ModuleType.__init__ needs a name; set up a special __dict__ for the proxy.
        super().__init__(module_name)
        self.__dict__["_lazy_module_name"] = module_name
        self.__dict__["_lazy_package"] = package
        self.__dict__["_lazy_lock"] = threading.RLock()
        self.__dict__["_lazy_real_module"] = None  # type: Optional[ModuleType]

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        """True when the real module has been imported (in sys.modules)."""
        return self.__dict__["_lazy_real_module"] is not None

    def load(self) -> ModuleType:
        """Forces the import now (instead of waiting for first use). Returns the real module."""
        real = self.__dict__["_lazy_real_module"]
        if real is None:
            with self.__dict__["_lazy_lock"]:
                real = self.__dict__["_lazy_real_module"]
                if real is None:  # Double-check so 2 threads don't import twice.
                    real = importlib.import_module(
                        self.__dict__["_lazy_module_name"],
                        package=self.__dict__["_lazy_package"],
                    )
                    self.__dict__["_lazy_real_module"] = real
        return real

    # ------------------------------------------------------------------
    # Proxy: every attribute access goes through the real import
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        # Only runs when the attribute is NOT in the proxy's __dict__,
        # i.e. first use -> import the real module, then delegate.
        if name.startswith("_lazy_"):
            raise AttributeError(name)
        return getattr(self.load(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        # Assigns attributes onto the real module (after ensuring it's loaded).
        if name.startswith("_lazy_"):
            self.__dict__[name] = value
        else:
            setattr(self.load(), name, value)

    def __dir__(self):
        # dir() also lists the real module's attributes.
        return sorted(set(super().__dir__()) | set(dir(self.load())))

    def __repr__(self) -> str:  # pragma: no cover - pretty debugging only
        state = "loaded" if self.is_loaded else "not loaded yet"
        return f"<LazyLoader {self.__dict__['_lazy_module_name']!r} ({state})>"


def lazy_import(module_name: str, *, package: Optional[str] = None) -> LazyLoader:
    """Creates a lazy proxy for ``module_name``. Nothing is imported until used.

    Real-world example for Boom (in environment.py)::

        _yaml = lazy_import("yaml")          # pyyaml only loads when parsing frontmatter
        _openpyxl = lazy_import("openpyxl")  # only loads on real xlsx export
    """
    return LazyLoader(module_name, package=package)
