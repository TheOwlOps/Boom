"""LazyLoader — lazy import các module nặng, chỉ load khi thật sự dùng.

Bài toán trong Boom:
- ``agent/src/environment.py`` import hàng loạt tool module ở đầu file,
  kể cả module nặng (ocr/tesseract, openpyxl, pptx...) mà session đó
  có thể không bao giờ gọi tới. Startup chậm, tốn RAM.
- ``skill_loader.py`` import ``yaml`` ngay ở module-level dù nhiều
  skill không có frontmatter YAML.

LazyLoader bọc tên module trong một proxy: ``importlib.import_module``
chỉ chạy ở lần truy cập thuộc tính/phương thức ĐẦU TIÊN, sau đó mọi
lần dùng tiếp đều nhanh như import thường (module đã nằm trong
``sys.modules``).
"""

from __future__ import annotations

import importlib
import sys
import threading
from types import ModuleType
from typing import Any, Optional


class LazyLoader(ModuleType):
    """Proxy module: import thật bị hoãn tới lần dùng đầu tiên.

    Cách dùng::

        tesseract = LazyLoader("pytesseract")   # chưa import gì cả
        ...
        text = tesseract.image_to_string(img)   # lúc này mới import thật

    Sau lần đầu, ``is_loaded`` = True và mọi truy cập sau là trực tiếp.
    """

    def __init__(self, module_name: str, *, package: Optional[str] = None):
        # ModuleType.__init__ cần name; đặt __dict__ đặc biệt cho proxy.
        super().__init__(module_name)
        self.__dict__["_lazy_module_name"] = module_name
        self.__dict__["_lazy_package"] = package
        self.__dict__["_lazy_lock"] = threading.RLock()
        self.__dict__["_lazy_real_module"] = None  # type: Optional[ModuleType]

    # ------------------------------------------------------------------
    # Trạng thái
    # ------------------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        """True nếu module thật đã được import (nằm trong sys.modules)."""
        return self.__dict__["_lazy_real_module"] is not None

    def load(self) -> ModuleType:
        """Ép import ngay (thay vì đợi lần dùng đầu). Trả về module thật."""
        real = self.__dict__["_lazy_real_module"]
        if real is None:
            with self.__dict__["_lazy_lock"]:
                real = self.__dict__["_lazy_real_module"]
                if real is None:  # Double-check để 2 thread không import 2 lần.
                    real = importlib.import_module(
                        self.__dict__["_lazy_module_name"],
                        package=self.__dict__["_lazy_package"],
                    )
                    self.__dict__["_lazy_real_module"] = real
        return real

    # ------------------------------------------------------------------
    # Proxy: mọi truy cập thuộc tính đều đi qua import thật
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        # Chỉ chạy khi thuộc tính KHÔNG có trong __dict__ của proxy,
        # tức là lần đầu dùng -> import thật rồi ủy thác.
        if name.startswith("_lazy_"):
            raise AttributeError(name)
        return getattr(self.load(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        # Gán thuộc tính lên module thật (sau khi đảm bảo đã load).
        if name.startswith("_lazy_"):
            self.__dict__[name] = value
        else:
            setattr(self.load(), name, value)

    def __dir__(self):
        # dir() cũng liệt kê được thuộc tính của module thật.
        return sorted(set(super().__dir__()) | set(dir(self.load())))

    def __repr__(self) -> str:  # pragma: no cover - chỉ để debug đẹp
        state = "loaded" if self.is_loaded else "not loaded yet"
        return f"<LazyLoader {self.__dict__['_lazy_module_name']!r} ({state})>"


def lazy_import(module_name: str, *, package: Optional[str] = None) -> LazyLoader:
    """Tạo proxy lazy cho ``module_name``. Chưa hề import gì cho tới khi dùng.

    Ví dụ thực tế cho Boom (trong environment.py)::

        _yaml = lazy_import("yaml")          # pyyaml chỉ load khi parse frontmatter
        _openpyxl = lazy_import("openpyxl")  # chỉ load khi export xlsx thật
    """
    return LazyLoader(module_name, package=package)
