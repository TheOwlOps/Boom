"""VideoProcessor — extracts frames from video, then describes them with OCR.

Frame extraction backend (auto-selected in priority order):
  1. ffmpeg (subprocess) — RECOMMENDED, already on the machine
  2. cv2 (OpenCV VideoCapture) — fallback when no ffmpeg

Both are OPTIONAL dependencies. When both are missing, extract_frames()
raises RuntimeError with a clear error message.

This module copies no code from any reference repo: written from scratch
for Boom.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Error message when no frame extraction backend is available.
ERR_NO_BACKEND = (
    "Cannot extract frames: this machine has neither ffmpeg nor OpenCV. "
    "Install one of the two and try again:\n"
    "  - ffmpeg:        apt install ffmpeg   (recommended)\n"
    "  - OpenCV:        pip install opencv-python-headless\n"
    "Note: don't use apt install python3-opencv if you want a clean environment."
)

FFMPEG_TIMEOUT = 120  # seconds, per ffmpeg call


def _has_cv2() -> bool:
    """Checks whether OpenCV is importable (without noisy imports)."""
    try:
        import importlib.util

        return importlib.util.find_spec("cv2") is not None
    except (ImportError, ValueError):
        return False


def _resolve_default_ocr() -> Optional[Callable[[str], str]]:
    """Auto-finds Boom's ocr_tool and wraps it as function(path) -> text.

    Boom's ocr_image() returns dict {"text": ...} or {"error": ...} and never
    raises — so the wrapper turns every error into empty text.
    """
    for mod in ("agent.src.tools.ocr_tool", "tools.ocr_tool"):
        try:
            module = __import__(mod, fromlist=["ocr_image"])
            ocr_image = getattr(module, "ocr_image")

            def _ocr(path: str, _ocr_image=ocr_image) -> str:
                try:
                    result = _ocr_image(path)
                except Exception:
                    return ""
                if isinstance(result, dict):
                    return str(result.get("text", "") or "")
                return str(result or "")

            return _ocr
        except (ImportError, AttributeError):
            continue
    return None


class VideoProcessor:
    def __init__(
        self,
        ocr_fn: Optional[Callable[[str], str]] = None,
        work_dir: str = "/tmp/boom_frames",
    ) -> None:
        """Args:
            ocr_fn: function(path) -> text. When None, auto-tries Boom's
                tools.ocr_tool. When that's also missing, frames still
                extract fine — only the text will be empty.
            work_dir: root dir for extracted frames.
        """
        self.ocr_fn = ocr_fn if ocr_fn is not None else _resolve_default_ocr()
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._ffmpeg = shutil.which("ffmpeg")
        self._cv2_ok = _has_cv2()

    # ------------------------------------------------------------------
    # Backend availability flags
    # ------------------------------------------------------------------
    @property
    def has_ffmpeg(self) -> bool:
        return self._ffmpeg is not None

    @property
    def has_cv2(self) -> bool:
        return self._cv2_ok

    # ------------------------------------------------------------------
    # Main API
    # ------------------------------------------------------------------
    def extract_frames(
        self,
        video_path: str,
        fps: float = 1.0,
        max_frames: int = 60,
    ) -> List[Dict[str, Any]]:
        """Extracts frames from a video.

        Returns: [{"index": int, "timestamp": float (seconds), "path": str}]
        """
        video = Path(video_path)
        if not video.exists():
            raise FileNotFoundError("Video file not found: %s" % video_path)
        if fps <= 0:
            raise ValueError("fps must be > 0, got %r" % (fps,))
        if max_frames < 1:
            raise ValueError("max_frames must be >= 1, got %r" % (max_frames,))

        # Each video gets its own frames dir (no clobbering across videos).
        digest = hashlib.sha256(str(video.resolve()).encode()).hexdigest()[:12]
        out_dir = self.work_dir / digest
        # Clear this video's old frames so results are always fresh.
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)

        if self._ffmpeg:
            return self._extract_ffmpeg(video, out_dir, fps, max_frames)
        if self._cv2_ok:
            return self._extract_cv2(video, out_dir, fps, max_frames)
        raise RuntimeError(ERR_NO_BACKEND)

    def describe_frames(self, frames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Runs OCR on each frame.

        Returns: [{"index", "timestamp", "text", "path"}]
        OCR failure or no ocr_fn -> text = "" (no crash).
        """
        described: List[Dict[str, Any]] = []
        for f in frames:
            text = ""
            if self.ocr_fn is not None:
                try:
                    text = self.ocr_fn(f["path"]) or ""
                except Exception:
                    text = ""
            described.append(
                {
                    "index": f["index"],
                    "timestamp": f["timestamp"],
                    "text": text,
                    "path": f["path"],
                }
            )
        return described

    # ------------------------------------------------------------------
    # Backend: ffmpeg
    # ------------------------------------------------------------------
    def _extract_ffmpeg(
        self, video: Path, out_dir: Path, fps: float, max_frames: int
    ) -> List[Dict[str, Any]]:
        pattern = str(out_dir / "frame_%05d.jpg")
        cmd = [
            self._ffmpeg,  # type: ignore[list-item]
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vf",
            "fps=%.4f" % fps,  # extract at exactly the requested fps, independent of source fps
            "-frames:v",
            str(max_frames),  # frame cap -> no disk explosions
            "-q:v",
            "4",  # JPEG quality good enough for OCR, small files
            pattern,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "ffmpeg frame extraction exceeded %ds, killed (video: %s)"
                % (FFMPEG_TIMEOUT, video)
            )
        except OSError as e:
            raise RuntimeError("Cannot run ffmpeg: %s" % (e,))
        if proc.returncode != 0:
            raise RuntimeError(
                "ffmpeg frame extraction failed: %s" % ((proc.stderr or "").strip()[:500])
            )

        frames: List[Dict[str, Any]] = []
        for i, p in enumerate(sorted(out_dir.glob("frame_*.jpg"))):
            frames.append(
                {
                    "index": i,
                    # Timestamp inferred from frame order / requested fps (the fps=
                    # filter gives evenly spaced frames, accurate for normal videos).
                    "timestamp": round(i / fps, 3),
                    "path": str(p),
                }
            )
        return frames

    # ------------------------------------------------------------------
    # Backend: OpenCV
    # ------------------------------------------------------------------
    def _extract_cv2(
        self, video: Path, out_dir: Path, fps: float, max_frames: int
    ) -> List[Dict[str, Any]]:
        import cv2  # lazy import: cv2 is optional

        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            raise RuntimeError("OpenCV cannot open video: %s" % (video,))
        try:
            src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            # Step: take 1 frame per every `step` source frames.
            step = max(1, int(round(src_fps / fps))) if src_fps > 0 else 1

            frames: List[Dict[str, Any]] = []
            pos = 0
            taken = 0
            while taken < max_frames:
                ok, img = cap.read()
                if not ok or img is None:
                    break
                if pos % step == 0:
                    path = out_dir / "frame_%05d.jpg" % taken
                    cv2.imwrite(str(path), img)
                    ts = (
                        round(pos / src_fps, 3)
                        if src_fps > 0
                        else round(taken / fps, 3)
                    )
                    frames.append(
                        {"index": taken, "timestamp": ts, "path": str(path)}
                    )
                    taken += 1
                pos += 1
            return frames
        finally:
            cap.release()


def _supports_import(name: str) -> bool:
    """Tiny helper for tests to check importability without crashing."""
    try:
        __import__(name)
        return True
    except ImportError:
        return False


if __name__ == "__main__":  # pragma: no cover
    # Quick run: python3 video.py <file.mp4>
    vp = VideoProcessor()
    print("ffmpeg:", "yes" if vp.has_ffmpeg else "no",
          "| cv2:", "yes" if vp.has_cv2 else "no",
          "| ocr:", "yes" if vp.ocr_fn else "no")
    if len(sys.argv) > 1:
        fr = vp.extract_frames(sys.argv[1])
        for d in vp.describe_frames(fr):
            print("[%.1fs] %s" % (d["timestamp"], d["text"][:80]))
