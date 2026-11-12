"""The platform seam, plus smoke tests that the real backends load on the OS the tests run on."""

import io
import os
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from meeting_copilot import cli, platforms
from meeting_copilot.models import KOKORO_FILES, cache_dir
from meeting_copilot.run import iso, parse_iso
from meeting_copilot.sources import LoopbackSource, MacTapSource, SilenceFill

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def test_pid_alive_for_this_process_and_not_for_an_exited_one():
    assert platforms.pid_alive(os.getpid())
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    # On Windows the Popen object still holds a handle, so the exited process can still be opened.
    assert not platforms.pid_alive(p.pid)
    assert not platforms.pid_alive(0) and not platforms.pid_alive(-1)


@pytest.mark.parametrize("plat, parts", [
    ("darwin", ("Library", "Application Support", "meeting-copilot")),
    ("win32", ("Roaming", "meeting-copilot")),
    ("linux", (".config", "meeting-copilot")),
])
def test_config_dir_per_os(monkeypatch, tmp_path, plat, parts):
    monkeypatch.setattr(sys, "platform", plat)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert platforms.config_dir().parts[-len(parts):] == parts


def test_cache_and_data_dirs_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert platforms.cache_dir() == tmp_path / "meeting-copilot" / "Cache"
    assert platforms.data_dir() == tmp_path / "meeting-copilot"


def test_call_source_per_os(monkeypatch):
    logs = []
    monkeypatch.setattr(sys, "platform", "darwin")
    assert isinstance(platforms.call_source("zoom.us", None, False, logs.append), MacTapSource)
    assert platforms.call_source(None, None, False, logs.append) is None
    with pytest.raises(ValueError, match="--loopback is for Windows"):
        platforms.call_source(None, None, True, logs.append)

    monkeypatch.setattr(sys, "platform", "win32")
    assert isinstance(platforms.call_source(None, None, True, logs.append), LoopbackSource)
    assert isinstance(platforms.call_source("zoom.us", None, False, logs.append), LoopbackSource)
    assert any("ignored" in m for m in logs)
    assert platforms.call_source(None, None, False, logs.append) is None

    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(ValueError):
        platforms.call_source(None, None, True, logs.append)


def test_tail_command_uses_the_flag_where_the_shell_has_no_env_prefix(monkeypatch):
    a = cli.parser().parse_args(["listen"])
    monkeypatch.setenv("COPILOT_OUT", "C:/runs")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert cli.tail_command(a) == 'COPILOT_OUT="C:/runs" copilot tail'
    monkeypatch.setattr(sys, "platform", "win32")
    assert cli.tail_command(a) == 'copilot --out "C:/runs" tail'


def test_ansi_tty_is_false_for_a_pipe_or_buffer():
    assert not platforms.ansi_tty(io.StringIO())


APPENDER = """
import sys
sys.path.insert(0, sys.argv[3])
from meeting_copilot.run import append_event
for i in range(int(sys.argv[2])):
    append_event(sys.argv[1], "line", who=sys.argv[4], i=i, text="x" * 200)
"""


def test_two_processes_appending_never_overwrite_each_other(tmp_path):
    import json

    path = tmp_path / "lines.jsonl"
    src = str(Path(__file__).resolve().parents[1] / "src")
    procs = [subprocess.Popen([sys.executable, "-c", APPENDER, str(path), "400", src, who]) for who in "ab"]
    with platforms.open_append(path) as f:  # this process too, the way the listener holds the file open
        for i in range(400):
            f.write(json.dumps({"ev": "line", "who": "c", "i": i}) + "\n")
            f.flush()
    assert all(p.wait(timeout=60) == 0 for p in procs)
    raw = path.read_bytes()
    assert b"\r" not in raw
    events = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    for who in "abc":
        assert sorted(e["i"] for e in events if e["who"] == who) == list(range(400))


def test_iso_near_the_epoch_round_trips():
    # Tests use small timestamps; a naive datetime's astimezone() fails on Windows for these.
    assert abs(parse_iso(iso(100.0)) - 100.0) < 0.001


def test_silence_fill_feeds_silence_only_in_a_gap():
    now = [0.0]
    got = []
    fill = SilenceFill(lambda x, t: got.append((len(x), t)), gap_s=0.3, step_s=0.1, clock=lambda: now[0])
    fill.audio(np.ones(1600, dtype=np.float32))
    now[0] = 0.2
    assert not fill.tick()
    now[0] = 0.35
    assert fill.tick() and got[-1] == (1600, 0.35)
    now[0] = 0.4
    assert not fill.tick()  # the next step is due 0.1 s after the last
    now[0] = 0.45
    assert fill.tick()
    fill.audio(np.ones(1600, dtype=np.float32))  # real audio again: the gap clock restarts
    now[0] = 0.6
    assert not fill.tick()
    assert [n for n, _ in got] == [1600, 1600, 1600, 1600]


def test_windows_speech_passes_text_on_stdin_and_the_path_in_the_environment(tmp_path):
    calls = []

    def run(args, input, check, capture_output, env):
        calls.append((args, input, env))

    wav = tmp_path / "it's here.wav"
    platforms.system_speech("Hi 'there'; Remove-Item x", wav, 16000, run=run, platform="win32")
    args, text, env = calls[0]
    assert args[0] == "powershell" and "Hi" not in " ".join(args) and str(wav) not in " ".join(args)
    assert text == "Hi 'there'; Remove-Item x".encode("utf-8")
    assert env["COPILOT_SPEECH_WAV"] == str(wav) and env["COPILOT_SPEECH_RATE"] == "16000"


def _system_speech_works() -> bool:
    if not platforms.has_system_voice():
        return False
    try:
        platforms.system_speech("ok", Path(__import__("tempfile").mkdtemp()) / "probe.wav", 16000)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


@pytest.mark.skipif(not _system_speech_works(), reason="no system voice on this machine")
def test_system_speech_writes_16k_mono_wav(tmp_path):
    wav = tmp_path / "ünïcode it's.wav"
    platforms.system_speech("Zażółć gęślą jaźń, hello.", wav, 16000)
    with wave.open(str(wav)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        assert w.getnframes() > 8000


@windows_only
def test_windows_backends_import():
    import pyaudiowpatch  # noqa: F401  WASAPI loopback
    import sherpa_onnx  # noqa: F401  Parakeet and the speaker model
    import sounddevice  # noqa: F401  the mic and playback; PortAudio ships in the wheel

    assert platforms.ansi_tty(io.StringIO()) is False


def test_espeak_ng_phonemizes():
    """Kokoro's phonemes come from espeak-ng, loaded from the espeakng-loader wheel on every OS."""
    from kokoro_onnx.tokenizer import Tokenizer

    assert Tokenizer().phonemize("hello world").strip()


@pytest.mark.skipif(not all((cache_dir() / "kokoro-v1.0" / f).exists() for f in KOKORO_FILES),
                    reason="needs the Kokoro model (copilot download)")
def test_kokoro_makes_audio():
    from meeting_copilot.voice import KokoroVoice

    chunks = list(KokoroVoice().chunks("Two hundred and twelve."))
    assert chunks and chunks[0][1] == 24000 and len(chunks[0][0]) > 2400
