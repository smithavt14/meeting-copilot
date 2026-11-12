"""End to end on generated speech: the system voice (macOS ``say``, Windows System.Speech) makes the audio, the real
Silero VAD and Parakeet transcribe it. Skipped where the voice or the downloaded models are missing (run
``copilot download`` first). The two-voice diarize test needs macOS ``say`` and its named voices."""

import json
import shutil
import subprocess
import sys
import wave

import pytest

from meeting_copilot import cli, platforms
from meeting_copilot.models import PARAKEET_FILES, PARAKEET_NAME, cache_dir

pytestmark = pytest.mark.skipif(
    not platforms.has_system_voice() or not all((cache_dir() / PARAKEET_NAME / f).exists() for f in PARAKEET_FILES)
    or not (cache_dir() / "silero_vad.onnx").exists(),
    reason="needs a system voice and the downloaded models",
)

SENTENCES = ["The weather report says it will rain on Saturday.", "Claude, how many days are in a leap year?"]


def make_wav(path):
    rate, gap = 16000, b"\x00\x00" * 16000
    frames = [gap]
    for i, s in enumerate(SENTENCES):
        part = path.parent / f"part{i}.wav"
        platforms.system_speech(s, part, rate)
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


TALK = [("Samantha", "Good morning, I wanted to walk through the budget for next quarter before lunch."),
        ("Daniel", "Sure, I looked at the numbers last night and the travel line looks too high to me."),
        ("Samantha", "That is fair, we can cut the conference trips and keep the customer visits."),
        ("Daniel", "Then I will rewrite the plan this afternoon and send it to everyone by Friday."),
        ("Samantha", "Great, and let us keep the hiring plan exactly as it is for now."),
        ("Daniel", "Agreed, I think that covers everything we needed to decide today.")]


@pytest.mark.skipif(shutil.which("say") is None, reason="needs macOS say and its named voices")
@pytest.mark.skipif(not (cache_dir() / __import__("meeting_copilot.models", fromlist=["x"]).SPEAKER_MODEL).exists(),
                    reason="needs the speaker model (copilot download --diarize)")
def test_listen_file_diarize_labels_two_voices(tmp_path):
    rate, gap = 16000, b"\x00\x00" * 16000
    frames = [gap]
    for i, (voice, text) in enumerate(TALK):
        part = tmp_path / f"v{i}.wav"
        subprocess.run(["say", "-v", voice, "-o", str(part), f"--data-format=LEI16@{rate}", text], check=True)
        with wave.open(str(part)) as w:
            frames += [w.readframes(w.getnframes()), gap]
    wav = tmp_path / "two.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(b"".join(frames))
    out = tmp_path / "out"
    assert cli.main(["--out", str(out), "listen", "--file", str(wav), "--as", "me", "--speed", "0",
                     "--draft-ms", "0", "--diarize"]) == 0
    events = [json.loads(l) for l in next(out.glob("*/lines.jsonl")).read_text().splitlines()]
    lines = [e for e in events if e["ev"] == "line"]
    relabel = next((e["lines"] for e in events if e["ev"] == "relabel"), {})
    labels = [relabel.get(str(l["n"]), l["speaker"]) for l in lines]
    assert len(lines) == len(TALK)
    assert all(l["stream"] == "me" for l in lines)
    assert labels[0::2] == [labels[0]] * 3 and labels[1::2] == [labels[1]] * 3 and labels[0] != labels[1]
