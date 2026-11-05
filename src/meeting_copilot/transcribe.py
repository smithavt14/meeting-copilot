"""Transcription engines. Each is a callable: 16 kHz mono float32 samples in, text out.

- ``local`` (default): NVIDIA Parakeet TDT 0.6B v3 through sherpa-onnx, on the CPU.
- ``cloud``: OpenRouter's ``POST /api/v1/audio/transcriptions``, one WAV per call, key from OPENROUTER_API_KEY.
"""

from __future__ import annotations

import base64
import io
import json
import os
import urllib.error
import urllib.request
import wave

import numpy as np

from meeting_copilot.vad import RATE

OPENROUTER_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
DEFAULT_CLOUD_MODEL = "openai/whisper-large-v3-turbo"


class LocalParakeet:
    def __init__(self, threads: int = 0) -> None:
        import sherpa_onnx

        from meeting_copilot.models import parakeet_dir

        d = parakeet_dir()
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(d / "encoder.int8.onnx"),
            decoder=str(d / "decoder.int8.onnx"),
            joiner=str(d / "joiner.int8.onnx"),
            tokens=str(d / "tokens.txt"),
            num_threads=threads or max(1, min(4, (os.cpu_count() or 2) // 2)),
            model_type="nemo_transducer",
            decoding_method="greedy_search",
        )
        self(np.zeros(RATE, dtype=np.float32))  # warm up so the first utterance does not pay for it

    def __call__(self, samples: np.ndarray) -> str:
        s = self.recognizer.create_stream()
        s.accept_waveform(RATE, samples.astype(np.float32))
        self.recognizer.decode_stream(s)
        return s.result.text.strip()


def wav_bytes(samples: np.ndarray, rate: int = RATE) -> bytes:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def openrouter_request(samples: np.ndarray, model: str, key: str, language: str | None) -> urllib.request.Request:
    """The JSON request OpenRouter's speech-to-text guide documents: base64 audio under ``input_audio``."""
    body: dict = {
        "model": model,
        "input_audio": {"data": base64.b64encode(wav_bytes(samples)).decode("ascii"), "format": "wav"},
    }
    if language:
        body["language"] = language
    return urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "X-Title": "meeting-copilot"},
        method="POST",
    )


class CloudOpenRouter:
    def __init__(self, model: str = DEFAULT_CLOUD_MODEL, language: str | None = "en", timeout_s: float = 30.0,
                 opener=urllib.request.urlopen) -> None:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise RuntimeError("cloud transcription needs OPENROUTER_API_KEY in the environment")
        self._key = key
        self.model = model
        self.language = language
        self.timeout_s = timeout_s
        self._open = opener

    def __call__(self, samples: np.ndarray) -> str:
        req = openrouter_request(samples, self.model, self._key, self.language)
        try:
            with self._open(req, timeout=self.timeout_s) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"OpenRouter returned {e.code}: {detail}") from None
        return str(data.get("text") or "").strip()


def make_engine(kind: str, cloud_model: str = DEFAULT_CLOUD_MODEL, language: str | None = "en"):
    if kind == "local":
        return LocalParakeet()
    if kind == "cloud":
        return CloudOpenRouter(cloud_model, language)
    raise ValueError(f"unknown transcription setting {kind!r} (local or cloud)")
