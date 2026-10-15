"""Vòng lặp hội thoại giọng nói (framework).

Thiết kế theo kiểu dependency injection để dễ test và dễ gắn backend thật:
- ``stt_fn(audio_bytes) -> str``   — mặc định dùng :mod:`voice.stt`
- ``llm_fn(transcript) -> str``    — BẮT BUỘC truyền vào (module này không
  ép phụ thuộc vào provider LLM nào của repo, tránh import vòng)
- ``tts_fn(text) -> bytes``        — mặc định dùng :mod:`voice.tts`
- ``audio_source() -> bytes | None`` — nguồn audio (mic, file, hàng đợi...);
  trả về ``None`` nghĩa là hết → loop dừng êm.

Luồng một lượt: ``process_turn`` chạy STT → LLM → TTS.
``run`` lặp: lấy audio → xử lý → gọi ``callback(transcript, reply_text, reply_audio)``.
Lỗi ở bất kỳ stage nào cũng KHÔNG crash loop: ghi nhận qua ``on_error``
rồi làm tiếp lượt sau; chỉ dừng khi lỗi liên tiếp vượt ngưỡng.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, List, Optional


class VoiceStageError(Exception):
    """Lỗi của một stage trong voice loop. ``stage`` là một trong:
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
        # Import lazy để voice_loop.py đứng độc lập, không bắt buộc stt/tts
        # phải import được ngay cả khi người dùng chỉ inject mock.
        from voice.stt import transcribe as _default_stt
        from voice.tts import speak as _default_tts

        self.stt_fn = stt_fn or _default_stt
        self.llm_fn = llm_fn  # None → báo lỗi rõ ràng khi tới lượt dùng, không đoán mò
        self.tts_fn = tts_fn or _default_tts
        self.audio_source = audio_source
        self.on_error = on_error
        self.max_consecutive_errors = max_consecutive_errors
        self.errors: List[VoiceStageError] = []
        self._stop_requested = False

    # ------------------------------------------------------------------ API chính

    def process_turn(self, audio_bytes: bytes) -> bytes:
        """Xử lý một lượt hội thoại: STT → LLM → TTS.

        Trả về audio bytes của câu trả lời. Lỗi stage nào → raise
        ``VoiceStageError`` ghi rõ stage đó (để ``run`` bắt và tiếp tục loop).
        """
        return self._run_turn(audio_bytes).reply_audio

    def run(
        self,
        callback: Callable[[str, str, bytes], None],
        audio_source: Optional[Callable[[], Optional[bytes]]] = None,
        max_turns: Optional[int] = None,
    ) -> int:
        """Chạy vòng lặp cho tới khi ``audio_source()`` trả về ``None``,
        gọi :meth:`stop`, hoặc đạt ``max_turns``.

        Sau mỗi lượt thành công gọi ``callback(transcript, reply_text, reply_audio)``.
        Trả về số lượt đã xử lý thành công.
        """
        source = audio_source or self.audio_source
        if source is None:
            raise ValueError(
                "Chưa có audio_source — truyền vào run(callback, audio_source=...) "
                "hoặc VoiceLoop(audio_source=...)."
            )
        if callback is None:
            raise ValueError("callback là bắt buộc — nơi nhận (transcript, reply_text, reply_audio).")

        turns = 0
        consecutive_errors = 0
        while not self._stop_requested:
            if max_turns is not None and turns >= max_turns:
                break

            try:
                audio = source()
            except Exception as exc:
                consecutive_errors = self._note_error(
                    VoiceStageError("source", f"Lỗi lấy audio: {exc}", original=exc),
                    consecutive_errors,
                )
                if self._too_many_errors(consecutive_errors):
                    break
                continue
            if audio is None:
                break  # hết nguồn audio → dừng êm, không phải lỗi

            try:
                turn = self._run_turn(audio)
            except VoiceStageError as err:
                consecutive_errors = self._note_error(err, consecutive_errors)
                if self._too_many_errors(consecutive_errors):
                    break
                continue

            consecutive_errors = 0  # lượt OK → reset đếm lỗi liên tiếp
            turns += 1
            try:
                callback(turn.transcript, turn.reply_text, turn.reply_audio)
            except Exception as exc:
                # Callback của người dùng lỗi thì ghi nhận, không giết loop.
                self._note_error(
                    VoiceStageError("callback", f"Lỗi trong callback: {exc}", original=exc),
                    consecutive_errors,
                )
        return turns

    def stop(self) -> None:
        """Yêu cầu dừng loop một cách êm (thread-safe cho flag boolean)."""
        self._stop_requested = True

    # ------------------------------------------------------------------ nội bộ

    def _run_turn(self, audio_bytes: bytes) -> VoiceTurn:
        started = time.monotonic()

        transcript = (self._stage("stt", lambda: self.stt_fn(audio_bytes)) or "").strip()
        if not transcript:
            # Không nghe được gì (im lặng / nhiễu) → bỏ qua lượt, khỏi tốn LLM.
            raise VoiceStageError("stt", "Không nhận dạng được giọng nói (transcript rỗng).")

        if self.llm_fn is None:
            raise VoiceStageError(
                "llm",
                "Chưa cấu hình llm_fn — truyền hàm LLM vào VoiceLoop(llm_fn=...).",
            )
        reply_text = (self._stage("llm", lambda: self.llm_fn(transcript)) or "").strip()
        if not reply_text:
            raise VoiceStageError("llm", "LLM trả về text rỗng.")

        reply_audio = self._stage("tts", lambda: self.tts_fn(reply_text))
        if not reply_audio:
            raise VoiceStageError("tts", "TTS không trả về audio.")

        return VoiceTurn(
            transcript=transcript,
            reply_text=reply_text,
            reply_audio=reply_audio,
            duration_s=time.monotonic() - started,
        )

    def _stage(self, name: str, fn: Callable):
        """Chạy một stage, bọc mọi exception lạ thành VoiceStageError giữ nguyên gốc."""
        try:
            return fn()
        except VoiceStageError:
            raise  # đã là lỗi stage chuẩn thì giữ nguyên, không bọc 2 lần
        except Exception as exc:
            raise VoiceStageError(
                name, f"{type(exc).__name__}: {exc}", original=exc
            ) from exc

    def _note_error(self, err: VoiceStageError, consecutive_errors: int) -> int:
        """Ghi nhận lỗi: lưu vào self.errors + gọi on_error nếu có. Trả về số lỗi liên tiếp mới."""
        self.errors.append(err)
        if self.on_error is not None:
            try:
                self.on_error(err)
            except Exception:
                pass  # on_error của user lỗi thì bỏ qua, loop vẫn sống
        return consecutive_errors + 1

    def _too_many_errors(self, consecutive_errors: int) -> bool:
        if consecutive_errors >= self.max_consecutive_errors:
            self._note_error(
                VoiceStageError(
                    "loop",
                    f"Dừng loop sau {consecutive_errors} lỗi liên tiếp.",
                ),
                consecutive_errors,
            )
            return True
        return False
