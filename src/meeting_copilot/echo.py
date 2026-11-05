"""Speaker-echo backstop. On laptop speakers the mic hears the call, so a call line can come back as a "me" line.
A "me" line whose words mostly repeat, in order, a call line from the last few seconds is dropped.

The mic often finishes its copy of a fragment before the call stream finishes the whole sentence, so a "me" line
is held while a call utterance is open or waiting for its final, and released when the call's finals are in, or
after ``hold_s``."""

from __future__ import annotations

import re
from collections import deque
from difflib import SequenceMatcher

WORD = re.compile(r"[a-z0-9']+")
SHORT = 3  # a line under this many words is echo only as an exact run of call words heard just before


def tokens(text: str) -> list[str]:
    return WORD.findall(text.lower())


def match_ratio(heard: list[str], played: list[str]) -> float:
    """Share of the heard words that line up, in order, with the played ones."""
    if not heard or not played:
        return 0.0
    m = SequenceMatcher(None, heard, played, autojunk=False)
    return sum(b.size for b in m.get_matching_blocks()) / len(heard)


def contains_run(heard: list[str], played: list[str]) -> bool:
    n = len(heard)
    return any(played[i:i + n] == heard for i in range(len(played) - n + 1))


class EchoFilter:
    """Remembers recent call lines and says whether a "me" line repeats one."""

    def __init__(self, window_s: float = 8.0, threshold: float = 0.7, short_window_s: float = 2.0) -> None:
        self.window_s = window_s
        self.threshold = threshold
        self.short_window_s = short_window_s
        self._played: deque[tuple[float, float, list[str]]] = deque()  # (start, end, words)

    def played(self, start: float, end: float, text: str) -> None:
        self._played.append((start, end, tokens(text)))

    def is_echo(self, text: str, start: float, end: float) -> tuple[bool, float]:
        heard = tokens(text)
        cutoff = start - self.window_s
        while self._played and self._played[0][1] < cutoff:
            self._played.popleft()
        refs = [(e, w) for s, e, w in self._played if s <= end]
        if not heard:
            return False, 0.0
        if len(heard) < SHORT:
            # "yes" after a long call sentence that contains it is a real reply; "to ninety" right after
            # "get to ninety" is echo.
            hit = any(e >= start - self.short_window_s and contains_run(heard, w) for e, w in refs)
            return hit, 1.0 if hit else 0.0
        best = max((match_ratio(heard, w) for _, w in refs), default=0.0)
        return best >= self.threshold, best


class EchoGate:
    """Orders the two streams for the filter: holds "me" finals while the call stream is mid-utterance."""

    def __init__(self, window_s: float = 8.0, threshold: float = 0.7, hold_s: float = 8.0) -> None:
        self.echo = EchoFilter(window_s, threshold)
        self.hold_s = hold_s
        self.call_open = False
        self.call_pending = 0  # call utterances ended and not yet transcribed
        self._held: list[tuple[float, dict]] = []

    @property
    def busy(self) -> bool:
        return self.call_open or self.call_pending > 0

    def call_speech(self) -> None:
        self.call_open = True

    def call_speech_end(self) -> None:
        self.call_open = False
        self.call_pending += 1

    def call_final(self, start: float, end: float, text: str) -> list[dict]:
        """A call final arrived (or an empty one, with ``text`` ""). Returns held "me" lines now free to check."""
        if text:
            self.echo.played(start, end, text)
        self.call_pending = max(0, self.call_pending - 1)
        return [] if self.busy else self._release()

    def me_final(self, line: dict, now: float) -> list[dict]:
        self._held.append((now, line))
        return [] if self.busy else self._release()

    def tick(self, now: float) -> list[dict]:
        if self._held and now - self._held[0][0] > self.hold_s:
            return self._release()  # never wait on a long call turn forever
        return []

    def flush(self) -> list[dict]:
        return self._release()

    def _release(self) -> list[dict]:
        out = [line for _, line in self._held]
        self._held = []
        return out
