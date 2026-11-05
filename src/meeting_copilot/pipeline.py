"""One audio stream ("me" or "call"): the VAD cuts utterances, each utterance gets one final transcription, and
while it is open the audio so far is re-transcribed every ``draft_s`` seconds for a draft.

Scheduling rules, shared by both streams through one ``Scheduler``:

- Only one transcription call runs at a time.
- Finals come first. A final counts as waiting from its call until it returns, and no draft starts while a final
  waits or another draft runs, so a final queues behind one draft call at most.
- A draft starts only while the speech is still going (under ``DRAFT_QUIET_S`` of trailing silence), so it has
  ``silence_s - DRAFT_QUIET_S`` to finish before the cut.
- A draft that returns after its utterance ended is dropped, so no draft is written after its utterance's end.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

from meeting_copilot.vad import CHUNK, RATE

DRAFT_QUIET_S = 0.25
MIN_CALL_S = 1.0  # shorter audio is padded with silence before a transcription call

Engine = Callable[[np.ndarray], str]


class Scheduler:
    """The one transcription slot both streams share."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._call = threading.Lock()  # held for the length of one engine call
        self._gate = threading.Lock()  # guards the counters below
        self.finals_waiting = 0
        self.draft_busy = False
        self.max_concurrent = 0  # for tests: the most calls seen running at once
        self._running = 0
        # While muted, finals still run (the listener has to hear "unmute") but drafts do not. With a cloud engine,
        # ``muted_engine`` (local Parakeet, when installed) takes the muted finals; without one, only utterances up
        # to ``muted_max_s`` are sent.
        self.muted = False
        self.muted_engine: Engine | None = None
        self.muted_max_s: float | None = None

    def _run(self, samples: np.ndarray, engine: Engine | None = None) -> str:
        audio = samples.astype(np.float32)
        if len(audio) < int(MIN_CALL_S * RATE):
            audio = np.concatenate([audio, np.zeros(int(MIN_CALL_S * RATE) - len(audio), dtype=np.float32)])
        with self._call:
            with self._gate:
                self._running += 1
                self.max_concurrent = max(self.max_concurrent, self._running)
            try:
                return (engine or self.engine)(audio).strip()
            finally:
                with self._gate:
                    self._running -= 1

    def final(self, samples: np.ndarray) -> tuple[str, bool]:
        """One utterance's final, and whether a draft was running when it came in (it then waited for it)."""
        engine = None
        if self.muted:
            if self.muted_engine is not None:
                engine = self.muted_engine
            elif self.muted_max_s is not None and len(samples) > self.muted_max_s * RATE:
                return "", False  # too long to be "Claude, unmute"; not sent
        with self._gate:
            self.finals_waiting += 1
            behind = self.draft_busy
        try:
            return self._run(samples, engine), behind
        finally:
            with self._gate:
                self.finals_waiting -= 1

    def claim_draft(self) -> bool:
        """Takes the one draft slot: False while a final waits or another draft runs (try again later)."""
        with self._gate:
            if self.muted or self.finals_waiting > 0 or self.draft_busy:
                return False
            self.draft_busy = True
            return True

    def draft(self, samples: np.ndarray) -> str:
        """A draft, after ``claim_draft`` returned True; releases the slot."""
        try:
            return self._run(samples)
        finally:
            with self._gate:
                self.draft_busy = False


def has_text(text: str) -> bool:
    return any(c.isalnum() for c in text)


@dataclass
class Final:
    stream: str
    utt: int
    text: str
    start: float  # wall clock, seconds
    end: float
    emit: float
    drafts: int
    behind_draft: bool


@dataclass
class Draft:
    stream: str
    utt: int
    text: str
    start: float
    emit: float
    audio_ms: int
    ms: int


@dataclass
class Handlers:
    speech: Callable[[str, int, float], None] = lambda stream, utt, t: None
    speech_end: Callable[[str, int, float], None] = lambda stream, utt, t: None
    draft: Callable[[Draft], None] = lambda d: None
    final: Callable[[Final], None] = lambda f: None
    empty: Callable[[str, int], None] = lambda stream, utt: None
    error: Callable[[str], None] = lambda msg: None


@dataclass
class _Segment:
    utt: int
    samples: np.ndarray
    start_wall: float
    end_wall: float


