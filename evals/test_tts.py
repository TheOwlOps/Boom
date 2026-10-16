"""Test voice/tts.py — chạy bằng mock, không gọi API thật, không cần loa."""

import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Import root is agent/src (repo convention); the modules under test live
# in subpackages there (they previously lived next to this file).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

from voice import tts


def _env_without_key():
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    return patch.dict(os.environ, env, clear=True)


def _hide_optional_deps():
    """Giả lập máy chưa cài edge_tts/pyttsx3 và không có binary espeak."""
    return (
        patch.dict(sys.modules, {"edge_tts": None, "pyttsx3": None}),
        patch("shutil.which", return_value=None),
    )


def _fake_edge_tts_module():
    mod = types.ModuleType("edge_tts")

    class FakeCommunicate:
        last_call = None

        def __init__(self, text, voice):
            FakeCommunicate.last_call = (text, voice)

        async def stream(self):
            # mô phỏng đúng format chunk của edge-tts: chỉ type=="audio" chứa data
            yield {"type": "words", "data": "ignored"}
            yield {"type": "audio", "data": b"chunk1"}
            yield {"type": "audio", "data": b"chunk2"}

    mod.Communicate = FakeCommunicate
    return mod


def _fake_pyttsx3_module():
    mod = types.ModuleType("pyttsx3")

    class FakeEngine:
        def save_to_file(self, text, path):
            Path(path).write_bytes(b"FAKEWAV:" + text.encode("utf-8"))

        def runAndWait(self):
            pass

        def stop(self):
            pass

    mod.init = lambda: FakeEngine()
    return mod


class TestAvailableBackends(unittest.TestCase):
    def test_empty_when_nothing_available(self):
        p1, p2 = _hide_optional_deps()
        with _env_without_key(), p1, p2:
            self.assertEqual(tts.available_backends(), [])

    def test_detects_edge_tts(self):
        p1, p2 = _hide_optional_deps()
        with _env_without_key(), p1, p2, patch.dict(
            sys.modules, {"edge_tts": _fake_edge_tts_module()}
        ):
            self.assertEqual(tts.available_backends(), ["edge-tts"])

    def test_priority_order(self):
        p1, p2 = _hide_optional_deps()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), p1, p2, patch.dict(
            sys.modules,
            {"edge_tts": _fake_edge_tts_module(), "pyttsx3": _fake_pyttsx3_module()},
        ), patch("shutil.which", side_effect=lambda b: "/usr/bin/espeak-ng" if b == "espeak-ng" else None):
            self.assertEqual(
                tts.available_backends(), ["edge-tts", "openai", "espeak", "pyttsx3"]
            )


class TestSpeakNoBackend(unittest.TestCase):
    def test_raises_not_implemented_vietnamese(self):
        p1, p2 = _hide_optional_deps()
        with _env_without_key(), p1, p2:
            with self.assertRaises(NotImplementedError) as ctx:
                tts.speak("xin chào")
            msg = str(ctx.exception)
            self.assertIn("No Text-to-Speech backend available", msg)
            self.assertIn("edge-tts", msg)

    def test_empty_text_raises_value_error(self):
        with self.assertRaises(ValueError):
            tts.speak("   ")

    def test_unknown_backend_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            tts.speak("hi", backend="khong-ton-tai")
        self.assertIn("Unsupported TTS backend", str(ctx.exception))


class TestSpeakEdgeTTS(unittest.TestCase):
    def test_success_mock_and_voice_mapping(self):
        fake = _fake_edge_tts_module()
        with _env_without_key(), patch.dict(sys.modules, {"edge_tts": fake}):
            result = tts.speak("xin chào")
        self.assertEqual(result, b"chunk1chunk2")
        # voice "vi-VN" phải được map sang tên Neural đầy đủ
        self.assertEqual(fake.Communicate.last_call, ("xin chào", "vi-VN-NamMinhNeural"))

    def test_full_voice_name_pass_through(self):
        fake = _fake_edge_tts_module()
        with _env_without_key(), patch.dict(sys.modules, {"edge_tts": fake}):
            tts.speak("hi", voice="en-US-AriaNeural")
        self.assertEqual(fake.Communicate.last_call[1], "en-US-AriaNeural")

    def test_priority_edge_over_openai(self):
        fake = _fake_edge_tts_module()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), patch.dict(
            sys.modules, {"edge_tts": fake}
        ), patch("httpx.post") as mock_post:
            tts.speak("xin chào")
        mock_post.assert_not_called()  # edge-tts được ưu tiên, openai không đụng tới

    def test_not_installed_but_selected(self):
        p1, p2 = _hide_optional_deps()
        with _env_without_key(), p1, p2:
            with self.assertRaises(RuntimeError) as ctx:
                tts.speak("hi", backend="edge-tts")
        self.assertIn("pip install edge-tts", str(ctx.exception))


