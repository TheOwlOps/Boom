"""Tests for the Ollama status endpoint logic in gateway/api.py.

Run:  python3 evals/test_ollama_status.py   (or: pytest evals/test_ollama_status.py)

Covers ``_check_ollama_status`` — the function behind
``GET /api/providers/ollama/status``. All network calls are mocked with
``unittest.mock``; no real Ollama daemon or internet access is needed.
"""

import io
import json
import sys
import urllib.error
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch

# Repo layout: evals/ sits next to gateway/, so the gateway package is importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.api import _check_ollama_status


def _fake_response(status=200, payload=None):
    """Build a minimal context-manager stand-in for urlopen's response."""
    if payload is None:
        payload = {"models": []}

    class FakeResponse:
        def __init__(self):
            self.status = status
            self._body = io.BytesIO(json.dumps(payload).encode("utf-8"))

        def read(self):
            return self._body.read()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return FakeResponse()


class TestOllamaStatus(TestCase):
    def test_running_with_models_parses_names(self):
        """A healthy /api/tags payload yields running=True + parsed model names."""
        payload = {
            "models": [
                {"name": "llama3.1:latest"},
                {"name": "codellama:7b"},
                {"name": "mistral"},
            ]
        }
        with patch("urllib.request.urlopen", return_value=_fake_response(200, payload)):
            result = _check_ollama_status()
        self.assertEqual(
            result,
            {
                "running": True,
                # ":latest" is stripped, other tags are kept.
                "models": ["llama3.1", "codellama:7b", "mistral"],
            },
        )

    def test_running_with_no_models(self):
        """Ollama running but empty library -> running=True, models=[]."""
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_response(200, {"models": []}),
        ):
            result = _check_ollama_status()
        self.assertEqual(result, {"running": True, "models": []})

    def test_connection_refused_returns_not_running(self):
        """Ollama not running -> never raises, returns running=False."""
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            result = _check_ollama_status()
        self.assertEqual(result, {"running": False, "models": []})

    def test_timeout_returns_not_running(self):
        """A hung localhost call (timeout) -> running=False, no exception."""
        with patch(
            "urllib.request.urlopen",
            side_effect=TimeoutError("timed out"),
        ):
            result = _check_ollama_status()
        self.assertEqual(result, {"running": False, "models": []})

    def test_non_200_status_returns_not_running(self):
        """e.g. an HTTP 500 from the daemon -> running=False, no exception."""
        with patch("urllib.request.urlopen", return_value=_fake_response(500)):
            result = _check_ollama_status()
        self.assertEqual(result, {"running": False, "models": []})

    def test_malformed_json_returns_not_running(self):
        """Garbage body -> running=False, no exception."""

        class BadJson:
            status = 200

            def read(self):
                return b"this is not json{{{"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with patch("urllib.request.urlopen", return_value=BadJson()):
            result = _check_ollama_status()
        self.assertEqual(result, {"running": False, "models": []})

    def test_uses_localhost_with_short_timeout(self):
        """The probe must hit localhost:11434 and not wait long."""

        def fake_urlopen(req, timeout=None):
            self.assertEqual(req.full_url, "http://localhost:11434/api/tags")
            self.assertLessEqual(timeout, 5)
            return _fake_response(200, {"models": []})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _check_ollama_status()


if __name__ == "__main__":
    main(verbosity=2)
