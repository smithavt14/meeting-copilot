"""A listening run: its directory, its ``lines.jsonl``, the echo backstop between the streams, and stopping.

Each run writes ``<out>/<YYYY-MM-DD_HHMMSS>/lines.jsonl`` (``<out>`` is ``--out``, ``COPILOT_OUT`` or a per-user
data directory), one JSON object per line, appended as it happens:

- ``{"ev": "start", ...}`` once.
- ``{"ev": "line", "n", "speaker", "stream": "me"|"call", "text", "start", "end", "utt", "named", "latency_ms",
  "drafts"}``: a final. ``n`` counts from 1. This is the record. ``stream`` is the audio source. ``speaker`` is the
  stream too, except on a diarized stream (``--diarize``), where it is the voice: ``s1``, ``s2``, ...
- ``{"ev": "draft", "d", "speaker", "stream", "utt", "text", "start", "named"}``: the open utterance so far
  (``speaker`` is always the stream). The final with the same ``stream`` and ``utt`` replaces it. Files written
  before ``stream`` existed have only ``speaker``, which was the stream.
- ``{"ev": "echo", ...}``: a "me" line dropped as speaker echo of a call line.
- ``{"ev": "said", "id", "phase": "start"|"end", "text", "start", "end"}``: written by ``copilot say`` around
  playback. A "me" line or draft inside that window that repeats the answer is dropped as echo.
- ``{"ev": "mute"|"unmute", "by": "voice"|"command"}``: while muted nothing else is written; the spoken
  "Claude, mute" and "Claude, unmute" lines themselves are.
- ``{"ev": "empty", "speaker", "utt"}``: an utterance that transcribed to nothing.
- ``{"ev": "name", "speaker", "name"}``: written by ``copilot name``: show that voice label as that name from now on,
  in ``tail``, ``wake`` and ``transcript`` (the lines keep the label).
- ``{"ev": "relabel", "lines": {"<n>": "<label>"}}``: on a diarized run, once just before ``stop``: the lines whose
  speaker changed when every utterance was clustered again with the whole meeting in hand. It wins over the line's
  own ``speaker``.
- ``{"ev": "error", "msg"}``, and ``{"ev": "stop"}`` last.

``<out>/current.json`` names the running run, and a ``stop`` file in the run directory ends it.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from meeting_copilot.echo import EchoGate, SaidWindows
from meeting_copilot.names import NameMatcher
from meeting_copilot.pipeline import Draft, Final

LINES = "lines.jsonl"
CURRENT = "current.json"
STOP = "stop"


def iso(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="milliseconds")


def parse_iso(s) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except ValueError:
        return None


class Follower:
    """Reads complete new JSON lines from a growing file; a torn last line waits for the next read."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.pos = 0
        self._rest = b""

    def read(self) -> list[dict]:
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
        except FileNotFoundError:
            return []
        self.pos += len(data)
        data = self._rest + data
        parts = data.split(b"\n")
        self._rest = parts.pop()
        out = []
        for raw in parts:
            try:
                out.append(json.loads(raw.decode("utf-8", errors="replace")))
            except json.JSONDecodeError:
                continue
        return out


