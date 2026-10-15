"""Text-to-Speech (TTS) cho Boom voice-mode.

GHI CHÚ: repo đã có ``agent/src/tools/tts_tool.py`` (tool ghi file .mp3 ra đĩa,
chỉ dùng edge-tts, và khi thiếu lib thì ghi SILENT một file stub giả).
Module này khác ở 3 điểm: (1) trả về ``bytes`` thay vì đường dẫn file —
tiện cho voice loop stream trực tiếp; (2) nhiều backend dự phòng theo thứ tự
ưu tiên; (3) KHÔNG bao giờ trả audio giả — thiếu backend thì raise lỗi rõ ràng.

Thứ tự ưu tiên backend (tự động chọn backend đầu tiên khả dụng):
1. ``edge-tts`` — MIỄN PHÍ, không cần key, giọng tiếng Việt tự nhiên
   (vi-VN-NamMinhNeural). Cần ``pip install edge-tts`` và mạng tới Microsoft.
2. ``openai`` — OpenAI TTS. Cần ``OPENAI_API_KEY``. Gọi qua HTTPS (httpx).
3. ``espeak`` — offline qua binary hệ thống ``espeak``/``espeak-ng``
   (giọng robot nhưng không cần mạng, không cần key).
4. ``pyttsx3`` — offline qua thư viện (``pip install pyttsx3``), dùng engine
   giọng nói có sẵn của OS.

Không có backend nào → ``NotImplementedError`` kèm hướng dẫn tiếng Việt.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

import httpx

_OPENAI_SPEECH_URL = "https://api.openai.com/v1/audio/speech"
_OPENAI_TTS_MODEL = "tts-1"
_OPENAI_VOICES = {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}

# Map mã giọng ngắn gọn -> tên giọng Edge TTS đầy đủ.
# Nếu người dùng truyền thẳng tên "xx-XX-...Neural" thì giữ nguyên.
_EDGE_VOICES = {
    "vi": "vi-VN-NamMinhNeural",
    "vi-VN": "vi-VN-NamMinhNeural",
    "en": "en-US-ChristopherNeural",
    "en-US": "en-US-ChristopherNeural",
}

_DEFAULT_VOICE = "vi-VN"


def available_backends() -> List[str]:
    """Liệt kê backend TTS đang dùng được, theo đúng thứ tự ưu tiên."""
    backends: List[str] = []
    try:
        import edge_tts  # noqa: F401

        backends.append("edge-tts")
    except ImportError:
        pass
    if os.environ.get("OPENAI_API_KEY"):
        backends.append("openai")
    if shutil.which("espeak-ng") or shutil.which("espeak"):
        backends.append("espeak")
    try:
        import pyttsx3  # noqa: F401

        backends.append("pyttsx3")
    except ImportError:
        pass
    return backends


def speak(
    text: str,
    voice: str = _DEFAULT_VOICE,
    backend: Optional[str] = None,
) -> bytes:
    """Tổng hợp giọng nói, trả về audio bytes (mp3 với edge-tts/openai, wav với espeak/pyttsx3).

    :param text: nội dung cần đọc. Rỗng → ``ValueError``.
    :param voice: ``"vi-VN"`` (mặc định) | ``"en-US"`` | tên giọng đầy đủ của backend
        (ví dụ ``"vi-VN-NamMinhNeural"``, hoặc ``"nova"`` cho OpenAI).
    :param backend: ép dùng backend cụ thể, mặc định tự chọn theo thứ tự ưu tiên.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("text rỗng — không có gì để đọc.")

    chosen = backend or _auto_backend()
    if chosen == "edge-tts":
        return _speak_edge(text, voice)
    if chosen == "openai":
        return _speak_openai(text, voice)
    if chosen == "espeak":
        return _speak_espeak(text, voice)
    if chosen == "pyttsx3":
        return _speak_pyttsx3(text, voice)
    raise ValueError(
        f"Backend TTS không hỗ trợ: {backend!r}. "
        f"Backend khả dụng hiện tại: {available_backends()}"
    )


