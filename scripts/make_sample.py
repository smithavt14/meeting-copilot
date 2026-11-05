"""Make a sample meeting WAV from invented sentences with macOS ``say``, for ``copilot listen --file``.

    uv run python scripts/make_sample.py sample.wav
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import wave
from pathlib import Path

SENTENCES = [
    "Good morning everyone, thanks for joining the planning call.",
    "The quarterly numbers came in a little higher than we planned.",
    "Claude, what is the boiling point of water at sea level in Fahrenheit, and how does it change at high altitude?",
    "Let's move the design review to Thursday afternoon and keep the scope small.",
    "I can send the updated budget by the end of the week.",
    "Claude, remind me what we decided about the launch date.",
]
GAP_S = 1.2
RATE = 16000


def say(text: str, path: Path) -> bytes:
    subprocess.run(["say", "-o", str(path), f"--data-format=LEI16@{RATE}", text], check=True)
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


def main(out: str) -> None:
    gap = b"\x00\x00" * int(GAP_S * RATE)
    with tempfile.TemporaryDirectory() as tmp:
        frames = [gap]
        for i, s in enumerate(SENTENCES):
            frames += [say(s, Path(tmp) / f"{i}.wav"), gap]
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(frames))
    print(out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sample.wav")
