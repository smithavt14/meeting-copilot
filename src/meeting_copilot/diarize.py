"""Speaker labels for one audio stream: every final utterance gets a voice embedding, and voices are clustered as
they arrive, so the people in a room come out as ``s1``, ``s2``, ...

- ``Embedder``: 3D-Speaker ERes2Net-base through sherpa-onnx, 16 kHz mono float32 in, a unit vector out (about
  50 ms per utterance on a laptop CPU).
- ``Voices``: the clustering, numpy only. An utterance joins the closest voice when its cosine similarity is at
  least ``threshold``. Otherwise, if it is long enough to trust (``min_new_s``), it starts a new voice. A new voice
  is provisional until a second utterance joins it: while provisional, its utterances carry the closest confirmed
  label, so a cough or a door slam does not mint a speaker. Short utterances never start a voice and never move a
  centroid; they take the closest one.
- ``Voices.relabel``: the pass at the end of the run. It clusters every utterance again with all of them in hand
  (average linkage), folds voices with under ``min_voice_s`` of speech (or under a twentieth of the run's speech, if
  that is less, so a short run keeps its people) into the nearest bigger one, and maps the
  result back onto the live labels, so names given during the meeting still point at the same people. If that
  leaves fewer voices than the live labels that each had enough speech, the live labels stand.

An utterance is one stretch of speech between pauses, and it gets one label: when two people speak in it without
a pause, the label is whoever's voice dominates the embedding.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import numpy as np

from meeting_copilot.vad import RATE

THRESHOLD = 0.4
MIN_NEW_S = 1.5
CONFIRM = 2
MIN_VOICE_S = 20.0


def unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    return v / (float(np.linalg.norm(v)) + 1e-9)


@dataclass
class _Voice:
    total: np.ndarray  # sum of the unit embeddings of the utterances that moved it
    n: int = 1
    label: str | None = None

    def centroid(self) -> np.ndarray:
        return unit(self.total)


@dataclass
class Voices:
    threshold: float = THRESHOLD
    min_new_s: float = MIN_NEW_S
    confirm: int = CONFIRM
    voices: list[_Voice] = field(default_factory=list)
    history: list[tuple[np.ndarray, float, str]] = field(default_factory=list)  # (embedding, seconds, live label)
    _next: int = 1

    def _mint(self, v: _Voice) -> None:
        v.label = f"s{self._next}"
        self._next += 1

    def assign(self, emb: np.ndarray, seconds: float) -> tuple[str, int]:
        """The live label for one utterance, and its index in ``history`` (for ``relabel``)."""
        e = unit(emb)
        sims = [float(e @ v.centroid()) for v in self.voices]
        best = int(np.argmax(sims)) if sims else -1
        if best >= 0 and (sims[best] >= self.threshold or seconds < self.min_new_s):
            voice = self.voices[best]
            if seconds >= self.min_new_s:
                voice.total = voice.total + e
                voice.n += 1
        else:
            voice = _Voice(e.copy())
            self.voices.append(voice)
        if voice.label is None and (voice.n >= self.confirm or not any(v.label for v in self.voices)):
            self._mint(voice)
        if voice.label is None:
            confirmed = [v for v in self.voices if v.label]
            label = max(confirmed, key=lambda v: float(e @ v.centroid())).label
        else:
            label = voice.label
        self.history.append((e, float(seconds), label))
        return label, len(self.history) - 1

    def relabel(self, min_voice_s: float = MIN_VOICE_S) -> list[str]:
        """Every utterance's label after clustering them all again; see the module docstring."""
        if not self.history:
            return []
        E = np.stack([h[0] for h in self.history])
        secs = [h[1] for h in self.history]
        live = [h[2] for h in self.history]
        groups = _agglomerate(E, secs, self.threshold, self.min_new_s)
        # Fold voices with too little speech into the nearest bigger one.
        talk = {g: sum(s for s, x in zip(secs, groups) if x == g) for g in set(groups)}
        floor = min(min_voice_s, 0.05 * sum(secs))
        big = [g for g in talk if talk[g] >= floor] or [max(talk, key=talk.get)]
        cents = {g: unit(E[[i for i, x in enumerate(groups) if x == g]].mean(0)) for g in big}
        groups = [g if g in big else max(big, key=lambda b: float(E[i] @ cents[b])) for i, g in enumerate(groups)]
        # Name each group after the live label it overlaps most (by seconds), biggest groups first; a group with no
        # live label left gets a new one.
        overlap: dict[tuple[int, str], float] = {}
        for g, lab, s in zip(groups, live, secs):
            overlap[(g, lab)] = overlap.get((g, lab), 0.0) + s
        names: dict[int, str] = {}
        used: set[str] = set()
        for (g, lab), _ in sorted(overlap.items(), key=lambda kv: -kv[1]):
            if g not in names and lab not in used:
                names[g] = lab
                used.add(lab)
        for g in sorted(set(groups)):
            if g not in names:
                names[g] = f"s{self._next}"
                self._next += 1
        end = [names[g] for g in groups]
        # The pass may split a voice the live run missed, but it may not merge two voices that each talked enough:
        # with all the speech in hand, average linkage can pull two similar people together that the live
        # centroids kept apart. Then the live labels stand.
        live_talk: dict[str, float] = {}
        for lab, sec in zip(live, secs):
            live_talk[lab] = live_talk.get(lab, 0.0) + sec
        if len(set(end)) < sum(1 for t in live_talk.values() if t >= floor):
            return live
        return end


