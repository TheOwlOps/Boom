"""PyInstaller runtime hook for boom-backend.

Windowed (console=False) apps have no usable stdout/stderr on some
platforms (Windows); gateway/api.py writes to both (startup banner,
BaseHTTPRequestHandler.log_message). Point them at devnull instead of
letting the server die on the first print().
"""

import os
import sys

for _name in ("stdout", "stderr"):
    _stream = getattr(sys, _name, None)
    try:
        if _stream is None or getattr(_stream, "closed", False):
            setattr(sys, _name, open(os.devnull, "w"))
    except Exception:
        pass