class Stream:
    """Feed 16 kHz mono float32 audio with ``feed``; finals and drafts come back through ``handlers``."""

    def __init__(self, name: str, vad: Callable[[np.ndarray], float], scheduler: Scheduler, handlers: Handlers,
                 threshold: float = 0.5, silence_s: float = 0.5, pre_roll_s: float = 0.3, max_utt_s: float = 30.0,
                 draft_s: float = 1.0, clock: Callable[[], float] = time.time, sync: bool = False) -> None:
        self.name = name
        self.vad = vad
        self.scheduler = scheduler
        self.h = handlers
        self.threshold = threshold
        self.silence_s = silence_s
        self.pre_roll_s = pre_roll_s
        self.max_utt_s = max_utt_s
        self.draft_s = draft_s
        self.clock = clock
        self.sync = sync  # tests: run finals inline and drafts on threads that ``wait_drafts`` can join

        self._pending = np.zeros(0, dtype=np.float32)
        self._ring: list[np.ndarray] = []  # recent chunks for the pre-roll
        self._ring_n = int(pre_roll_s * RATE / CHUNK) + 1
        self.pos = 0.0  # stream seconds the VAD has decided through
        self._wall_anchor = 0.0
        self._pos_anchor = 0.0
        self._fed = 0  # samples fed

        self.in_speech = False
        self.speech_start = 0.0
        self.last_voiced_end = 0.0
        self._utt_samples: list[np.ndarray] = []
        self.utt = 0
        self._last_draft_at = 0.0

        self._draft_lock = threading.Lock()
        self._closed_utt = 0  # utterances through this id have ended: a draft that returns later is dropped
        self._draft_counts: dict[int, int] = {}
        self._draft_threads: list[threading.Thread] = []

        self._finals: queue.Queue[_Segment | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        if not sync:
            self._worker = threading.Thread(target=self._final_loop, name=f"final-{name}", daemon=True)
            self._worker.start()

    # Time
    def wall(self, t: float) -> float:
        """Wall-clock time of a point of stream audio, from the latest buffer's arrival."""
        return self._wall_anchor - (self._pos_anchor - t)

    # Input
    def feed(self, samples: np.ndarray, wall_end: float | None = None) -> None:
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        if not len(samples):
            return
        self._fed += len(samples)
        self._pos_anchor = self._fed / RATE
        self._wall_anchor = self.clock() if wall_end is None else wall_end
        self._pending = np.concatenate([self._pending, samples])
        while len(self._pending) >= CHUNK:
            chunk = self._pending[:CHUNK]
            self._pending = self._pending[CHUNK:]
            self._chunk(chunk)

    def _chunk(self, chunk: np.ndarray) -> None:
        start = self.pos
        end = start + CHUNK / RATE
        self.pos = end
        voiced = self.vad(chunk) >= self.threshold
        if not self.in_speech:
            if voiced:
                self.in_speech = True
                self.speech_start = start
                self.last_voiced_end = end
                self.utt += 1
                self._last_draft_at = start
                keep = int(self.pre_roll_s * RATE / CHUNK)
                pre = self._ring[-keep:] if keep else []
                self._utt_samples = [*pre, chunk]
                self.h.speech(self.name, self.utt, self.wall(start))
            self._ring.append(chunk)
            del self._ring[:-self._ring_n]
            return
        self._ring.append(chunk)
        del self._ring[:-self._ring_n]
        self._utt_samples.append(chunk)
        if voiced:
            self.last_voiced_end = end
        if end - self.last_voiced_end >= self.silence_s or end - self.speech_start >= self.max_utt_s:
            self._close(end if voiced else self.last_voiced_end)
            return
        self._maybe_draft()

    def _close(self, seg_end: float) -> None:
        self.in_speech = False
        # Closed before speech_end goes out, so a draft returning now cannot follow it.
        with self._draft_lock:
            self._closed_utt = self.utt
        end_wall = self.wall(seg_end)
        self.h.speech_end(self.name, self.utt, end_wall)
        seg = _Segment(self.utt, np.concatenate(self._utt_samples), self.wall(self.speech_start), end_wall)
        self._utt_samples = []
        if self.sync:
            self._finish(seg)
        else:
            self._finals.put(seg)

    # Drafts
    def _maybe_draft(self) -> None:
        if self.draft_s <= 0 or not self.in_speech:
            return
        if self.pos - self._last_draft_at < self.draft_s or self.pos - self.last_voiced_end >= DRAFT_QUIET_S:
            return
        if not self.scheduler.claim_draft():
            return
        self._last_draft_at = self.pos
        utt, start_wall = self.utt, self.wall(self.speech_start)
        samples = np.concatenate(self._utt_samples)
        t = threading.Thread(target=self._draft, args=(utt, samples, start_wall), name=f"draft-{self.name}", daemon=True)
        self._draft_threads.append(t)
        self._draft_threads = [x for x in self._draft_threads if x.is_alive() or x is t]
        t.start()

    def _draft(self, utt: int, samples: np.ndarray, start_wall: float) -> None:
        t0 = self.clock()
        try:
            text = self.scheduler.draft(samples)
        except Exception as e:  # a failed draft costs nothing; the final still runs
            self.h.error(f"{self.name}: draft failed: {e}")
            return
        now = self.clock()
        with self._draft_lock:
            if utt <= self._closed_utt or not has_text(text):
                return
            self._draft_counts[utt] = self._draft_counts.get(utt, 0) + 1
            self.h.draft(Draft(self.name, utt, text, start_wall, now, int(len(samples) / RATE * 1000), int((now - t0) * 1000)))

    def wait_drafts(self) -> None:
        for t in list(self._draft_threads):
            t.join()

    # Finals
    def _final_loop(self) -> None:
        while (seg := self._finals.get()) is not None:
            self._finish(seg)

    def _finish(self, seg: _Segment) -> None:
        try:
            text, behind = self.scheduler.final(seg.samples)
        except Exception as e:
            self.h.error(f"{self.name}: transcription failed: {e}")
            self.h.empty(self.name, seg.utt)
            return
        with self._draft_lock:
            drafts = self._draft_counts.pop(seg.utt, 0)
        if not has_text(text):
            self.h.empty(self.name, seg.utt)
            return
        self.h.final(Final(self.name, seg.utt, text, seg.start_wall, seg.end_wall, self.clock(), drafts, behind))

    def close(self) -> None:
        """End any open utterance, transcribe it, and wait for every final."""
        if self.in_speech:
            self._close(self.last_voiced_end)
        if self._worker is not None:
            self._finals.put(None)
            self._worker.join()
            self._worker = None