def _agglomerate(E: np.ndarray, secs: list[float], threshold: float, min_s: float) -> list[int]:
    """Average-linkage clustering of the utterances of at least ``min_s``, merging while the closest pair of groups
    is at least ``threshold`` similar; shorter utterances join the nearest group. Group similarities update in
    place (Lance-Williams), so an hour of meeting (about a thousand utterances) takes well under a second."""
    n = len(E)
    seeds = [i for i in range(n) if secs[i] >= min_s] or list(range(n))
    members = [[i] for i in seeds]
    S = (E[seeds] @ E[seeds].T).astype(np.float64)
    np.fill_diagonal(S, -np.inf)
    size = np.ones(len(seeds))
    alive = np.ones(len(seeds), dtype=bool)
    while alive.sum() > 1:
        flat = int(np.argmax(S))
        i, j = divmod(flat, S.shape[1])
        if S[i, j] < threshold:
            break
        merged = (size[i] * S[i] + size[j] * S[j]) / (size[i] + size[j])
        S[i, :] = merged
        S[:, i] = merged
        S[i, i] = -np.inf
        S[j, :] = -np.inf
        S[:, j] = -np.inf
        size[i] += size[j]
        alive[j] = False
        members[i] += members[j]
    groups = [members[k] for k in range(len(seeds)) if alive[k]]
    out = [-1] * n
    for k, g in enumerate(groups):
        for i in g:
            out[i] = k
    cents = [unit(E[g].mean(0)) for g in groups]
    for i in range(n):
        if out[i] < 0:
            out[i] = int(np.argmax([float(E[i] @ c) for c in cents]))
    return out


class Embedder:
    def __init__(self, threads: int = 1) -> None:
        import sherpa_onnx

        from meeting_copilot.models import speaker_model_path

        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(speaker_model_path()), num_threads=threads)
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self(np.zeros(RATE, dtype=np.float32))  # warm up

    def __call__(self, samples: np.ndarray) -> np.ndarray:
        s = self.extractor.create_stream()
        s.accept_waveform(RATE, np.asarray(samples, dtype=np.float32))
        s.input_finished()
        return unit(np.array(self.extractor.compute(s)))


class Diarizer:
    """``embed`` plus ``Voices``, safe to call from a stream's final thread: ``label(samples)`` returns
    ``(label, index)``."""

    def __init__(self, embed=None, voices: Voices | None = None) -> None:
        self.embed = embed if embed is not None else Embedder()
        self.voices = voices or Voices()
        self._lock = threading.Lock()

    def label(self, samples: np.ndarray) -> tuple[str, int]:
        emb = self.embed(samples)
        with self._lock:
            return self.voices.assign(emb, len(samples) / RATE)

    def relabel(self) -> list[str]:
        with self._lock:
            return self.voices.relabel()
