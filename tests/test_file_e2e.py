"""End to end on generated speech: macOS ``say`` makes the audio, the real Silero VAD and Parakeet transcribe it.
Skipped where ``say`` or the downloaded models are missing (run ``copilot download`` first)."""

import json
import shutil
import subprocess
import sys
import wave

import pytest

from meeting_copilot import cli
from meeting_copilot.models import PARAKEET_FILES, PARAKEET_NAME, cache_dir

pytestmark = pytest.mark.skipif(
    shutil.which("say") is None or not all((cache_dir() / PARAKEET_NAME / f).exists() for f in PARAKEET_FILES)
    or not (cache_dir() / "silero_vad.onnx").exists(),
    reason="needs macOS say and the downloaded models",
)

SENTENCES = ["The weather report says it will rain on Saturday.", "Claude, how many days are in a leap year?"]


def make_wav(path):
    rate, gap = 16000, b"\x00\x00" * 16000
    frames = [gap]
    for i, s in enumerate(SENTENCES):
        part = path.parent / f"part{i}.wav"
        subprocess.run(["say", "-o", str(part), f"--data-format=LEI16@{rate}", s], check=True)
        with wave.open(str(part)) as w:
            frames += [w.readframes(w.getnframes()), gap]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(b"".join(frames))


def test_listen_file_writes_lines_and_drafts(tmp_path, monkeypatch, capsys):
    wav = tmp_path / "talk.wav"
    make_wav(wav)
    out = tmp_path / "out"
    assert cli.main(["--out", str(out), "listen", "--file", str(wav), "--as", "me"]) == 0
    events = [json.loads(l) for l in next(out.glob("*/lines.jsonl")).read_text().splitlines()]
    lines = [e for e in events if e["ev"] == "line"]
    drafts = [e for e in events if e["ev"] == "draft"]
    assert [l["n"] for l in lines] == [1, 2]
    assert all(l["speaker"] == "me" for l in lines)
    assert "rain" in lines[0]["text"].lower() and "leap year" in lines[1]["text"].lower()
    assert lines[1]["named"] and not lines[0]["named"]
    assert drafts and all(d["d"] == i + 1 for i, d in enumerate(drafts))
    assert events[-1]["ev"] == "stop"
    assert all(l["latency_ms"] < 2000 for l in lines)
    assert not (out / "current.json").exists()
