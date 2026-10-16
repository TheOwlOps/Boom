"""Test cho VideoProcessor (agent/src/vision/video.py).

- Video test: tạo bằng ffmpeg testsrc (nếu có ffmpeg).
- OCR: dùng mock (không cần tesseract thật) + 1 test smoke với OCR thật
  nếu tesseract có trên máy.
- Fallback: giả lập thiếu cả ffmpeg + cv2 -> RuntimeError tiếng Việt.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# VideoProcessor lives at agent/src/vision/video.py (import root is agent/src).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

from vision.video import ERR_NO_BACKEND, VideoProcessor  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
TESSERACT = shutil.which("tesseract")


@pytest.fixture(scope="session")
def test_video(tmp_path_factory):
    """Tạo video 5s bằng ffmpeg testsrc (skip nếu không có ffmpeg)."""
    if not FFMPEG:
        pytest.skip("máy không có ffmpeg -> không tạo được video test")
    out = tmp_path_factory.mktemp("vids") / "test_5s.mp4"
    proc = subprocess.run(
        [
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=5:size=320x240:rate=10",
            "-pix_fmt", "yuv420p", str(out),
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return str(out)


@pytest.fixture()
def vp(tmp_path):
    return VideoProcessor(work_dir=str(tmp_path / "frames"))


# ----------------------------------------------------------------------
# 1. extract_frames với ffmpeg thật
# ----------------------------------------------------------------------
def test_extract_frames_ffmpeg(test_video, vp):
    if not vp.has_ffmpeg:
        pytest.skip("máy không có ffmpeg")
    frames = vp.extract_frames(test_video, fps=2.0, max_frames=60)
    # 5s * 2fps = 10 frames
    assert len(frames) == 10
    assert [f["index"] for f in frames] == list(range(10))
    # timestamp cách đều 0.5s
    assert frames[0]["timestamp"] == 0.0
    assert frames[1]["timestamp"] == 0.5
    assert frames[-1]["timestamp"] == 4.5
    for f in frames:
        assert Path(f["path"]).is_file(), f"thiếu file frame {f['path']}"


def test_extract_frames_respects_max_frames(test_video, vp):
    if not vp.has_ffmpeg:
        pytest.skip("máy không có ffmpeg")
    frames = vp.extract_frames(test_video, fps=10.0, max_frames=7)
    assert len(frames) == 7  # bị giới hạn, không nổ disk


def test_extract_frames_missing_file(vp):
    with pytest.raises(FileNotFoundError):
        vp.extract_frames("/khong-ton-tai/video.mp4")


def test_extract_frames_bad_args(vp, test_video):
    with pytest.raises(ValueError):
        vp.extract_frames(test_video, fps=0)
    with pytest.raises(ValueError):
        vp.extract_frames(test_video, max_frames=0)


# ----------------------------------------------------------------------
# 2. describe_frames với mock OCR
# ----------------------------------------------------------------------
def test_describe_frames_mock_ocr(tmp_path):
    frames = [
        {"index": 0, "timestamp": 0.0, "path": str(tmp_path / "a.jpg")},
        {"index": 1, "timestamp": 1.0, "path": str(tmp_path / "b.jpg")},
    ]
    vp = VideoProcessor(
        ocr_fn=lambda p: "chữ trong " + Path(p).name,
        work_dir=str(tmp_path / "frames"),
    )
    out = vp.describe_frames(frames)
    assert len(out) == 2
    assert out[0]["text"] == "chữ trong a.jpg"
    assert out[1]["text"] == "chữ trong b.jpg"
    assert out[0]["index"] == 0 and out[1]["timestamp"] == 1.0


def test_describe_frames_ocr_fail_no_crash(tmp_path):
    def bad_ocr(_p):
        raise RuntimeError("boom")

    vp = VideoProcessor(ocr_fn=bad_ocr, work_dir=str(tmp_path / "frames"))
    out = vp.describe_frames([{"index": 0, "timestamp": 0.0, "path": "x.jpg"}])
    assert out[0]["text"] == ""  # OCR fail -> text rỗng, không crash


def test_describe_frames_no_ocr_fn(tmp_path):
    vp = VideoProcessor(ocr_fn=None, work_dir=str(tmp_path / "frames"))
    vp.ocr_fn = None  # giả lập máy không có ocr_tool
    out = vp.describe_frames([{"index": 0, "timestamp": 0.0, "path": "x.jpg"}])
    assert out[0]["text"] == ""


# ----------------------------------------------------------------------
# 3. Fallback: thiếu cả ffmpeg + cv2 -> RuntimeError tiếng Việt
# ----------------------------------------------------------------------
def test_no_backend_raises_vietnamese_runtime_error(monkeypatch, vp, test_video):
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)
    vp._ffmpeg = None
    vp._cv2_ok = False
    with pytest.raises(RuntimeError) as exc:
        vp.extract_frames(test_video)
    msg = str(exc.value)
    assert "ffmpeg" in msg and "OpenCV" in msg
    assert "apt install ffmpeg" in msg  # hướng dẫn cài đặt rõ ràng
    assert ERR_NO_BACKEND in msg


# ----------------------------------------------------------------------
# 4. Smoke test OCR thật qua ocr_tool của Boom (skip nếu thiếu tesseract)
# ----------------------------------------------------------------------
def test_real_ocr_end_to_end(tmp_path, test_video):
    pytest.importorskip("PIL.Image")
    if not TESSERACT:
        pytest.skip("máy không có tesseract")
    from PIL import Image, ImageDraw

    img_path = tmp_path / "text.png"
    img = Image.new("RGB", (400, 120), "white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 40), "HELLO BOOM 123", fill="black")
    img.save(img_path)

    vp = VideoProcessor(work_dir=str(tmp_path / "frames"))
    if vp.ocr_fn is None:
        pytest.skip("không import được tools.ocr_tool của Boom")
    text = vp.ocr_fn(str(img_path))
    assert "HELLO" in text.upper() and "BOOM" in text.upper()
