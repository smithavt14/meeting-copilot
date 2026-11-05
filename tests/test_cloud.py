import base64
import io
import json
import urllib.error
import wave

import numpy as np
import pytest

from meeting_copilot import transcribe as tr

FAKE = "test-not-a-real-key"


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_request_shape_matches_openrouter_docs(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE)
    seen = {}

    def opener(req, timeout):
        seen["req"], seen["timeout"] = req, timeout
        return FakeResponse(json.dumps({"text": " hello there ", "usage": {"seconds": 1.0}}).encode())

    eng = tr.CloudOpenRouter(opener=opener)
    text = eng(np.zeros(16000, np.float32))
    assert text == "hello there"
    req = seen["req"]
    assert req.full_url == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == f"Bearer {FAKE}"
    assert req.get_header("Content-type") == "application/json"
    body = json.loads(req.data)
    assert body["model"] == tr.DEFAULT_CLOUD_MODEL
    assert body["language"] == "en"
    assert body["input_audio"]["format"] == "wav"
    raw = base64.b64decode(body["input_audio"]["data"])
    with wave.open(io.BytesIO(raw)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (16000, 1, 2, 16000)


def test_model_and_language_are_settings(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE)
    seen = {}
    eng = tr.CloudOpenRouter(model="openai/whisper-large-v3", language=None,
                             opener=lambda req, timeout: seen.setdefault("r", req) and FakeResponse(b'{"text": "x"}'))
    eng(np.zeros(100, np.float32))
    body = json.loads(seen["r"].data)
    assert body["model"] == "openai/whisper-large-v3" and "language" not in body


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        tr.CloudOpenRouter()


def test_http_error_names_the_status_and_not_the_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE)

    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"error":"bad key"}'))

    with pytest.raises(RuntimeError) as e:
        tr.CloudOpenRouter(opener=opener)(np.zeros(100, np.float32))
    assert "401" in str(e.value) and FAKE not in str(e.value)