def append_event(path: Path, ev: str, **fields) -> None:
    """One event appended by another process (``copilot say``): a single write of one line."""
    line = json.dumps({"ev": ev, **fields}, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def default_out() -> Path:
    """COPILOT_OUT, else a per-user data directory, so listen and wake agree from any working directory."""
    env = os.environ.get("COPILOT_OUT")
    if env:
        return Path(env)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "meeting-copilot" / "out"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "meeting-copilot" / "out"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "meeting-copilot" / "out"


def new_run_dir(out: Path) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    d = out / stamp
    i = 1
    while d.exists():
        i += 1
        d = out / f"{stamp}-{i}"
    d.mkdir(parents=True)
    return d


def read_current(out: Path) -> dict | None:
    """``current.json``: ``{"pid", "dir"}``; ``dir`` is null while the run is still starting."""
    try:
        cur = json.loads((out / CURRENT).read_text())
        return cur if isinstance(cur, dict) and "pid" in cur else None
    except (OSError, ValueError):
        return None


def newest_run(out: Path) -> Path | None:
    runs = sorted((p.parent for p in out.glob(f"*/{LINES}")), key=lambda p: p.name)
    return runs[-1] if runs else None


def current_run(out: Path) -> Path | None:
    """The run named in ``current.json``, else the newest run directory with a lines file."""
    cur = read_current(out)
    if cur and cur.get("dir") and Path(cur["dir"]).exists():
        return Path(cur["dir"])
    return newest_run(out)


def find_run(out: Path, wait_s: float = 15.0, poll_s: float = 0.2, clock=time.time, sleep=time.sleep) -> Path | None:
    """The live run, waiting up to ``wait_s`` for one that is starting or about to start; after that, the newest
    finished run (or None). A command started a moment before ``listen`` thus attaches to the new run."""
    deadline = clock() + wait_s
    while True:
        cur = read_current(out)
        if cur and pid_alive(int(cur.get("pid") or 0)) and cur.get("dir") and (Path(cur["dir"]) / LINES).exists():
            return Path(cur["dir"])
        if clock() >= deadline:
            return newest_run(out)
        sleep(poll_s)


class RunBusy(Exception):
    def __init__(self, pid: int) -> None:
        super().__init__(f"a run is already going (pid {pid})")
        self.pid = pid


def reserve(out: Path) -> None:
    """Claims ``current.json`` for this process with an exclusive create, so two listeners cannot both start.
    A file left by a process that is gone is taken over. Raises RunBusy when a live process holds it."""
    path = out / CURRENT
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            cur = read_current(out)
            if cur is None:
                # Being written right now, or unreadable: look again, then treat it as stale.
                time.sleep(0.1)
                cur = read_current(out)
            pid = int(cur.get("pid") or 0) if cur else 0
            if pid and pid_alive(pid):
                raise RunBusy(pid)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"pid": os.getpid(), "dir": None}))
        return
    raise RuntimeError(f"cannot claim {path}")


def publish(out: Path, run_dir: Path) -> None:
    """Names the run directory in this process's ``current.json`` (atomic replace)."""
    tmp = out / f"{CURRENT}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps({"pid": os.getpid(), "dir": str(run_dir.resolve())}))
    os.replace(tmp, out / CURRENT)


def release(out: Path) -> None:
    """Removes ``current.json`` if this process holds it."""
    cur = read_current(out)
    if cur and int(cur.get("pid") or 0) == os.getpid():
        try:
            (out / CURRENT).unlink()
        except FileNotFoundError:
            pass


def run_pid(events: list[dict]) -> int | None:
    """The listener's pid, from the run's start event."""
    for e in events:
        if e.get("ev") == "start" and e.get("pid"):
            return int(e["pid"])
    return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_pid(out: Path) -> int | None:
    try:
        pid = int(json.loads((out / CURRENT).read_text())["pid"])
    except (OSError, ValueError, KeyError):
        return None
    return pid if pid_alive(pid) else None


