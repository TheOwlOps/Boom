"""Speech-to-Text (STT) cho Boom voice-mode.

Thứ tự ưu tiên backend (tự động chọn backend đầu tiên khả dụng):
1. ``openai-whisper`` — OpenAI Whisper API, chất lượng cao, nhận tiếng Việt tốt.
   Cần biến môi trường ``OPENAI_API_KEY``. Gọi qua HTTPS (dùng httpx, không cần lib openai).
2. ``vosk`` — offline, miễn phí, không cần key.
   Cần ``pip install vosk`` và model tiếng Việt (đặt ``VOSK_MODEL_PATH`` trỏ tới thư mục model).

Nếu không có backend nào khả dụng, :func:`transcribe` raise ``NotImplementedError``
kèm hướng dẫn tiếng Việt.
"""

from __future__ import annotations

import io
import json
import os
import wave
from pathlib import Path
from typing import List, Optional, Tuple

import httpx

_OPENAI_TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"
_OPENAI_MODEL = "whisper-1"


def available_backends() -> List[str]:
    """Liệt kê backend STT đang dùng được, theo đúng thứ tự ưu tiên."""
    backends: List[str] = []
    if os.environ.get("OPENAI_API_KEY"):
        backends.append("openai-whisper")
    try:
        import vosk  # noqa: F401

        backends.append("vosk")
    except ImportError:
        pass
    return backends


def transcribe(
    audio_bytes: bytes,
    language: str = "vi",
    backend: Optional[str] = None,
) -> str:
    """Nhận dạng giọng nói từ audio bytes, trả về text.

    :param audio_bytes: dữ liệu audio thô (wav/mp3/ogg/m4a...).
    :param language: mã ngôn ngữ ISO-639-1, mặc định ``"vi"``.
    :param backend: ép dùng backend cụ thể (``"openai-whisper"`` | ``"vosk"``),
        mặc định tự chọn theo thứ tự ưu tiên.
    """
    if not audio_bytes:
        raise ValueError("audio_bytes rỗng — không có gì để nhận dạng.")

    chosen = backend or _auto_backend()
    if chosen == "openai-whisper":
        return _transcribe_openai(audio_bytes, language)
    if chosen == "vosk":
        return _transcribe_vosk(audio_bytes, language)
    raise ValueError(
        f"Backend STT không hỗ trợ: {backend!r}. "
        f"Backend khả dụng hiện tại: {available_backends()}"
    )


def _auto_backend() -> str:
    backends = available_backends()
    if not backends:
        raise NotImplementedError(
            "Không có backend Speech-to-Text nào khả dụng.\n"
            "Hãy chọn một trong hai cách sau:\n"
            "  1. Dùng OpenAI Whisper API (khuyên dùng, tiếng Việt tốt): "
            "export OPENAI_API_KEY=sk-...\n"
            "  2. Dùng Vosk offline (miễn phí): pip install vosk, rồi tải model "
            "tiếng Việt (ví dụ vosk-model-small-vi-0.4) từ "
            "https://alphacephei.com/vosk/models và đặt VOSK_MODEL_PATH "
            "trỏ tới thư mục model."
        )
    return backends[0]


def _guess_audio_format(audio_bytes: bytes) -> Tuple[str, str]:
    """Đoán định dạng audio từ magic bytes để đặt tên file gửi lên API.

    Whisper API yêu cầu multipart field ``file`` có filename + content-type;
    đoán sai định dạng thường vẫn OK vì server tự sniff, nhưng đặt đúng thì chắc ăn hơn.
    """
    if audio_bytes[:4] == b"RIFF" and audio_bytes[8:12] == b"WAVE":
        return "wav", "audio/wav"
    if audio_bytes[:3] == b"ID3" or audio_bytes[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3", "audio/mpeg"
    if audio_bytes[:4] == b"OggS":
        return "ogg", "audio/ogg"
    if audio_bytes[4:8] == b"ftyp":
        return "m4a", "audio/mp4"
    return "wav", "audio/wav"  # mặc định — Whisper tự nhận diện được


def _transcribe_openai(audio_bytes: bytes, language: str) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Backend openai-whisper được chọn nhưng thiếu OPENAI_API_KEY. "
            "Hãy export OPENAI_API_KEY=sk-... trước khi gọi."
        )
    ext, mime = _guess_audio_format(audio_bytes)
    files = {"file": (f"audio.{ext}", io.BytesIO(audio_bytes), mime)}
    data = {"model": _OPENAI_MODEL, "language": language}
    try:
        resp = httpx.post(
            _OPENAI_TRANSCRIBE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files=files,
            data=data,
            timeout=120.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Gọi Whisper API thất bại (lỗi mạng): {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(
            f"Whisper API trả lỗi HTTP {resp.status_code}: {resp.text[:300]}"
        )
    return (resp.json().get("text") or "").strip()


def _transcribe_vosk(audio_bytes: bytes, language: str) -> str:
    """Nhận dạng offline bằng Vosk.

    Vosk chỉ nhận PCM WAV 16kHz mono 16-bit — nếu audio không đúng chuẩn thì
    báo lỗi rõ ràng kèm lệnh ffmpeg để convert, thay vì fail một cách khó hiểu.
    """
    try:
        from vosk import KaldiRecognizer, Model
    except ImportError:
        raise RuntimeError(
            "Backend vosk được chọn nhưng chưa cài thư viện: pip install vosk"
        )

    model_path = os.environ.get("VOSK_MODEL_PATH") or str(
        Path.home() / ".boom" / "vosk-model"
    )
    if not os.path.isdir(model_path):
        raise RuntimeError(
            f"Không tìm thấy model Vosk tại: {model_path}\n"
            "Hãy tải model tiếng Việt (ví dụ vosk-model-small-vi-0.4) từ "
            "https://alphacephei.com/vosk/models, giải nén rồi đặt "
            "VOSK_MODEL_PATH trỏ tới thư mục đó."
        )

    # Vosk bắt buộc WAV 16kHz / mono / 16-bit — kiểm tra trước bằng module wave của stdlib.
    try:
        wf = wave.open(io.BytesIO(audio_bytes), "rb")
    except wave.Error as exc:
        raise RuntimeError(
            f"Backend vosk cần file WAV hợp lệ, dữ liệu đưa vào không parse được: {exc}"
        ) from exc
    with wf:
        if (wf.getnchannels(), wf.getframerate(), wf.getsampwidth()) != (1, 16000, 2):
            raise RuntimeError(
                "Backend vosk cần WAV 16kHz mono 16-bit. Hãy convert trước bằng:\n"
                "  ffmpeg -i input.mp3 -ar 16000 -ac 1 -c:a pcm_s16le output.wav"
            )
        model = Model(model_path)
        rec = KaldiRecognizer(model, wf.getframerate())
        rec.SetWords(False)
        parts = []
        # Đọc từng chunk 4000 frames (~0.25s ở 16kHz): vừa đủ mượt, vừa không nuốt RAM.
        while True:
            chunk = wf.readframes(4000)
            if not chunk:
                break
            if rec.AcceptWaveform(chunk):
                parts.append(json.loads(rec.Result()).get("text", ""))
        parts.append(json.loads(rec.FinalResult()).get("text", ""))
    return " ".join(p for p in parts if p).strip()
