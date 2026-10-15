"""Hệ thống plugin thống nhất của Boom.

Hợp nhất 2 hệ thống rời rạc trước đây:

- **Skills** (``skill_loader.py`` / ``skills_hub.py``): thư mục chứa ``SKILL.md``
  — markdown hướng dẫn cho LLM đọc, lazy-load, không chạy code.
- **Plugins** (``plugin_registry.py``): thư mục chứa ``plugin.json`` + ``entry.py``
  — code Python thật, load bằng importlib, full quyền, không lifecycle.

Kiến trúc mới: mỗi plugin gồm ``plugin.json`` (manifest) + ``entry.py`` (code)
+ ``SKILL.md`` (tùy chọn, hướng dẫn cho LLM), quản lý bởi ``PluginManager``
với vòng đời đầy đủ (install → enable → disable → uninstall → update),
đọc manifest qua ``PluginManifest``, và chạy trong sandbox theo quyền qua
``PluginSandbox``.

Các import bên dưới dùng ``try/except ImportError`` để package vẫn import
được ngay cả khi một trong các module thành phần (do worker khác làm song
song) chưa tồn tại. Khi module xuất hiện, export tương ứng tự có giá trị.
"""

__all__ = ["PluginManager", "PluginManifest", "PluginSandbox"]

try:
    from .lifecycle import PluginManager
except ImportError:  # module do worker khác viết, có thể chưa có
    PluginManager = None  # type: ignore[assignment]

try:
    from .manifest import PluginManifest
except ImportError:  # module do worker khác viết, có thể chưa có
    PluginManifest = None  # type: ignore[assignment]

try:
    from .sandbox import PluginSandbox
except ImportError:  # module do worker khác viết, có thể chưa có
    PluginSandbox = None  # type: ignore[assignment]


def available():
    """Trả về dict tên export -> True/False cho biết module nào đã sẵn sàng."""
    return {
        name: globals().get(name) is not None
        for name in __all__
    }
