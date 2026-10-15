"""OCR tool: read text from images via the local Tesseract binary.

Free, offline, no API key. Requires `tesseract` on PATH
(apt: tesseract-ocr + tesseract-ocr-vie for Vietnamese).

Every failure returns {"error": ...} -- never raises.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import urllib.request
from typing import Any, Dict, Optional

SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
OCR_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 15

# Magic-byte signatures for common image formats.
_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"RIFF", "webp"),      # RIFF....WEBP -- checked loosely
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
]


def _tesseract_bin() -> Optional[str]:
    return shutil.which("tesseract")


def _looks_like_image(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext in SUPPORTED_EXTS:
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return False
    for sig, _ in _MAGIC:
        if head.startswith(sig):
            return True
    return False


def _run_tesseract(image_path: str, lang: str) -> Dict[str, Any]:
    """Run tesseract on a local file. Assumes the file was validated."""
    binary = _tesseract_bin()
    if not binary:
        return {"error": "tesseract binary not found on PATH: "
                         "install tesseract-ocr to use OCR"}
    if not (lang or "").strip():
        return {"error": "lang must be a non-empty string, e.g. 'vie+eng'"}
    try:
        proc = subprocess.run(
            [binary, image_path, "stdout", "-l", lang.strip(),
             "--psm", "6"],
            capture_output=True, text=True, timeout=OCR_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"error": "tesseract timed out after %ds" % OCR_TIMEOUT}
    except OSError as e:
        return {"error": "could not run tesseract: %s" % (e,)}
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        # Tesseract reports unknown languages on stderr with non-zero exit.
        return {"error": "tesseract failed: %s" % (err or "exit %d"
                                                   % proc.returncode)}
    text = (proc.stdout or "").strip()
    return {"text": text, "lang": lang.strip(), "char_count": len(text)}


def ocr_image(image_path: str, lang: str = "vie+eng") -> Dict[str, Any]:
    """Extract text from a local image file.

    Returns {"text", "lang", "char_count"} or {"error"}.
    """
    path = (image_path or "").strip()
    if not path:
        return {"error": "image_path is required"}
    if not os.path.isfile(path):
        return {"error": "file not found: %s" % path}
    if not _looks_like_image(path):
        return {"error": "not a supported image: %s "
                         "(png/jpg/webp/bmp/tiff)" % path}
    return _run_tesseract(path, lang)


def ocr_image_url(url: str, lang: str = "vie+eng") -> Dict[str, Any]:
    """Download an image (max 10MB) and OCR it. Temp file is removed after."""
    url = (url or "").strip()
    if not url:
        return {"error": "url is required"}
    if not url.lower().startswith(("http://", "https://")):
        return {"error": "url must start with http:// or https://"}
    tmp = None
    try:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "BoomOCR/1.0"})
            with urllib.request.urlopen(req,
                                        timeout=DOWNLOAD_TIMEOUT) as resp:
                chunks = []
                total = 0
                while True:
                    data = resp.read(65536)
                    if not data:
                        break
                    total += len(data)
                    if total > MAX_DOWNLOAD_BYTES:
                        return {"error": "image larger than 10MB, refused"}
                    chunks.append(data)
                body = b"".join(chunks)
        except Exception as e:
            return {"error": "download failed: %s" % (e,)}
        suffix = os.path.splitext(url.split("?")[0])[1].lower() or ".png"
        if suffix not in SUPPORTED_EXTS:
            suffix = ".png"
        fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="boom_ocr_")
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        return ocr_image(tmp, lang)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _schema(props: Dict[str, Any], required: list) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


def build_tools(env_or_root_dir=None) -> Dict[str, Dict[str, Any]]:
    """2 tools: ocr_image, ocr_image_url. Env is accepted for convention
    but not needed -- OCR is stateless."""
    return {
        "ocr_image": {
            "name": "ocr_image",
            "func": ocr_image,
            "description": ("Extract text from a local image file "
                            "(png/jpg/webp/bmp/tiff) using local Tesseract "
                            "OCR. Returns {'text', 'lang', 'char_count'}."),
            "schema": _schema(
                {
                    "image_path": {"type": "string",
                                   "description": "Local path to the image."},
                    "lang": {"type": "string",
                             "description": "Tesseract languages, e.g. "
                                            "'vie+eng'. Default 'vie+eng'."},
                },
                ["image_path"],
            ),
        },
        "ocr_image_url": {
            "name": "ocr_image_url",
            "func": ocr_image_url,
            "description": ("Download an image (max 10MB) and extract its "
                            "text with local Tesseract OCR."),
            "schema": _schema(
                {
                    "url": {"type": "string",
                            "description": "http(s) URL of the image."},
                    "lang": {"type": "string",
                             "description": "Tesseract languages, e.g. "
                                            "'vie+eng'. Default 'vie+eng'."},
                },
                ["url"],
            ),
        },
    }
