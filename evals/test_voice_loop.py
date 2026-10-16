"""Test voice/voice_loop.py — mock toàn bộ stt/llm/tts, không cần mic/loa."""

import os
import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock

# Import root is agent/src (repo convention); the modules under test live
# in subpackages there (they previously lived next to this file).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

from voice.voice_loop import VoiceLoop, VoiceStageError, VoiceTurn


def make_loop(**overrides):
    """Tạo VoiceLoop với 3 mock đơn giản + dict ghi lại lời gọi."""
    calls = {}

    def stt_fn(audio):
        calls["stt"] = audio
        return "xin chào"

    def llm_fn(text):
        calls["llm"] = text
        return "chào bạn"

    def tts_fn(text):
        calls["tts"] = text
        return b"AUDIO_BYTES"

    kw = dict(stt_fn=stt_fn, llm_fn=llm_fn, tts_fn=tts_fn)
    kw.update(overrides)
    return VoiceLoop(**kw), calls


class TestProcessTurn(unittest.TestCase):
    def test_happy_path_returns_reply_audio(self):
        loop, calls = make_loop()
        result = loop.process_turn(b"fake-mic-audio")
        self.assertEqual(result, b"AUDIO_BYTES")
        # kiểm tra chuỗi STT → LLM → TTS nối đúng nhau
        self.assertEqual(calls["stt"], b"fake-mic-audio")
        self.assertEqual(calls["llm"], "xin chào")
        self.assertEqual(calls["tts"], "chào bạn")

    def test_stt_failure_wraps_stage(self):
        def bad_stt(audio):
            raise ValueError("mic hỏng")

        loop, calls = make_loop(stt_fn=bad_stt)
        with self.assertRaises(VoiceStageError) as ctx:
            loop.process_turn(b"x")
        self.assertEqual(ctx.exception.stage, "stt")
        self.assertIn("ValueError", str(ctx.exception))
        self.assertIsInstance(ctx.exception.original, ValueError)
        self.assertNotIn("llm", calls)  # stage sau không được chạy
        self.assertNotIn("tts", calls)

    def test_llm_failure_wraps_stage(self):
        def bad_llm(text):
            raise RuntimeError("LLM timeout")

        loop, _ = make_loop(llm_fn=bad_llm)
        with self.assertRaises(VoiceStageError) as ctx:
            loop.process_turn(b"x")
        self.assertEqual(ctx.exception.stage, "llm")

    def test_tts_failure_wraps_stage(self):
        def bad_tts(text):
            raise RuntimeError("no backend")

        loop, _ = make_loop(tts_fn=bad_tts)
        with self.assertRaises(VoiceStageError) as ctx:
            loop.process_turn(b"x")
        self.assertEqual(ctx.exception.stage, "tts")

    def test_empty_transcript_skips_llm(self):
        loop, calls = make_loop(stt_fn=lambda audio: "   ")
        with self.assertRaises(VoiceStageError) as ctx:
            loop.process_turn(b"im-lang")
        self.assertEqual(ctx.exception.stage, "stt")
        self.assertNotIn("llm", calls)  # không tốn LLM cho audio im lặng

    def test_empty_llm_reply_raises(self):
        loop, _ = make_loop(llm_fn=lambda text: "")
        with self.assertRaises(VoiceStageError) as ctx:
            loop.process_turn(b"x")
        self.assertEqual(ctx.exception.stage, "llm")

    def test_missing_llm_fn_raises_clear_error(self):
        loop = VoiceLoop(stt_fn=lambda a: "hi", tts_fn=lambda t: b"a")
        with self.assertRaises(VoiceStageError) as ctx:
            loop.process_turn(b"x")
        self.assertEqual(ctx.exception.stage, "llm")
        self.assertIn("llm_fn", str(ctx.exception))

    def test_turn_dataclass_fields(self):
        loop, _ = make_loop()
        turn = loop._run_turn(b"x")
        self.assertIsInstance(turn, VoiceTurn)
        self.assertEqual(turn.transcript, "xin chào")
        self.assertEqual(turn.reply_text, "chào bạn")
        self.assertEqual(turn.reply_audio, b"AUDIO_BYTES")
        self.assertGreaterEqual(turn.duration_s, 0.0)


