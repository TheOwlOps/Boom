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
    """3 tools: ocr_image, ocr_image_url, ocr_image_queued. Env is accepted
    for convention but not needed -- OCR is stateless."""
    tools = {
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
        "ocr_image_queued": {
            "name": "ocr_image_queued",
            "func": ocr_image_queued,
            "description": ("OCR via a bounded worker queue: safe under heavy "
                            "concurrent load (limits simultaneous tesseract "
                            "processes, caches repeat images). Returns "
                            "{'text', 'lang', 'char_count'}."),
            "schema": _schema(
                {
                    "image_path": {"type": "string",
                                   "description": "Local path to the image."},
                    "lang": {"type": "string",
                             "description": "Tesseract languages, e.g. "
                                            "'vie+eng'. Default 'vie+eng'."},
                    "timeout": {"type": "number",
                                "description": "Max seconds to wait. "
                                               "Default 120."},
                },
                ["image_path"],
            ),
        },
    }
    return tools


# ---------------------------------------------------------------------------
# Async OCR queue: bounded concurrency + result cache + backpressure.
#
# Problem: 100 users asking for OCR at once would spawn 100 tesseract
# processes and hang the machine. OCRQueue serializes the work through a
# small worker pool, caches results by image hash, and refuses new work
# with an honest {"error"} when the backlog is full instead of hanging.
#
# The original sync functions above (ocr_image / ocr_image_url) are
# untouched for backward compatibility.
# ---------------------------------------------------------------------------

import asyncio
import hashlib
import random
import threading
import time
from collections import OrderedDict

QUEUE_MAX_PENDING = 1000  # backpressure: refuse beyond this many active jobs


def _new_job_id() -> str:
    return "ocr_%d_%04x" % (int(time.time() * 1000), random.getrandbits(16))


