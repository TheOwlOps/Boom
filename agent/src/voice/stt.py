"""Speech-to-Text (STT) for Boom voice-mode.

Backend priority order (auto-picks the first available):
1. ``openai-whisper`` — OpenAI Whisper API, high quality, handles Vietnamese well.
   Needs the ``OPENAI_API_KEY`` env var. Called over HTTPS (httpx, no openai lib needed).
2. ``vosk`` — offline, free, no key needed.
   Needs ``pip install vosk`` plus a Vietnamese model (point ``VOSK_MODEL_PATH``
   at the model directory).

With no usable backend, :func:`transcribe` raises ``NotImplementedError``
with setup instructions.
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
    """Lists the currently usable STT backends, in priority order."""
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
    """Transcribes speech from audio bytes, returns text.

    :param audio_bytes: raw audio data (wav/mp3/ogg/m4a...).
    :param language: ISO-639-1 language code, default ``"vi"``.
    :param backend: forces a specific backend (``"openai-whisper"`` | ``"vosk"``),
        default auto-picks by priority.
    """
    if not audio_bytes:
        raise ValueError("Empty audio_bytes — nothing to transcribe.")

    chosen = backend or _auto_backend()
    if chosen == "openai-whisper":
        return _transcribe_openai(audio_bytes, language)
    if chosen == "vosk":
        return _transcribe_vosk(audio_bytes, language)
    raise ValueError(
        f"Unsupported STT backend: {backend!r}. "
        f"Currently available backends: {available_backends()}"
    )


def _auto_backend() -> str:
    backends = available_backends()
    if not backends:
        raise NotImplementedError(
            "No Speech-to-Text backend available.\n"
            "Pick one of these:\n"
            "  1. OpenAI Whisper API (recommended, good Vietnamese): "
            "export OPENAI_API_KEY=sk-...\n"
            "  2. Vosk offline (free): pip install vosk, then download a "
            "Vietnamese model (e.g. vosk-model-small-vi-0.4) from "
            "https://alphacephei.com/vosk/models and point VOSK_MODEL_PATH "
            "at the model directory."
        )
    return backends[0]


def _guess_audio_format(audio_bytes: bytes) -> Tuple[str, str]:
    """Guesses the audio format from magic bytes to name the API upload.

    The Whisper API requires the multipart ``file`` field to have a filename +
    content-type; a wrong guess is usually fine since the server sniffs, but
    getting it right is safer.
    """
    if audio_bytes[:4] == b"RIFF" and audio_bytes[8:12] == b"WAVE":
        return "wav", "audio/wav"
    if audio_bytes[:3] == b"ID3" or audio_bytes[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3", "audio/mpeg"
    if audio_bytes[:4] == b"OggS":
        return "ogg", "audio/ogg"
    if audio_bytes[4:8] == b"ftyp":
        return "m4a", "audio/mp4"
    return "wav", "audio/wav"  # default — Whisper sniffs it anyway


def _transcribe_openai(audio_bytes: bytes, language: str) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "The openai-whisper backend was selected but OPENAI_API_KEY is missing. "
            "Export OPENAI_API_KEY=sk-... before calling."
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
        raise RuntimeError(f"Whisper API call failed (network error): {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(
            f"Whisper API returned HTTP {resp.status_code}: {resp.text[:300]}"
        )
    return (resp.json().get("text") or "").strip()


def _transcribe_vosk(audio_bytes: bytes, language: str) -> str:
    """Offline transcription with Vosk.

    Vosk only accepts 16kHz mono 16-bit PCM WAV — wrong-format audio gets a
    clear error plus the ffmpeg command to convert, instead of failing obscurely.
    """
    try:
        from vosk import KaldiRecognizer, Model
    except ImportError:
        raise RuntimeError(
            "The vosk backend was selected but the library is not installed: "
            "pip install vosk"
        )

    model_path = os.environ.get("VOSK_MODEL_PATH") or str(
        Path.home() / ".boom" / "vosk-model"
    )
    if not os.path.isdir(model_path):
        raise RuntimeError(
            f"Vosk model not found at: {model_path}\n"
            "Download a Vietnamese model (e.g. vosk-model-small-vi-0.4) from "
            "https://alphacephei.com/vosk/models, extract it, and point "
            "VOSK_MODEL_PATH at that directory."
        )

    # Vosk requires WAV 16kHz / mono / 16-bit — verify first with stdlib's wave module.
    try:
        wf = wave.open(io.BytesIO(audio_bytes), "rb")
    except wave.Error as exc:
        raise RuntimeError(
            f"The vosk backend needs a valid WAV file; the input could not be parsed: {exc}"
        ) from exc
    with wf:
        if (wf.getnchannels(), wf.getframerate(), wf.getsampwidth()) != (1, 16000, 2):
            raise RuntimeError(
                "The vosk backend needs 16kHz mono 16-bit WAV. Convert first with:\n"
                "  ffmpeg -i input.mp3 -ar 16000 -ac 1 -c:a pcm_s16le output.wav"
            )
        model = Model(model_path)
        rec = KaldiRecognizer(model, wf.getframerate())
        rec.SetWords(False)
        parts = []
        # Read 4000 frames per chunk (~0.25s at 16kHz): smooth enough without hogging RAM.
        while True:
            chunk = wf.readframes(4000)
            if not chunk:
                break
            if rec.AcceptWaveform(chunk):
                parts.append(json.loads(rec.Result()).get("text", ""))
        parts.append(json.loads(rec.FinalResult()).get("text", ""))
    return " ".join(p for p in parts if p).strip()