class TestRun(unittest.TestCase):
    def test_processes_multiple_turns_until_source_none(self):
        loop, _ = make_loop()
        audios = [b"A1", b"A2"]

        def source():
            return audios.pop(0) if audios else None

        received = []
        n = loop.run(lambda t, r, a: received.append((t, r, a)), audio_source=source)
        self.assertEqual(n, 2)
        self.assertEqual(
            received,
            [("xin chào", "chào bạn", b"AUDIO_BYTES")] * 2,
        )

    def test_error_in_one_turn_does_not_crash_loop(self):
        script = iter([Exception("mic lỗi 1 lần"), "hello"])

        def flaky_stt(audio):
            v = next(script)
            if isinstance(v, Exception):
                raise v
            return v

        on_error = MagicMock()
        loop, _ = make_loop(stt_fn=flaky_stt, on_error=on_error)
        audios = [b"BAD", b"GOOD"]

        def source():
            return audios.pop(0) if audios else None

        received = []
        n = loop.run(lambda t, r, a: received.append((t, r, a)), audio_source=source)
        self.assertEqual(n, 1)  # lượt lỗi bị bỏ, lượt sau vẫn chạy
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], "hello")
        on_error.assert_called_once()
        err = on_error.call_args[0][0]
        self.assertIsInstance(err, VoiceStageError)
        self.assertEqual(err.stage, "stt")
        self.assertEqual(len(loop.errors), 1)

    def test_empty_transcript_turn_is_skipped_silently(self):
        script = iter(["   ", "hi"])
        loop, _ = make_loop(stt_fn=lambda audio: next(script))
        audios = [b"SILENT", b"HI"]

        def source():
            return audios.pop(0) if audios else None

        received = []
        n = loop.run(lambda t, r, a: received.append((t, r, a)), audio_source=source)
        self.assertEqual(n, 1)
        self.assertEqual(received[0][0], "hi")
        self.assertEqual(len(loop.errors), 1)  # vẫn ghi nhận để debug

    def test_stops_after_max_consecutive_errors(self):
        def always_fail(audio):
            raise RuntimeError("lỗi hoài")

        on_error = MagicMock()
        loop, _ = make_loop(stt_fn=always_fail, on_error=on_error,
                            max_consecutive_errors=3)
        n = loop.run(lambda t, r, a: None, audio_source=lambda: b"x")
        self.assertEqual(n, 0)
        # 3 lỗi stage + 1 thông báo dừng loop
        self.assertEqual(on_error.call_count, 4)
        self.assertEqual(loop.errors[-1].stage, "loop")

    def test_callback_error_does_not_crash_loop(self):
        loop, _ = make_loop()
        audios = [b"A1", b"A2"]

        def source():
            return audios.pop(0) if audios else None

        def bad_callback(t, r, a):
            raise RuntimeError("callback của user lỗi")

        n = loop.run(bad_callback, audio_source=source)
        self.assertEqual(n, 2)  # cả 2 lượt vẫn xử lý xong
        self.assertEqual(loop.errors[-1].stage, "callback")

    def test_source_error_does_not_crash_loop(self):
        script = iter([Exception("source lỗi"), b"OK"])

        def flaky_source():
            v = next(script, None)
            if isinstance(v, Exception):
                raise v
            return v

        loop, _ = make_loop()
        received = []
        n = loop.run(lambda t, r, a: received.append((t, r, a)), audio_source=flaky_source)
        self.assertEqual(n, 1)
        self.assertEqual(loop.errors[0].stage, "source")

    def test_stop_ends_infinite_loop(self):
        loop, _ = make_loop()
        received = []

        def cb(t, r, a):
            received.append((t, r, a))
            loop.stop()

        n = loop.run(cb, audio_source=lambda: b"x")  # nguồn vô hạn
        self.assertEqual(n, 1)
        self.assertEqual(len(received), 1)

    def test_max_turns_limits_loop(self):
        loop, _ = make_loop()
        n = loop.run(lambda t, r, a: None, audio_source=lambda: b"x", max_turns=3)
        self.assertEqual(n, 3)

    def test_missing_source_raises_value_error(self):
        loop, _ = make_loop()
        with self.assertRaises(ValueError) as ctx:
            loop.run(lambda t, r, a: None)
        self.assertIn("audio_source", str(ctx.exception))

    def test_missing_callback_raises_value_error(self):
        loop, _ = make_loop()
        with self.assertRaises(ValueError):
            loop.run(None, audio_source=lambda: None)


if __name__ == "__main__":
    unittest.main()