class OCRQueue:
    """Bounded async OCR worker pool.

    Single event loop per instance. Create one per loop, or use the
    sync wrapper ocr_image_queued() which manages its own loop.

    - submit(image_path, lang) -> job_id | {"error"}  (sync, fail-fast)
    - await get_result(job_id, timeout) -> {"status", ...}  (async)
    - stats() -> dict  (sync)
    """

    def __init__(self, max_workers: int = 4, cache_size: int = 100,
                 max_pending: int = QUEUE_MAX_PENDING) -> None:
        self._max_workers = max(1, int(max_workers))
        self._cache_size = max(0, int(cache_size))
        self._max_pending = max(1, int(max_pending))
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._queue: Optional[asyncio.Queue] = None
        self._sem: Optional[asyncio.Semaphore] = None
        self._cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._inflight: Dict[str, str] = {}  # sha:lang -> job_id
        self._workers_started = False
        self._submit_lock = threading.Lock()
        self._cache_hits = 0
        self._done_count = 0

    # -- internals ------------------------------------------------------

    def _ensure(self) -> None:
        # asyncio primitives bind lazily (3.10+); safe without a running loop.
        if self._queue is None:
            self._queue = asyncio.Queue()
            self._sem = asyncio.Semaphore(self._max_workers)

    @staticmethod
    def _sha256_file(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    async def _ensure_workers(self) -> None:
        self._ensure()
        if not self._workers_started:
            self._workers_started = True
            self._worker_tasks = []
            for _ in range(self._max_workers):
                self._worker_tasks.append(asyncio.create_task(self._worker_loop()))

    async def aclose(self) -> None:
        """Cancel worker tasks. Call before dropping the queue to avoid
        'Task was destroyed but it is pending' warnings."""
        for t in getattr(self, "_worker_tasks", []):
            t.cancel()
        for t in getattr(self, "_worker_tasks", []):
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._workers_started = False

    async def _worker_loop(self) -> None:
        assert self._queue is not None
        while True:
            job_id = await self._queue.get()
            try:
                await self._process_job(job_id)
            except Exception as e:  # never let a worker die
                job = self._jobs.get(job_id)
                if job is not None and job["status"] not in ("done", "error"):
                    job["status"] = "error"
                    job["result"] = {"error": "ocr worker crashed: %s" % (e,)}
                    with self._submit_lock:
                        self._inflight.pop(job["sha"], None)
            finally:
                self._queue.task_done()

    async def _process_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or job["status"] != "pending":
            return
        job["status"] = "running"
        try:
            assert self._sem is not None
            async with self._sem:
                # _run_tesseract is resolved at call time so tests can patch it.
                result = await asyncio.to_thread(
                    _run_tesseract, job["image_path"], job["lang"])
        except Exception as e:
            result = {"error": "ocr execution failed: %s" % (e,)}
        if not isinstance(result, dict):
            result = {"error": "ocr returned non-dict: %r" % (result,)}
        ok = "error" not in result
        with self._submit_lock:
            self._inflight.pop(job["sha"], None)
            if ok and self._cache_size > 0:
                self._cache[job["sha"]] = dict(result)
                self._cache.move_to_end(job["sha"])
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
        if ok:
            self._done_count += 1
        job["result"] = result
        job["status"] = "done" if ok else "error"

    @staticmethod
    def _format(job: Dict[str, Any]) -> Dict[str, Any]:
        st = job["status"]
        if st == "done":
            r = job.get("result") or {}
            return {"status": "done", "job_id": job["job_id"],
                    "text": r.get("text", ""),
                    "lang": r.get("lang", job["lang"]),
                    "char_count": r.get("char_count", 0)}
        if st == "error":
            r = job.get("result") or {}
            return {"status": "error", "job_id": job["job_id"],
                    "error": r.get("error", "unknown error")}
        return {"status": st, "job_id": job["job_id"]}

    # -- public API -----------------------------------------------------

    def submit(self, image_path: str, lang: str = "vie+eng") -> Any:
        """Validate (fail fast) and enqueue. Returns job_id, or {"error"}.

        Sync-safe: validates and enqueues immediately; workers start when a
        running loop is available (submit from async code, or first
        get_result / ocr_image_queued call).
        """
        path = (image_path or "").strip()
        if not path:
            return {"error": "image_path is required"}
        if not os.path.isfile(path):
            return {"error": "file not found: %s" % path}
        if not _looks_like_image(path):
            return {"error": "not a supported image: %s "
                             "(png/jpg/webp/bmp/tiff)" % path}
        lang = (lang or "").strip()
        if not lang:
            return {"error": "lang must be a non-empty string, e.g. 'vie+eng'"}
        try:
            key = self._sha256_file(path) + ":" + lang
        except OSError as e:
            return {"error": "could not read file: %s" % (e,)}

        with self._submit_lock:
            if key in self._cache:  # instant hit, no worker needed
                self._cache_hits += 1
                self._cache.move_to_end(key)
                job_id = _new_job_id()
                self._jobs[job_id] = {
                    "job_id": job_id, "image_path": path, "lang": lang,
                    "sha": key, "status": "done",
                    "result": dict(self._cache[key]),
                    "created_at": time.time(),
                }
                self._done_count += 1
                return job_id
            if key in self._inflight:  # same image already queued: dedupe
                return self._inflight[key]
            active = sum(1 for j in self._jobs.values()
                         if j["status"] in ("pending", "running"))
            if active >= self._max_pending:
                return {"error": "ocr queue full (%d active jobs), "
                                 "try again later" % active}
            job_id = _new_job_id()
            self._jobs[job_id] = {
                "job_id": job_id, "image_path": path, "lang": lang,
                "sha": key, "status": "pending", "result": None,
                "created_at": time.time(),
            }
            self._inflight[key] = job_id
            self._ensure()
            assert self._queue is not None
            self._queue.put_nowait(job_id)

        # Opportunistically start workers if we're inside a running loop.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            asyncio.create_task(self._ensure_workers())
        return job_id

    async def get_result(self, job_id: str,
                         timeout: float = 30) -> Dict[str, Any]:
        """Wait up to `timeout` seconds for a job. Never raises.

        Returns {"status": "done", "text", ...}, {"status": "error", "error"},
        or the current {"status": "pending"/"running"} on timeout.
        """
        await self._ensure_workers()
        job = self._jobs.get(job_id)
        if job is None:
            return {"status": "error", "job_id": job_id,
                    "error": "unknown job_id: %s" % (job_id,)}
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if job["status"] in ("done", "error"):
                return self._format(job)
            await asyncio.sleep(0.05)
        return self._format(job)

    def stats(self) -> Dict[str, Any]:
        with self._submit_lock:
            queued = sum(1 for j in self._jobs.values()
                         if j["status"] == "pending")
            running = sum(1 for j in self._jobs.values()
                          if j["status"] == "running")
            return {
                "queued": queued,
                "running": running,
                "done": self._done_count,
                "total_jobs": len(self._jobs),
                "cache_hits": self._cache_hits,
                "cache_size": len(self._cache),
                "max_workers": self._max_workers,
                "max_pending": self._max_pending,
            }


def ocr_image_queued(image_path: str, lang: str = "vie+eng",
                     timeout: float = 120, max_workers: int = 4) -> Dict[str, Any]:
    """Sync convenience: submit to a private queue and wait for the result.

    Returns {"text", "lang", "char_count"} or {"error"}. Never raises.
    Safe to call with or without a running event loop.
    """
    async def _run() -> Dict[str, Any]:
        q = OCRQueue(max_workers=max_workers)
        try:
            rid = q.submit(image_path, lang)
            if isinstance(rid, dict):  # validation / backpressure error
                return rid
            res = await q.get_result(rid, timeout=timeout)
            if res.get("status") == "done":
                return {"text": res.get("text", ""),
                        "lang": res.get("lang", (lang or "vie+eng").strip()),
                        "char_count": res.get("char_count", 0)}
            if res.get("status") == "error":
                return {"error": res.get("error", "unknown ocr error")}
            return {"error": "ocr timed out after %ss (job %s)" %
                             (timeout, res.get("status"))}
        finally:
            await q.aclose()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(_run())
        except Exception as e:
            return {"error": "queued ocr failed: %s" % (e,)}
    # Already inside a loop: run in a helper thread with a fresh loop
    # so we never deadlock the caller's loop.
    out: Dict[str, Any] = {}

    def _target() -> None:
        try:
            out["r"] = asyncio.run(_run())
        except Exception as e:  # pragma: no cover - defensive
            out["r"] = {"error": "queued ocr failed: %s" % (e,)}

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout + 10)
    return out.get("r", {"error": "queued ocr thread did not finish"})
