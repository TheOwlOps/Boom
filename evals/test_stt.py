"""Test voice/stt.py — chạy bằng mock, không gọi API thật, không cần model Vosk thật."""

import io
import json
import os
import sys
from pathlib import Path
import tempfile
import types
import unittest
import wave
from unittest.mock import MagicMock, patch

# Import root is agent/src (repo convention); the modules under test live
# in subpackages there (they previously lived next to this file).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

from voice import stt


def _env_without_key():
    """Trả về context patch.dict xóa OPENAI_API_KEY khỏi môi trường."""
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    return patch.dict(os.environ, env, clear=True)


def _hide_vosk():
    """Giả lập môi trường chưa cài vosk (kể cả khi máy test có cài)."""
    return patch.dict(sys.modules, {"vosk": None})


def _make_wav_16k_mono(frames=8000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * frames)
    return buf.getvalue()


def _fake_vosk_module():
    mod = types.ModuleType("vosk")

    class FakeModel:
        def __init__(self, path):
            self.path = path

    class FakeRecognizer:
        def __init__(self, model, rate):
            self._first = True

        def SetWords(self, v):
            pass

        def AcceptWaveform(self, chunk):
            # chunk đầu accepted (partial), các chunk sau không
            if self._first:
                self._first = False
                return True
            return False

        def Result(self):
            return json.dumps({"text": "xin chào"})

        def FinalResult(self):
            return json.dumps({"text": "các bạn"})

    mod.Model = FakeModel
    mod.KaldiRecognizer = FakeRecognizer
    return mod


class TestAvailableBackends(unittest.TestCase):
    def test_empty_when_nothing_available(self):
        with _env_without_key(), _hide_vosk():
            self.assertEqual(stt.available_backends(), [])

    def test_openai_listed_when_key_present(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), _hide_vosk():
            self.assertEqual(stt.available_backends(), ["openai-whisper"])

    def test_vosk_listed_when_importable(self):
        with _env_without_key(), patch.dict(sys.modules, {"vosk": _fake_vosk_module()}):
            self.assertIn("vosk", stt.available_backends())

    def test_priority_openai_before_vosk(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), patch.dict(
            sys.modules, {"vosk": _fake_vosk_module()}
        ):
            self.assertEqual(stt.available_backends()[0], "openai-whisper")


class TestTranscribeNoBackend(unittest.TestCase):
    def test_raises_not_implemented_vietnamese(self):
        with _env_without_key(), _hide_vosk():
            with self.assertRaises(NotImplementedError) as ctx:
                stt.transcribe(b"fake-audio")
            msg = str(ctx.exception)
            self.assertIn("No Speech-to-Text backend available", msg)
            self.assertIn("OPENAI_API_KEY", msg)
            self.assertIn("vosk", msg)

    def test_empty_audio_raises_value_error(self):
        with self.assertRaises(ValueError):
            stt.transcribe(b"")

    def test_unknown_backend_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            stt.transcribe(b"x", backend="khong-ton-tai")
        self.assertIn("Unsupported STT backend", str(ctx.exception))


class TestTranscribeOpenAI(unittest.TestCase):
    def test_success_mock(self):
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"text": "  xin chào các bạn  "}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), _hide_vosk(), patch(
            "httpx.post", return_value=fake_resp
        ) as mock_post:
            result = stt.transcribe(b"ID3\x04fake-mp3-bytes", language="vi")
        self.assertEqual(result, "xin chào các bạn")
        # kiểm tra payload gửi đi: multipart đúng tên file/mime, đúng model + language
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-test")
        filename, fileobj, mime = kwargs["files"]["file"]
        self.assertEqual((filename, mime), ("audio.mp3", "audio/mpeg"))
        self.assertEqual(kwargs["data"], {"model": "whisper-1", "language": "vi"})

    def test_priority_uses_openai_even_if_vosk_available(self):
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"text": "ok"}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), patch.dict(
            sys.modules, {"vosk": _fake_vosk_module()}
        ), patch("httpx.post", return_value=fake_resp) as mock_post:
            stt.transcribe(b"RIFF....WAVE....")
        mock_post.assert_called_once()  # openai được chọn, vosk không đụng tới

    def test_http_error_raises_runtime_error(self):
        fake_resp = MagicMock()
        fake_resp.status_code = 401
        fake_resp.text = "Incorrect API key"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-sai"}), _hide_vosk(), patch(
            "httpx.post", return_value=fake_resp
        ):
            with self.assertRaises(RuntimeError) as ctx:
                stt.transcribe(b"audio")
        self.assertIn("401", str(ctx.exception))

    def test_explicit_backend_without_key_raises_clear_error(self):
        with _env_without_key(), _hide_vosk():
            with self.assertRaises(RuntimeError) as ctx:
                stt.transcribe(b"audio", backend="openai-whisper")
        self.assertIn("OPENAI_API_KEY", str(ctx.exception))

    def test_guess_audio_format(self):
        self.assertEqual(stt._guess_audio_format(b"RIFFxxxxWAVE"), ("wav", "audio/wav"))
        self.assertEqual(stt._guess_audio_format(b"ID3xxx"), ("mp3", "audio/mpeg"))
        self.assertEqual(stt._guess_audio_format(b"OggSxxx"), ("ogg", "audio/ogg"))
        self.assertEqual(stt._guess_audio_format(b"xxxxftyp"), ("m4a", "audio/mp4"))
        self.assertEqual(stt._guess_audio_format(b"????"), ("wav", "audio/wav"))


class TestTranscribeVosk(unittest.TestCase):
    def setUp(self):
        self.model_dir = tempfile.mkdtemp()

    def test_success_with_fake_vosk(self):
        wav = _make_wav_16k_mono()
        with _env_without_key(), patch.dict(
            sys.modules, {"vosk": _fake_vosk_module()}
        ), patch.dict(os.environ, {"VOSK_MODEL_PATH": self.model_dir}):
            result = stt.transcribe(wav, backend="vosk")
        self.assertEqual(result, "xin chào các bạn")

    def test_missing_model_dir_raises_clear_error(self):
        wav = _make_wav_16k_mono()
        with _env_without_key(), patch.dict(
            sys.modules, {"vosk": _fake_vosk_module()}
        ), patch.dict(os.environ, {"VOSK_MODEL_PATH": "/khong/ton/tai/model"}):
            with self.assertRaises(RuntimeError) as ctx:
                stt.transcribe(wav, backend="vosk")
        self.assertIn("model", str(ctx.exception).lower())

    def test_wrong_wav_format_raises_with_ffmpeg_hint(self):
        # WAV stereo 44.1kHz — sai chuẩn Vosk
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(b"\x00\x00" * 100)
        with _env_without_key(), patch.dict(
            sys.modules, {"vosk": _fake_vosk_module()}
        ), patch.dict(os.environ, {"VOSK_MODEL_PATH": self.model_dir}):
            with self.assertRaises(RuntimeError) as ctx:
                stt.transcribe(buf.getvalue(), backend="vosk")
        self.assertIn("ffmpeg", str(ctx.exception))

    def test_vosk_not_installed_but_selected(self):
        with _env_without_key(), _hide_vosk():
            with self.assertRaises(RuntimeError) as ctx:
                stt.transcribe(b"x", backend="vosk")
        self.assertIn("pip install vosk", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
