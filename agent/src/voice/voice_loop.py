"""Voice conversation loop (framework).

Designed with dependency injection for easy testing and easy wiring of real
backends:
- ``stt_fn(audio_bytes) -> str``   — defaults to :mod:`voice.stt`
- ``llm_fn(transcript) -> str``    — REQUIRED (this module doesn't force a
  dependency on any of the repo's LLM providers, avoiding import cycles)
- ``tts_fn(text) -> bytes``        — defaults to :mod:`voice.tts`
- ``audio_source() -> bytes | None`` — audio source (mic, file, queue...);
  returning ``None`` means exhausted → the loop stops gracefully.

One turn: ``process_turn`` runs STT → LLM → TTS.
``run`` loops: takes audio → processes → calls
``callback(transcript, reply_text, reply_audio)``.
An error at any stage never crashes the loop: it's recorded via ``on_error``
and the next turn proceeds; the loop only stops when consecutive errors
exceed the threshold.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, List, Optional


class VoiceStageError(Exception):
    """Error of one stage in the voice loop. ``stage`` is one of:
    ``"source"`` | ``"stt"`` | ``"llm"`` | ``"tts"`` | ``"callback"`` | ``"loop"``."""

    def __init__(self, stage: str, message: str, original: Optional[Exception] = None):
        self.stage = stage
        self.original = original
        super().__init__(f"[{stage}] {message}")


@dataclass
class VoiceTurn:
    transcript: str
    reply_text: str
    reply_audio: bytes
    duration_s: float = 0.0


class VoiceLoop:
    def __init__(
        self,
        stt_fn: Optional[Callable[[bytes], str]] = None,
        llm_fn: Optional[Callable[[str], str]] = None,
        tts_fn: Optional[Callable[[str], bytes]] = None,
        audio_source: Optional[Callable[[], Optional[bytes]]] = None,
        on_error: Optional[Callable[[VoiceStageError], None]] = None,
        max_consecutive_errors: int = 5,
    ):
        # Lazy imports so voice_loop.py stands alone — stt/tts don't need to
        # import cleanly even when the user only injects mocks.
        from voice.stt import transcribe as _default_stt
        from voice.tts import speak as _default_tts

        self.stt_fn = stt_fn or _default_stt
        self.llm_fn = llm_fn  # None → a clear error when it's time to use it, no guessing
        self.tts_fn = tts_fn or _default_tts
        self.audio_source = audio_source
        self.on_error = on_error
        self.max_consecutive_errors = max_consecutive_errors
        self.errors: List[VoiceStageError] = []
        self._stop_requested = False

    # ------------------------------------------------------------------ main API

    def process_turn(self, audio_bytes: bytes) -> bytes:
        """Processes one conversation turn: STT → LLM → TTS.

        Returns the reply's audio bytes. A stage failure → raises
        ``VoiceStageError`` naming that stage (for ``run`` to catch and continue).
        """
        return self._run_turn(audio_bytes).reply_audio

    def run(
        self,
        callback: Callable[[str, str, bytes], None],
        audio_source: Optional[Callable[[], Optional[bytes]]] = None,
        max_turns: Optional[int] = None,
    ) -> int:
        """Runs the loop until ``audio_source()`` returns ``None``,
        :meth:`stop` is called, or ``max_turns`` is reached.

        After each successful turn calls ``callback(transcript, reply_text, reply_audio)``.
        Returns the number of successfully processed turns.
        """
        source = audio_source or self.audio_source
        if source is None:
            raise ValueError(
                "No audio_source — pass run(callback, audio_source=...) "
                "or VoiceLoop(audio_source=...)."
            )
        if callback is None:
            raise ValueError("callback is required — it receives (transcript, reply_text, reply_audio).")

        turns = 0
        consecutive_errors = 0
        while not self._stop_requested:
            if max_turns is not None and turns >= max_turns:
                break

            try:
                audio = source()
            except Exception as exc:
                consecutive_errors = self._note_error(
                    VoiceStageError("source", f"Audio capture error: {exc}", original=exc),
                    consecutive_errors,
                )
                if self._too_many_errors(consecutive_errors):
                    break
                continue
            if audio is None:
                break  # audio source exhausted → stop gracefully, not an error

            try:
                turn = self._run_turn(audio)
            except VoiceStageError as err:
                consecutive_errors = self._note_error(err, consecutive_errors)
                if self._too_many_errors(consecutive_errors):
                    break
                continue

            consecutive_errors = 0  # OK turn → reset the consecutive-error counter
            turns += 1
            try:
                callback(turn.transcript, turn.reply_text, turn.reply_audio)
            except Exception as exc:
                # A user callback failing is recorded, never kills the loop.
                self._note_error(
                    VoiceStageError("callback", f"Callback error: {exc}", original=exc),
                    consecutive_errors,
                )
        return turns

    def stop(self) -> None:
        """Requests a graceful loop stop (thread-safe for a boolean flag)."""
        self._stop_requested = True

    # ------------------------------------------------------------------ internals

    def _run_turn(self, audio_bytes: bytes) -> VoiceTurn:
        started = time.monotonic()

        transcript = (self._stage("stt", lambda: self.stt_fn(audio_bytes)) or "").strip()
        if not transcript:
            # Heard nothing (silence / noise) → skip the turn, don't waste LLM calls.
            raise VoiceStageError("stt", "No speech recognized (empty transcript).")

        if self.llm_fn is None:
            raise VoiceStageError(
                "llm",
                "llm_fn not configured — pass an LLM function into VoiceLoop(llm_fn=...).",
            )
        reply_text = (self._stage("llm", lambda: self.llm_fn(transcript)) or "").strip()
        if not reply_text:
            raise VoiceStageError("llm", "LLM returned empty text.")

        reply_audio = self._stage("tts", lambda: self.tts_fn(reply_text))
        if not reply_audio:
            raise VoiceStageError("tts", "TTS returned no audio.")

        return VoiceTurn(
            transcript=transcript,
            reply_text=reply_text,
            reply_audio=reply_audio,
            duration_s=time.monotonic() - started,
        )

    def _stage(self, name: str, fn: Callable):
        """Runs one stage, wrapping any stray exception as VoiceStageError keeping the original."""
        try:
            return fn()
        except VoiceStageError:
            raise  # already a proper stage error — don't double-wrap
        except Exception as exc:
            raise VoiceStageError(
                name, f"{type(exc).__name__}: {exc}", original=exc
            ) from exc

    def _note_error(self, err: VoiceStageError, consecutive_errors: int) -> int:
        """Records an error: saves to self.errors + calls on_error when set. Returns the new consecutive count."""
        self.errors.append(err)
        if self.on_error is not None:
            try:
                self.on_error(err)
            except Exception:
                pass  # a failing user on_error is ignored, the loop stays alive
        return consecutive_errors + 1

    def _too_many_errors(self, consecutive_errors: int) -> bool:
        if consecutive_errors >= self.max_consecutive_errors:
            self._note_error(
                VoiceStageError(
                    "loop",
                    f"Stopping the loop after {consecutive_errors} consecutive errors.",
                ),
                consecutive_errors,
            )
            return True
        return False
