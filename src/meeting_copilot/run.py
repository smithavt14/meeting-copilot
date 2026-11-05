"""A listening run: its directory, its ``lines.jsonl``, the echo backstop between the streams, and stopping.

Each run writes ``<out>/<YYYY-MM-DD_HHMMSS>/lines.jsonl`` (``<out>`` is ``--out``, ``COPILOT_OUT`` or a per-user
data directory), one JSON object per line, appended as it happens:

- ``{"ev": "start", ...}`` once.
- ``{"ev": "line", "n", "speaker": "me"|"call", "text", "start", "end", "utt", "named", "latency_ms", "drafts"}``:
  a final. ``n`` counts from 1. This is the record.
- ``{"ev": "draft", "d", "speaker", "utt", "text", "start", "named"}``: the open utterance so far. The final with the
  same speaker and ``utt`` replaces it.
- ``{"ev": "echo", ...}``: a "me" line dropped as speaker echo of a call line.
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

from meeting_copilot.echo import EchoGate
from meeting_copilot.names import NameMatcher
from meeting_copilot.pipeline import Draft, Final

LINES = "lines.jsonl"
CURRENT = "current.json"
STOP = "stop"


def iso(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="milliseconds")


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


def current_run(out: Path) -> Path | None:
    """The run named in ``current.json``, else the newest run directory with a lines file."""
    try:
        d = Path(json.loads((out / CURRENT).read_text())["dir"])
        if d.exists():
            return d
    except (OSError, ValueError, KeyError):
        pass
    runs = sorted((p.parent for p in out.glob(f"*/{LINES}")), key=lambda p: p.name)
    return runs[-1] if runs else None


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
            self.d += 1
            rec = self.emit("draft", d=self.d, speaker=d.stream, utt=d.utt, text=d.text, start=iso(d.start),
                            t=iso(d.emit), named=self.names(d.text), audio_ms=d.audio_ms, ms=d.ms)
            self.drafts.append(rec)
            if self.tty:
                sys.stdout.write(f"\r\033[K... {d.stream}: {d.text}")
                sys.stdout.flush()
                self._draft_shown = True

    def final(self, f: Final) -> None:
        with self._lock:
            if self.gate is None:
                self._write_line(f)
                return
            if f.stream == "call":
                self._write_line(f)
                released = self.gate.call_final(f.start, f.end, f.text)
            else:
                released = self.gate.me_final({"final": f}, time.time())
            self._release(released)

    def empty(self, stream: str, utt: int) -> None:
        if stream == "call" and self.gate:
            with self._lock:
                self._release(self.gate.call_final(0.0, 0.0, ""))

    def error(self, msg: str) -> None:
        self.status(msg)
        self.emit("error", msg=msg, t=iso(time.time()))

    def tick(self) -> None:
        if self.gate:
            with self._lock:
                self._release(self.gate.tick(time.time()))

    def flush(self) -> None:
        if self.gate:
            with self._lock:
                self._release(self.gate.flush())

    def _release(self, held: list[dict]) -> None:
        for item in held:
            f: Final = item["final"]
            echo, ratio = self.gate.echo.is_echo(f.text, f.start, f.end)
            if echo:
                rec = self.emit("echo", speaker=f.stream, utt=f.utt, text=f.text, ratio=round(ratio, 2),
                                start=iso(f.start), end=iso(f.end))
                self.echoes.append(rec)
                continue
            self._write_line(f)

    def _write_line(self, f: Final) -> None:
        self.n += 1
        rec = self.emit("line", n=self.n, speaker=f.stream, text=f.text, start=iso(f.start), end=iso(f.end),
                        utt=f.utt, named=self.names(f.text), latency_ms=int((f.emit - f.end) * 1000),
                        drafts=f.drafts, **({"behind_draft": True} if f.behind_draft else {}))
        self.lines.append(rec)
        if self.tty and self._draft_shown:
            sys.stdout.write("\r\033[K")
            self._draft_shown = False
        self.show(format_line(rec))

    def close(self) -> None:
        with self._lock:
            self._f.close()


def clock(iso_t: str | None) -> str:
    return iso_t[11:19] if iso_t and len(iso_t) >= 19 else "--:--:--"


def format_line(line: dict, call: bool = False) -> str:
    return f"[{line.get('n')} {clock(line.get('start'))}] {line.get('speaker', '?')}: {line.get('text', '')}" + (" CALL" if call else "")


def format_draft(draft: dict) -> str:
    return f"[d{draft.get('d')} {clock(draft.get('start'))}] {draft.get('speaker', '?')}: {draft.get('text', '')} DRAFT"
