"""VideoProcessor — trích xuất frames từ video rồi mô tả bằng OCR.

Backend trích frames (chọn tự động theo thứ tự ưu tiên):
  1. ffmpeg (subprocess) — KHUYẾN NGHỊ, có sẵn trên máy
  2. cv2 (OpenCV VideoCapture) — dự phòng nếu không có ffmpeg

Cả 2 đều là dependency OPTIONAL. Nếu thiếu cả 2, extract_frames() raise
RuntimeError với message tiếng Việt rõ ràng.

Module này KHÔNG copy code từ repo tham chiếu nào: tự viết cho Boom.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Message lỗi khi không có backend nào để trích frames.
ERR_NO_BACKEND = (
    "Không trích xuất được frame: máy chưa có ffmpeg và chưa có OpenCV. "
    "Hãy cài một trong hai rồi thử lại:\n"
    "  - ffmpeg:        apt install ffmpeg   (khuyến nghị)\n"
    "  - OpenCV:        pip install opencv-python-headless\n"
    "Lưu ý: không dùng apt install python3-opencv nếu muốn giữ môi trường sạch."
)

FFMPEG_TIMEOUT = 120  # giây, giới hạn mỗi lần gọi ffmpeg


def _has_cv2() -> bool:
    """Kiểm tra OpenCV có import được không (không import ồn ào)."""
    try:
        import importlib.util

        return importlib.util.find_spec("cv2") is not None
    except (ImportError, ValueError):
        return False


def _resolve_default_ocr() -> Optional[Callable[[str], str]]:
    """Tự tìm ocr_tool của Boom và bọc thành function(path) -> text.

    ocr_image() của Boom trả về dict {"text": ...} hoặc {"error": ...},
    không raise — nên wrapper chuyển mọi lỗi thành text rỗng.
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
            ocr_fn: function(path) -> text. Nếu None, tự thử lấy từ
                tools.ocr_tool của Boom. Nếu cũng không có thì frames
                vẫn trích được, chỉ là text sẽ rỗng.
            work_dir: thư mục gốc lưu frames trích xuất.
        """
        self.ocr_fn = ocr_fn if ocr_fn is not None else _resolve_default_ocr()
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._ffmpeg = shutil.which("ffmpeg")
        self._cv2_ok = _has_cv2()

    # ------------------------------------------------------------------
    # Thuộc tính kiểm tra backend
    # ------------------------------------------------------------------
    @property
    def has_ffmpeg(self) -> bool:
        return self._ffmpeg is not None

    @property
    def has_cv2(self) -> bool:
        return self._cv2_ok

    # ------------------------------------------------------------------
    # API chính
    # ------------------------------------------------------------------
    def extract_frames(
        self,
        video_path: str,
        fps: float = 1.0,
        max_frames: int = 60,
    ) -> List[Dict[str, Any]]:
        """Trích frames từ video.

        Returns: [{"index": int, "timestamp": float (giây), "path": str}]
        """
        video = Path(video_path)
        if not video.exists():
            raise FileNotFoundError("Không tìm thấy file video: %s" % video_path)
        if fps <= 0:
            raise ValueError("fps phải > 0, nhận được %r" % (fps,))
        if max_frames < 1:
            raise ValueError("max_frames phải >= 1, nhận được %r" % (max_frames,))

        # Mỗi video có thư mục frames riêng (tránh đè lẫn nhau khi xử lý nhiều video).
        digest = hashlib.sha256(str(video.resolve()).encode()).hexdigest()[:12]
        out_dir = self.work_dir / digest
        # Dọn frames cũ của video này để kết quả luôn tươi.
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)

        if self._ffmpeg:
            return self._extract_ffmpeg(video, out_dir, fps, max_frames)
        if self._cv2_ok:
            return self._extract_cv2(video, out_dir, fps, max_frames)
        raise RuntimeError(ERR_NO_BACKEND)

    def describe_frames(self, frames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Chạy OCR trên từng frame.

        Returns: [{"index", "timestamp", "text", "path"}]
        OCR fail hoặc không có ocr_fn -> text = "" (không crash).
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
            "fps=%.4f" % fps,  # trích đúng fps yêu cầu, không phụ thuộc fps gốc
            "-frames:v",
            str(max_frames),  # giới hạn số frame -> không nổ disk
            "-q:v",
            "4",  # chất lượng JPEG vừa đủ cho OCR, file nhỏ
            pattern,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "ffmpeg trích frames quá %ds, đã hủy (video: %s)"
                % (FFMPEG_TIMEOUT, video)
            )
        except OSError as e:
            raise RuntimeError("Không chạy được ffmpeg: %s" % (e,))
        if proc.returncode != 0:
            raise RuntimeError(
                "ffmpeg trích frames thất bại: %s" % ((proc.stderr or "").strip()[:500])
            )

        frames: List[Dict[str, Any]] = []
        for i, p in enumerate(sorted(out_dir.glob("frame_*.jpg"))):
            frames.append(
                {
                    "index": i,
                    # Timestamp suy từ thứ tự frame / fps yêu cầu (filter fps=
                    # cho frame cách đều, chính xác với video bình thường).
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
        import cv2  # import lazy: cv2 là optional

        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            raise RuntimeError("OpenCV không mở được video: %s" % (video,))
        try:
            src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            # Bước nhảy: cứ mỗi `step` frame gốc thì lấy 1 frame.
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
    """Helper nhỏ cho test kiểm tra khả năng import mà không gây crash."""
    try:
        __import__(name)
        return True
    except ImportError:
        return False


if __name__ == "__main__":  # pragma: no cover
    # Chạy nhanh: python3 video.py <file.mp4>
    vp = VideoProcessor()
    print("ffmpeg:", "có" if vp.has_ffmpeg else "không",
          "| cv2:", "có" if vp.has_cv2 else "không",
          "| ocr:", "có" if vp.ocr_fn else "không")
    if len(sys.argv) > 1:
        fr = vp.extract_frames(sys.argv[1])
        for d in vp.describe_frames(fr):
            print("[%.1fs] %s" % (d["timestamp"], d["text"][:80]))