class Recorder:
    """Takes finals and drafts from both streams and writes the run's events file."""

    def __init__(self, run_dir: Path, names: NameMatcher, echo: bool, show: Callable[[str], None] | None = None,
                 status: Callable[[str], None] = lambda s: print(s, file=sys.stderr), tty: bool = False) -> None:
        self.dir = run_dir
        self.path = run_dir / LINES
        self.names = names
        self.gate = EchoGate() if echo else None
        self.show = show or (lambda line: print(line, flush=True))
        self.status = status
        self.tty = tty
        self._lock = threading.RLock()
        self._f = open(self.path, "a", encoding="utf-8")
        self.n = 0
        self.d = 0
        self.lines: list[dict] = []
        self.drafts: list[dict] = []
        self.echoes: list[dict] = []
        self._draft_shown = False
        self._drafted: set[tuple[str, int]] = set()  # utterances with a draft on disk
        self.said = SaidWindows()
        self._follow = Follower(self.path)
        self.muted = False
        self.on_mute: Callable[[bool], None] = lambda muted: None
        self._voices: dict[int, int] = {}  # line n -> its utterance's index in the diarizer's history

    def relabel(self, labels: list[str]) -> dict[int, str]:
        """Writes one ``relabel`` event for the lines whose speaker changed in the diarizer's end-of-run pass."""
        with self._lock:
            changed = {}
            for rec in self.lines:
                i = self._voices.get(rec["n"])
                if i is not None and i < len(labels) and labels[i] != rec["speaker"]:
                    changed[rec["n"]] = labels[i]
            if changed:
                self.emit("relabel", lines={str(n): lab for n, lab in changed.items()}, t=iso(time.time()))
            return changed

    def poll(self) -> None:
        """Picks up events other processes appended to this run's file: ``said`` from ``copilot say``, ``mute`` and
        ``unmute`` from ``copilot mute``/``unmute`` (and this process's own, which change nothing)."""
        with self._lock:
            for e in self._follow.read():
                if e.get("ev") == "said":
                    self.said.event(e, parse_iso)
                elif e.get("ev") in ("mute", "unmute"):
                    self._set_muted(e["ev"] == "mute")

    def _set_muted(self, muted: bool) -> None:
        if muted == self.muted:
            return
        self.muted = muted
        if self.tty and self._draft_shown:
            sys.stdout.write("\r\033[K")
            self._draft_shown = False
        self.status('muted: nothing is written until "Claude, unmute" or copilot unmute' if muted else "listening again")
        self.on_mute(muted)

    def mute(self, muted: bool, by: str) -> None:
        with self._lock:
            self.emit("mute" if muted else "unmute", by=by, t=iso(time.time()))
            self._set_muted(muted)

    def _said_echo(self, stream: str, text: str, t0: float, t1: float) -> float | None:
        if stream != "me":
            return None
        self.poll()
        echo, ratio = self.said.is_echo(text, t0, t1)
        return ratio if echo else None

    def emit(self, ev: str, **fields) -> dict:
        rec = {"ev": ev, **fields}
        with self._lock:
            self._f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._f.flush()
        return rec

    # Stream handlers
    def speech(self, stream: str, utt: int, t: float) -> None:
        if stream == "call" and self.gate:
            with self._lock:
                self.gate.call_speech()

    def speech_end(self, stream: str, utt: int, t: float) -> None:
        if stream == "call" and self.gate:
            with self._lock:
                self.gate.call_speech_end()

    def draft(self, d: Draft) -> None:
        with self._lock:
            self.poll()
            if self.muted:
                return
            if self._said_echo(d.stream, d.text, d.start, d.emit) is not None:
                return  # the mic hearing the spoken answer
            self.d += 1
            rec = self.emit("draft", d=self.d, speaker=d.stream, stream=d.stream, utt=d.utt, text=d.text, start=iso(d.start),
                            t=iso(d.emit), named=self.names(d.text), audio_ms=d.audio_ms, ms=d.ms)
            self.drafts.append(rec)
            self._drafted.add((d.stream, d.utt))
            if self.tty:
                sys.stdout.write(f"\r\033[K... {d.stream}: {d.text}")
                sys.stdout.flush()
                self._draft_shown = True

    def final(self, f: Final) -> None:
        with self._lock:
            self.poll()
            cmd = self.names.command(f.text)
            if self.muted:
                self._settle_call(f, "")  # keep the stream bookkeeping, remember no words
                self._void_drafts(f.stream, f.utt)
                if cmd == "unmute":  # the command line is the one line written
                    self._write_line(f, force=True)
                    self.mute(False, by="voice")
                return
            if f.muted:  # overlapped a mute that has since ended: dropped whole
                self._settle_call(f, "")
                self._void_drafts(f.stream, f.utt)
                return
            if cmd == "mute":
                self._settle_call(f, f.text)  # releases mic lines held from before the mute
                self._write_line(f)
                self.mute(True, by="voice")
                return
            ratio = self._said_echo(f.stream, f.text, f.start, f.end)
            if ratio is not None:
                self.echoes.append(self.emit("echo", speaker=f.stream, stream=f.stream, utt=f.utt, text=f.text, ratio=round(ratio, 2),
                                             start=iso(f.start), end=iso(f.end), of="said"))
                if self.gate is not None:
                    self._release(self.gate.tick(time.time()))
                return
            if self.gate is None:
                self._write_line(f)
                return
            if f.stream == "call":
                self._write_line(f)
                released = self.gate.call_final(f.start, f.end, f.text)
            else:
                released = self.gate.me_final({"final": f}, time.time())
            self._release(released)

    def _settle_call(self, f: Final, text: str) -> None:
        """Every call final has to reach the echo gate, or mic lines wait on it until the hold timeout."""
        if f.stream == "call" and self.gate:
            self._release(self.gate.call_final(f.start, f.end, text))

    def _void_drafts(self, stream: str, utt: int) -> None:
        """A dropped utterance whose drafts went out before a mute gets an ``empty`` (no words), so they stop waking."""
        if (stream, utt) in self._drafted:
            self._drafted.discard((stream, utt))
            self.emit("empty", speaker=stream, stream=stream, utt=utt)

    def empty(self, stream: str, utt: int) -> None:
        with self._lock:
            if not self.muted:
                self._drafted.discard((stream, utt))
                self.emit("empty", speaker=stream, stream=stream, utt=utt)  # the utterance had no words: its drafts are void
            else:
                self._void_drafts(stream, utt)
        if stream == "call" and self.gate:
            with self._lock:
                self._release(self.gate.call_final(0.0, 0.0, ""))

    def error(self, msg: str) -> None:
        self.status(msg)
        self.emit("error", msg=msg, t=iso(time.time()))

    def tick(self) -> None:
        self.poll()
        if self.gate:
            with self._lock:
                self._release(self.gate.tick(time.time()))

    def flush(self) -> None:
        if self.gate:
            with self._lock:
                self._release(self.gate.flush())

    def _release(self, held: list[dict]) -> None:
        if self.muted:
            return  # held lines heard before the mute are dropped with the rest
        for item in held:
            f: Final = item["final"]
            echo, ratio = self.gate.echo.is_echo(f.text, f.start, f.end)
            if echo:
                rec = self.emit("echo", speaker=f.stream, stream=f.stream, utt=f.utt, text=f.text, ratio=round(ratio, 2),
                                start=iso(f.start), end=iso(f.end))
                self.echoes.append(rec)
                continue
            self._write_line(f)

    def _write_line(self, f: Final, force: bool = False) -> None:
        if self.muted and not force:
            return
        self._drafted.discard((f.stream, f.utt))
        self.n += 1
        rec = self.emit("line", n=self.n, speaker=f.speaker or f.stream, stream=f.stream, text=f.text, start=iso(f.start), end=iso(f.end),
                        utt=f.utt, named=self.names(f.text), latency_ms=int((f.emit - f.end) * 1000),
                        drafts=f.drafts, **({"behind_draft": True} if f.behind_draft else {}))
        self.lines.append(rec)
        if f.voice is not None:
            self._voices[self.n] = f.voice
        if self.tty and self._draft_shown:
            sys.stdout.write("\r\033[K")
            self._draft_shown = False
        self.show(format_line(rec))

    def close(self) -> None:
        with self._lock:
            self._f.close()


