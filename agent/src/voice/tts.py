"""Text-to-Speech (TTS) for Boom voice-mode.

NOTE: the repo already has ``agent/src/tools/tts_tool.py`` (a tool that writes
.mp3 files to disk, edge-tts only, and writes a SILENT fake stub file when the
lib is missing). This module differs in 3 ways: (1) returns ``bytes`` instead
of a file path — handy for streaming straight into the voice loop; (2) several
fallback backends in priority order; (3) NEVER returns fake audio — no backend
means a clear raised error.

Backend priority order (auto-picks the first available):
1. ``edge-tts`` — FREE, no key needed, natural Vietnamese voice
   (vi-VN-NamMinhNeural). Needs ``pip install edge-tts`` and network to Microsoft.
2. ``openai`` — OpenAI TTS. Needs ``OPENAI_API_KEY``. Called over HTTPS (httpx).
3. ``espeak`` — offline via the ``espeak``/``espeak-ng`` system binary
   (robotic voice but no network, no key).
4. ``pyttsx3`` — offline via the library (``pip install pyttsx3``), using the
   OS's built-in speech engine.

No backend at all → ``NotImplementedError`` with setup instructions.
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

# Map short voice codes -> full Edge TTS voice names.
# When the user passes a full "xx-XX-...Neural" name directly, keep it as-is.
_EDGE_VOICES = {
    "vi": "vi-VN-NamMinhNeural",
    "vi-VN": "vi-VN-NamMinhNeural",
    "en": "en-US-ChristopherNeural",
    "en-US": "en-US-ChristopherNeural",
}

_DEFAULT_VOICE = "vi-VN"


def available_backends() -> List[str]:
    """Lists the currently usable TTS backends, in priority order."""
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
    """Synthesizes speech, returns audio bytes (mp3 with edge-tts/openai, wav with espeak/pyttsx3).

    :param text: content to read. Empty → ``ValueError``.
    :param voice: ``"vi-VN"`` (default) | ``"en-US"`` | a backend's full voice name
        (e.g. ``"vi-VN-NamMinhNeural"``, or ``"nova"`` for OpenAI).
    :param backend: forces a specific backend; default auto-picks by priority.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Empty text — nothing to read.")

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
        f"Unsupported TTS backend: {backend!r}. "
        f"Currently available backends: {available_backends()}"
    )


def _auto_backend() -> str:
    backends = available_backends()
    if not backends:
        raise NotImplementedError(
            "No Text-to-Speech backend available.\n"
            "Pick one of these:\n"
            "  1. Edge TTS (recommended, free, no key): pip install edge-tts\n"
            "  2. OpenAI TTS: export OPENAI_API_KEY=sk-...\n"
            "  3. espeak offline: sudo apt install espeak-ng  (robotic voice, no network)\n"
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
            "The edge-tts backend was selected but the library is not installed: "
            "pip install edge-tts"
        )
    import asyncio

    edge_voice = _edge_voice_name(voice)

    async def _generate() -> bytes:
        chunks = []
        # stream() yields {"type": "audio", "data": ...} chunks — merge into a full mp3.
        async for chunk in edge_tts.Communicate(text, edge_voice).stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                chunks.append(chunk["data"])
        return b"".join(chunks)

    try:
        audio = asyncio.run(_generate())
    except Exception as exc:
        raise RuntimeError(
            f"Edge TTS failed — this backend needs network access to Microsoft. "
            f"Error: {exc}"
        ) from exc
    if not audio:
        raise RuntimeError("Edge TTS returned no audio (empty response).")
    return audio


def _speak_openai(text: str, voice: str) -> bytes:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "The openai backend was selected but OPENAI_API_KEY is missing. "
            "Export OPENAI_API_KEY=sk-... before calling."
        )
    # OpenAI only accepts 6 fixed voice names — map "vi-VN"/"en-US" to "alloy",
    # but keep it when the user passes "nova"... directly.
    openai_voice = voice if voice in _OPENAI_VOICES else "alloy"
    try:
        resp = httpx.post(
            _OPENAI_SPEECH_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": _OPENAI_TTS_MODEL, "input": text, "voice": openai_voice},
            timeout=120.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"OpenAI TTS call failed (network error): {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(
            f"OpenAI TTS returned HTTP {resp.status_code}: {resp.text[:300]}"
        )
    if not resp.content:
        raise RuntimeError("OpenAI TTS returned empty audio.")
    return resp.content


def _espeak_binary() -> Optional[str]:
    # Prefer espeak-ng (newer fork, better Vietnamese support than old espeak).
    return shutil.which("espeak-ng") or shutil.which("espeak")


def _speak_espeak(text: str, voice: str) -> bytes:
    binary = _espeak_binary()
    if not binary:
        raise RuntimeError(
            "The espeak backend was selected but no espeak/espeak-ng binary was found. "
            "Install with: sudo apt install espeak-ng"
        )
    espeak_voice = "vi" if voice.lower().startswith("vi") else "en"
    try:
        proc = subprocess.run(
            [binary, "--stdout", "-v", espeak_voice, "-s", "150", text],
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("espeak timed out after 60s.") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"espeak reported an error (exit {proc.returncode}): "
            f"{proc.stderr.decode(errors='replace')[:300]}"
        )
    if not proc.stdout:
        raise RuntimeError("espeak produced no audio.")
    return proc.stdout  # WAV bytes written to stdout via the --stdout flag


def _speak_pyttsx3(text: str, voice: str) -> bytes:
    try:
        import pyttsx3
    except ImportError:
        raise RuntimeError(
            "The pyttsx3 backend was selected but the library is not installed: "
            "pip install pyttsx3"
        )
    engine = pyttsx3.init()
    # pyttsx3 only writes to a file, so use a temp file then read the bytes, then delete.
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
        raise RuntimeError("pyttsx3 produced no audio.")
    return data
