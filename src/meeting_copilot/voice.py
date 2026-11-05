"""``copilot say``: speak a short answer. Engines, picked in this order unless the ``voice`` setting names one:

- ``elevenlabs`` when ELEVENLABS_API_KEY is set: the streaming text-to-speech endpoint, 24 kHz PCM.
- ``kokoro`` (the default otherwise): Kokoro-82M through kokoro-onnx, on the CPU, voice ``af_heart``.
- ``system``: ``say`` on macOS, System.Speech through PowerShell on Windows.

Every engine yields float32 audio chunks that play through sounddevice. An engine that fails before any audio has
played hands over to the next one in the order.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

Chunks = Iterator[tuple[np.ndarray, int]]

ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream?output_format=pcm_24000"
DEFAULT_ELEVENLABS_VOICE = "JBFqnCBsd6RMkjVDRZzb"  # the voice in ElevenLabs' own API examples
DEFAULT_ELEVENLABS_MODEL = "eleven_flash_v2_5"
DEFAULT_KOKORO_VOICE = "af_heart"
ORDER = ["elevenlabs", "kokoro", "system"]


class Unavailable(Exception):
    """The engine cannot run here (no key, no package, wrong OS)."""


# Engines

class ElevenLabs:
    name = "elevenlabs"

    def __init__(self, voice: str = DEFAULT_ELEVENLABS_VOICE, model: str = DEFAULT_ELEVENLABS_MODEL,
                 opener=urllib.request.urlopen, timeout_s: float = 20.0) -> None:
        key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if not key:
            raise Unavailable("ELEVENLABS_API_KEY is not set")
        self._key = key
        self.voice = voice
        self.model = model
        self._open = opener
        self.timeout_s = timeout_s

    def request(self, text: str) -> urllib.request.Request:
        return urllib.request.Request(
            ELEVENLABS_URL.format(voice=self.voice),
            data=json.dumps({"text": text, "model_id": self.model}).encode("utf-8"),
            headers={"xi-api-key": self._key, "Content-Type": "application/json", "Accept": "audio/pcm"},
            method="POST",
        )

    def chunks(self, text: str) -> Chunks:
        try:
            r = self._open(self.request(text), timeout=self.timeout_s)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"ElevenLabs returned {e.code}: {detail}") from None
        with r:
            rest = b""
            while data := r.read(4800):  # 100 ms of 24 kHz 16-bit audio
                data = rest + data
                cut = len(data) - len(data) % 2
                rest = data[cut:]
                if cut:
                    yield np.frombuffer(data[:cut], "<i2").astype(np.float32) / 32768, 24000


class KokoroVoice:
    name = "kokoro"

    def __init__(self, voice: str = DEFAULT_KOKORO_VOICE, speed: float = 1.0, lang: str = "en-us") -> None:
        try:
            from kokoro_onnx import Kokoro
        except ImportError as e:
            raise Unavailable(f"kokoro-onnx is not installed ({e})") from None
        from meeting_copilot.models import kokoro_paths

        model, voices = kokoro_paths()
        self.kokoro = Kokoro(str(model), str(voices))
        self.voice = voice
        self.speed = speed
        self.lang = lang

    def chunks(self, text: str) -> Chunks:
        """One chunk per sentence, made on a thread one sentence ahead, so the first plays while the rest are made."""
        import queue
        import threading

        q: queue.Queue = queue.Queue(maxsize=2)

        def make() -> None:
            try:
                for sentence in sentences(text):
                    samples, rate = self.kokoro.create(sentence, voice=self.voice, speed=self.speed, lang=self.lang)
                    q.put((np.asarray(samples, dtype=np.float32), int(rate)))
            except Exception as e:
                q.put(e)
            q.put(None)

        threading.Thread(target=make, daemon=True).start()
        while (item := q.get()) is not None:
            if isinstance(item, Exception):
                raise item
            yield item


def sentences(text: str, max_chars: int = 160) -> list[str]:
    """Sentences, with a long one split at its commas, so the first piece is short and plays soon."""
    import re

    out: list[str] = []
    for sent in re.split(r"(?<=[.!?])\s+", text.strip()):
        while len(sent) > max_chars and "," in sent[:max_chars]:
            cut = sent.rindex(",", 0, max_chars) + 1
            out.append(sent[:cut].strip())
            sent = sent[cut:].strip()
        if sent:
            out.append(sent)
    return out


class SystemVoice:
    name = "system"

    def __init__(self, run=subprocess.run, platform: str = sys.platform) -> None:
        if platform not in ("darwin", "win32"):
            raise Unavailable("the system voice is macOS say or Windows System.Speech")
        self._run = run
        self.platform = platform

    def chunks(self, text: str) -> Chunks:
        from meeting_copilot.sources import read_wav

        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "said.wav"
            if self.platform == "darwin":
                self._run(["say", "-o", str(wav), "--data-format=LEI16@22050", "-f", "-"], input=text.encode("utf-8"),
                          check=True, capture_output=True)
            else:
                script = ("Add-Type -AssemblyName System.Speech; "
                          "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                          f"$s.SetOutputToWaveFile('{wav}'); $s.Speak([Console]::In.ReadToEnd()); $s.Dispose()")
                self._run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          input=text.encode("utf-8"), check=True, capture_output=True)
            x, rate, ch = read_wav(wav)
        yield x.mean(axis=1).astype(np.float32), rate


def factories(s: dict) -> dict[str, Callable[[], object]]:
    return {
        "elevenlabs": lambda: ElevenLabs(s.get("elevenlabs_voice") or DEFAULT_ELEVENLABS_VOICE,
                                         s.get("elevenlabs_model") or DEFAULT_ELEVENLABS_MODEL),
        "kokoro": lambda: KokoroVoice(s.get("kokoro_voice") or DEFAULT_KOKORO_VOICE, float(s.get("voice_speed") or 1.0)),
        "system": lambda: SystemVoice(),
    }


def order(choice: str | None) -> list[str]:
    """The engines to try: the chosen one first, then the ones after it in ORDER. Auto starts at the top."""
    if not choice or choice == "auto":
        return list(ORDER)
    if choice not in ORDER:
        raise ValueError(f"unknown voice {choice!r} (elevenlabs, kokoro or system)")
    return ORDER[ORDER.index(choice):]


# Playback

class Player:
    """Plays float32 chunks through sounddevice, one output stream per sample rate."""

    def __init__(self) -> None:
        import sounddevice as sd

        self.sd = sd
        self.stream = None
        self.rate = None

    def play(self, samples: np.ndarray, rate: int) -> None:
        if self.stream is None or rate != self.rate:
            self.close()
            self.stream = self.sd.OutputStream(samplerate=rate, channels=1, dtype="float32")
            self.stream.start()
            self.rate = rate
        self.stream.write(np.ascontiguousarray(samples.reshape(-1, 1)))

    def close(self) -> None:
        if self.stream is not None:
            self.stream.stop()  # waits for the buffer to drain
            self.stream.close()
            self.stream = None


def speak(text: str, engines: list[str], make: dict[str, Callable[[], object]], player,
          on_start: Callable[[str, float], None] = lambda engine, t: None,
          log: Callable[[str], None] = lambda s: None, clock: Callable[[], float] = time.time,
          explicit: bool = True) -> dict:
    """Speaks ``text`` with the first engine that works (``explicit``: the first engine was asked for by name). Returns the engine, the time of first audio and the end."""
    t0 = clock()
    last_error = None
    for name in engines:
        try:
            engine = make[name]()
        except Unavailable as e:
            if explicit or name != engines[0]:  # auto mode skips a missing ElevenLabs key quietly
                log(f"{name}: {e}")
            continue
        except Exception as e:
            log(f"{name}: cannot start: {e}")
            last_error = e
            continue
        first = None
        try:
            for samples, rate in engine.chunks(text):
                if first is None:
                    first = clock()
                    on_start(name, first)
                player.play(samples, rate)
            player.close()
        except Exception as e:
            if first is not None:
                raise  # audio already played: a second voice would repeat it
            log(f"{name}: {e}")
            last_error = e
            continue
        if first is None:
            first = clock()
            on_start(name, first)
        return {"engine": name, "first_audio_ms": int((first - t0) * 1000), "start": first, "end": clock()}
    raise RuntimeError(f"no voice could speak{f' ({last_error})' if last_error else ''}")