def _auto_backend() -> str:
    backends = available_backends()
    if not backends:
        raise NotImplementedError(
            "Không có backend Text-to-Speech nào khả dụng.\n"
            "Hãy chọn một trong các cách sau:\n"
            "  1. Edge TTS (khuyên dùng, miễn phí, không cần key): pip install edge-tts\n"
            "  2. OpenAI TTS: export OPENAI_API_KEY=sk-...\n"
            "  3. espeak offline: sudo apt install espeak-ng  (giọng robot, không cần mạng)\n"
            "  4. pyttsx3 offline: pip install pyttsx3"
        )
    return backends[0]


def _edge_voice_name(voice: str) -> str:
    return _EDGE_VOICES.get(voice, voice)


def _speak_edge(text: str, voice: str) -> bytes:
    try:
        import edge_tts
    except ImportError:
        raise RuntimeError(
            "Backend edge-tts được chọn nhưng chưa cài thư viện: pip install edge-tts"
        )
    import asyncio

    edge_voice = _edge_voice_name(voice)

    async def _generate() -> bytes:
        chunks = []
        # stream() yield từng chunk {"type": "audio", "data": ...} — gom lại thành mp3 hoàn chỉnh.
        async for chunk in edge_tts.Communicate(text, edge_voice).stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                chunks.append(chunk["data"])
        return b"".join(chunks)

    try:
        audio = asyncio.run(_generate())
    except Exception as exc:
        raise RuntimeError(
            f"Edge TTS thất bại — backend này cần mạng tới Microsoft. Lỗi: {exc}"
        ) from exc
    if not audio:
        raise RuntimeError("Edge TTS không trả về audio (phản hồi rỗng).")
    return audio


def _speak_openai(text: str, voice: str) -> bytes:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Backend openai được chọn nhưng thiếu OPENAI_API_KEY. "
            "Hãy export OPENAI_API_KEY=sk-... trước khi gọi."
        )
    # OpenAI chỉ nhận 6 tên giọng cố định — map "vi-VN"/"en-US" về "alloy",
    # còn nếu user truyền thẳng "nova"... thì giữ nguyên.
    openai_voice = voice if voice in _OPENAI_VOICES else "alloy"
    try:
        resp = httpx.post(
            _OPENAI_SPEECH_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": _OPENAI_TTS_MODEL, "input": text, "voice": openai_voice},
            timeout=120.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Gọi OpenAI TTS thất bại (lỗi mạng): {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(
            f"OpenAI TTS trả lỗi HTTP {resp.status_code}: {resp.text[:300]}"
        )
    if not resp.content:
        raise RuntimeError("OpenAI TTS trả về audio rỗng.")
    return resp.content


def _espeak_binary() -> Optional[str]:
    # Ưu tiên espeak-ng (bản fork mới, hỗ trợ tiếng Việt tốt hơn espeak cũ).
    return shutil.which("espeak-ng") or shutil.which("espeak")


def _speak_espeak(text: str, voice: str) -> bytes:
    binary = _espeak_binary()
    if not binary:
        raise RuntimeError(
            "Backend espeak được chọn nhưng không tìm thấy binary espeak/espeak-ng. "
            "Cài bằng: sudo apt install espeak-ng"
        )
    espeak_voice = "vi" if voice.lower().startswith("vi") else "en"
    try:
        proc = subprocess.run(
            [binary, "--stdout", "-v", espeak_voice, "-s", "150", text],
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("espeak bị timeout sau 60s.") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"espeak báo lỗi (exit {proc.returncode}): "
            f"{proc.stderr.decode(errors='replace')[:300]}"
        )
    if not proc.stdout:
        raise RuntimeError("espeak không xuất ra audio.")
    return proc.stdout  # WAV bytes ghi ra stdout nhờ flag --stdout


def _speak_pyttsx3(text: str, voice: str) -> bytes:
    try:
        import pyttsx3
    except ImportError:
        raise RuntimeError(
            "Backend pyttsx3 được chọn nhưng chưa cài thư viện: pip install pyttsx3"
        )
    engine = pyttsx3.init()
    # pyttsx3 chỉ ghi ra file nên dùng file tạm rồi đọc bytes lên, xong xóa.
    fd, tmp_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        engine.save_to_file(text, tmp_path)
        engine.runAndWait()
        engine.stop()
        data = Path(tmp_path).read_bytes()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    if not data:
        raise RuntimeError("pyttsx3 không tạo ra audio.")
    return data