class TestSpeakOpenAI(unittest.TestCase):
    def test_success_mock_and_voice_passthrough(self):
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.content = b"MP3BYTES"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), patch.dict(
            sys.modules, {"edge_tts": None}
        ), patch("httpx.post", return_value=fake_resp) as mock_post:
            result = tts.speak("hello world", voice="nova")
        self.assertEqual(result, b"MP3BYTES")
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual(
            kwargs["json"], {"model": "tts-1", "input": "hello world", "voice": "nova"}
        )

    def test_vietnamese_voice_maps_to_alloy(self):
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.content = b"MP3"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), patch.dict(
            sys.modules, {"edge_tts": None}
        ), patch("httpx.post", return_value=fake_resp) as mock_post:
            tts.speak("xin chào")  # voice mặc định "vi-VN" → OpenAI không có → "alloy"
        self.assertEqual(mock_post.call_args[1]["json"]["voice"], "alloy")

    def test_http_error_raises_runtime_error(self):
        fake_resp = MagicMock()
        fake_resp.status_code = 401
        fake_resp.text = "bad key"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-sai"}), patch.dict(
            sys.modules, {"edge_tts": None}
        ), patch("httpx.post", return_value=fake_resp):
            with self.assertRaises(RuntimeError) as ctx:
                tts.speak("hi", backend="openai")
        self.assertIn("401", str(ctx.exception))

    def test_missing_key_raises_clear_error(self):
        with _env_without_key():
            with self.assertRaises(RuntimeError) as ctx:
                tts.speak("hi", backend="openai")
        self.assertIn("OPENAI_API_KEY", str(ctx.exception))


class TestSpeakEspeak(unittest.TestCase):
    def _which_espeak_ng(self, binary):
        return "/usr/bin/espeak-ng" if binary == "espeak-ng" else None

    def test_success_mock(self):
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = b"WAVDATA"
        fake_proc.stderr = b""
        with patch("shutil.which", side_effect=self._which_espeak_ng), patch(
            "subprocess.run", return_value=fake_proc
        ) as mock_run:
            result = tts.speak("xin chào", backend="espeak")
        self.assertEqual(result, b"WAVDATA")
        args = mock_run.call_args[0][0]
        self.assertEqual(args[0], "/usr/bin/espeak-ng")
        self.assertIn("--stdout", args)
        self.assertIn("vi", args)  # voice vi-VN → espeak voice "vi"

    def test_failure_raises_runtime_error(self):
        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stdout = b""
        fake_proc.stderr = b"some espeak error"
        with patch("shutil.which", side_effect=self._which_espeak_ng), patch(
            "subprocess.run", return_value=fake_proc
        ):
            with self.assertRaises(RuntimeError) as ctx:
                tts.speak("hi", backend="espeak")
        self.assertIn("espeak", str(ctx.exception))

    def test_binary_missing_raises_clear_error(self):
        with patch("shutil.which", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                tts.speak("hi", backend="espeak")
        self.assertIn("espeak-ng", str(ctx.exception))


class TestSpeakPyttsx3(unittest.TestCase):
    def test_success_mock(self):
        with patch.dict(sys.modules, {"pyttsx3": _fake_pyttsx3_module()}):
            result = tts.speak("hello", backend="pyttsx3")
        self.assertEqual(result, b"FAKEWAV:hello")

    def test_not_installed_but_selected(self):
        p1, p2 = _hide_optional_deps()
        with _env_without_key(), p1, p2:
            with self.assertRaises(RuntimeError) as ctx:
                tts.speak("hi", backend="pyttsx3")
        self.assertIn("pip install pyttsx3", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