def clock(iso_t: str | None) -> str:
    return iso_t[11:19] if iso_t and len(iso_t) >= 19 else "--:--:--"


def stream_of(e: dict):
    """The audio source of a line, draft, echo or empty event (older files carry it as ``speaker``)."""
    return e.get("stream") or e.get("speaker")


class Speakers:
    """Who each line is shown as: the ``relabel`` event's label over the line's own, then a ``copilot name``."""

    def __init__(self, events: list[dict] = ()) -> None:
        self.names: dict[str, str] = {}
        self.relabels: dict[str, str] = {}
        for e in events:
            self.event(e)

    def event(self, e: dict) -> None:
        if e.get("ev") == "name" and e.get("speaker"):
            name = str(e.get("name") or "").strip()
            if name:
                self.names[str(e["speaker"])] = name
            else:
                self.names.pop(str(e["speaker"]), None)
        elif e.get("ev") == "relabel" and isinstance(e.get("lines"), dict):
            self.relabels.update({str(k): str(v) for k, v in e["lines"].items()})

    def label(self, line: dict) -> str:
        return self.relabels.get(str(line.get("n")), str(line.get("speaker", "?")))

    def show(self, line: dict) -> str:
        label = self.label(line)
        return self.names.get(label, label)


def format_line(line: dict, call: bool = False, speakers: Speakers | None = None) -> str:
    who = speakers.show(line) if speakers else line.get("speaker", "?")
    return f"[{line.get('n')} {clock(line.get('start'))}] {who}: {line.get('text', '')}" + (" CALL" if call else "")


def format_draft(draft: dict) -> str:
    return f"[d{draft.get('d')} {clock(draft.get('start'))}] {stream_of(draft) or '?'}: {draft.get('text', '')} DRAFT"
