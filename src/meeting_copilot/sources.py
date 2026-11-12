"""Audio sources. Each delivers 16 kHz mono float32 chunks to a callback: ``on_audio(samples, wall_end)``.

- ``MicSource``: the microphone, through sounddevice (both OSes).
- ``MacTapSource``: one app's audio on macOS 14.4+, through the ``tap`` Swift binary (Core Audio process tap).
- ``LoopbackSource``: everything the speakers play on Windows, through WASAPI loopback (PyAudioWPatch).
- ``FileSource``: a WAV file played into the pipeline at real time (or faster), for tests.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from meeting_copilot.vad import RATE

OnAudio = Callable[[np.ndarray, float], None]
Log = Callable[[str], None]


class Resampler:
    """Any rate and channel count to 16 kHz mono, keeping filter state across chunks."""

    def __init__(self, rate: int, channels: int) -> None:
        self.rate = int(rate)
        self.channels = int(channels)
        self._rs = None
        if self.rate != RATE:
            import soxr

            self._rs = soxr.ResampleStream(self.rate, RATE, 1, dtype="float32")

    def __call__(self, x: np.ndarray, last: bool = False) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if x.ndim == 2:
            x = x.mean(axis=1)
        elif self.channels > 1:
            x = x.reshape(-1, self.channels).mean(axis=1)
        if self._rs is None:
            return x
        return self._rs.resample_chunk(x, last=last)


def read_wav(path: str | Path) -> tuple[np.ndarray, int, int]:
    """Samples as float32 (frames x channels), sample rate, channels. 16-bit, 24-bit, 32-bit PCM."""
    with wave.open(str(path), "rb") as w:
        rate, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768
    elif width == 4:
        x = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648
    elif width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3)
        v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        x = v.astype(np.float32) / (1 << 23)
    elif width == 1:
        x = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128) / 128
    else:
        raise ValueError(f"unsupported WAV sample width {width}")
    return x.reshape(-1, ch), rate, ch


class FileSource:
    def __init__(self, path: str | Path, speed: float = 1.0, tail_s: float = 1.5) -> None:
        self.path = Path(path)
        self.speed = speed
        self.tail_s = tail_s  # silence after the file, so its last utterance ends as it would live
        self.done = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, on_audio: OnAudio, log: Log) -> None:
        x, rate, ch = read_wav(self.path)
        audio = Resampler(rate, ch)(x, last=True)
        audio = np.concatenate([audio, np.zeros(int(self.tail_s * RATE), dtype=np.float32)])
        self._thread = threading.Thread(target=self._run, args=(audio, on_audio), daemon=True)
        self._thread.start()

    def _run(self, audio: np.ndarray, on_audio: OnAudio) -> None:
        step = RATE // 10  # 100 ms
        t0 = time.time()
        for i in range(0, len(audio), step):
            if self._stop.is_set():
                break
            chunk = audio[i:i + step]
            if self.speed > 0:
                due = t0 + (i + len(chunk)) / RATE / self.speed
                wait = due - time.time()
                if wait > 0:
                    time.sleep(wait)
            on_audio(chunk, time.time())
        self.done.set()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


class MicSource:
    def __init__(self, device: str | int | None = None) -> None:
        self.device = device
        self._stream = None
        self.done = threading.Event()  # never set: a mic runs until stopped

    def start(self, on_audio: OnAudio, log: Log) -> None:
        import sounddevice as sd

        device = self.device
        if isinstance(device, str) and device.isdigit():
            device = int(device)
        info = sd.query_devices(device, "input")
        rate = RATE
        try:
            sd.check_input_settings(device=device, samplerate=RATE, channels=1, dtype="float32")
        except Exception:
            rate = int(info["default_samplerate"])
        resample = Resampler(rate, 1)

        def callback(indata, frames, t, status):
            on_audio(resample(indata[:, 0].copy()), time.time())

        self._stream = sd.InputStream(device=device, samplerate=rate, channels=1, dtype="float32",
                                      blocksize=int(rate * 0.1), callback=callback)
        self._stream.start()
        log(f"mic: {info['name']}")

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


def find_tap() -> Path | None:
    """COPILOT_TAP, else the binary built in this checkout, else ``copilot-tap`` on the PATH."""
    env = os.environ.get("COPILOT_TAP")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parents[2] / "tap"
    for p in (here / "build" / "tap", here / ".build" / "release" / "tap"):
        if p.exists():
            return p
    found = shutil.which("copilot-tap")
    return Path(found) if found else None


class MacTapSource:
    """Runs ``tap --app <name>`` (or ``--pid``) and reads 16 kHz mono 16-bit PCM from its stdout."""

    def __init__(self, app: str | None = None, pid: int | None = None) -> None:
        self.app = app
        self.pid = pid
        self.proc: subprocess.Popen | None = None
        self.done = threading.Event()
        self.got_audio = threading.Event()
        self.errors: list[str] = []

    def start(self, on_audio: OnAudio, log: Log) -> None:
        tap = find_tap()
        if tap is None:
            raise RuntimeError("the tap binary is not built; run scripts/build-tap.sh")
        args = [str(tap), "--pid", str(self.pid)] if self.pid else [str(tap), "--app", str(self.app)]
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._read, args=(on_audio,), daemon=True).start()
        err = threading.Thread(target=self._errors, args=(log,), daemon=True)
        err.start()
        for _ in range(10):  # an app name that matches nothing, or a missing binary, fails at once
            if self.proc.poll() is not None:
                err.join(timeout=1)
                raise RuntimeError(f"tap exited: {self.errors[-1] if self.errors else self.proc.returncode}")
            time.sleep(0.1)

    def _read(self, on_audio: OnAudio) -> None:
        assert self.proc and self.proc.stdout
        step = RATE // 10 * 2  # 100 ms of 16-bit samples
        while True:
            data = self.proc.stdout.read(step)
            if not data:
                break
            if len(data) % 2:
                data = data[:-1]
            self.got_audio.set()
            on_audio(np.frombuffer(data, "<i2").astype(np.float32) / 32768, time.time())
        self.done.set()

    def _errors(self, log: Log) -> None:
        assert self.proc and self.proc.stderr
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self.errors.append(line)
                log(f"tap: {line}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class SilenceFill:
    """Feeds silence into a stream whose source has gone quiet. WASAPI loopback delivers no buffers at all while
    nothing plays, so without this an utterance that ends with the call going silent would never see the 500 ms of
    silence that closes it, and would wait for the next sound."""

    def __init__(self, on_audio: OnAudio, gap_s: float = 0.3, step_s: float = 0.1, clock=time.time) -> None:
        self.on_audio = on_audio
        self.gap_s = gap_s
        self.step_s = step_s
        self.clock = clock
        self.fed = clock()  # the time up to which the stream has audio, real or filled
        self.filling = False
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def audio(self, samples: np.ndarray) -> None:
        """Real audio from the source: passed on, and the gap clock restarts."""
        with self.lock:
            now = self.clock()
            self.on_audio(samples, now)
            self.fed = now
            self.filling = False

    def tick(self) -> bool:
        """Once the source has been quiet for ``gap_s``, feeds silence for the whole time since its last audio, then
        keeps up every ``step_s``, so the stream sees a pause as long as it really was. True when it fed some."""
        with self.lock:
            now = self.clock()
            due = self.step_s if self.filling else self.gap_s
            if now - self.fed < due:
                return False
            n = int(round((now - self.fed) * RATE))
            self.on_audio(np.zeros(n, dtype=np.float32), now)
            self.fed += n / RATE
            self.filling = True
            return True

    def start(self) -> None:
        def run() -> None:
            while not self._stop.wait(self.step_s / 2):
                self.tick()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)


class LoopbackSource:
    """Windows: WASAPI loopback of the default output device. It hears every app, notifications included."""

    def __init__(self) -> None:
        self._pa = None
        self._stream = None
        self._fill: SilenceFill | None = None
        self.done = threading.Event()

    def start(self, on_audio: OnAudio, log: Log) -> None:
        import pyaudiowpatch as pyaudio

        pa = pyaudio.PyAudio()
        self._pa = pa
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if not speakers.get("isLoopbackDevice"):
            for dev in pa.get_loopback_device_info_generator():
                if speakers["name"] in dev["name"]:
                    speakers = dev
                    break
            else:
                raise RuntimeError("no WASAPI loopback device for the default output")
        rate, ch = int(speakers["defaultSampleRate"]), int(speakers["maxInputChannels"])
        resample = Resampler(rate, ch)
        fill = self._fill = SilenceFill(on_audio)

        def callback(in_data, frame_count, time_info, status):
            fill.audio(resample(np.frombuffer(in_data, dtype=np.float32)))
            return (None, pyaudio.paContinue)

        self._stream = pa.open(format=pyaudio.paFloat32, channels=ch, rate=rate, input=True,
                               input_device_index=speakers["index"], frames_per_buffer=int(rate * 0.1),
                               stream_callback=callback)
        self._stream.start_stream()
        fill.start()
        log(f"loopback: {speakers['name']}")

    def stop(self) -> None:
        if self._fill is not None:
            self._fill.stop()
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
        if self._pa is not None:
            self._pa.terminate()


