"""Synthetic audio and a stand-in VAD for the tests: no recordings, no models."""

from __future__ import annotations

import numpy as np

RATE = 16000


def tone(seconds: float, freq: float = 220.0, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * RATE), dtype=np.float32)


def energy_vad(chunk: np.ndarray) -> float:
    """Speech probability 1 for a loud chunk, 0 for a quiet one."""
    return 1.0 if float(np.sqrt(np.mean(chunk ** 2))) > 0.01 else 0.0


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t
