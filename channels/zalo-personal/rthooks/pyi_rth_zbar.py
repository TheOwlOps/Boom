"""PyInstaller runtime hook: point pyzbar at the bundled libzbar.

pyzbar resolves the native zbar library via ``ctypes.util.find_library``,
which only works when the OS has the zbar system package installed. The
spec file bundles the library file into the executable root; this hook
runs before any user code and rewires ``pyzbar.zbar_library.load`` to use
it. When no bundled library exists the hook is a no-op and the bridge
keeps its graceful 501 path for /qr-lookup-image.
"""

import os
import sys


def _patch():
    if not getattr(sys, "frozen", False):
        return
    meipass = getattr(sys, "_MEIPASS", "")
    if not meipass:
        return
    for name in ("libzbar.so.0", "libzbar.so", "libzbar.dylib"):
        candidate = os.path.join(meipass, name)
        if not os.path.isfile(candidate):
            continue
        try:
            from ctypes import cdll

            lib = cdll.LoadLibrary(candidate)
        except Exception:
            return
        try:
            import pyzbar.zbar_library as _zl

            _zl.load = lambda: (lib, [])  # noqa: E731
        except Exception:
            pass
        return


_patch()
