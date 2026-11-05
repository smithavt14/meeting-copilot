"""``copilot tail``: a readable live view of a run. Final lines print as ``[n] speaker: text``. On a TTY the open
drafts (utterances with no final yet) show on one line under the transcript, rewritten in place and cleared when
the final arrives, and the speakers are colour-coded (``me`` and ``call`` in two colours, drafts dimmed) unless
NO_COLOR is set. It exits on the run's ``stop`` event, or when the listener's process is gone."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, TextIO

from meeting_copilot.run import LINES, pid_alive


COLORS = {"me": "\033[36m", "call": "\033[35m"}  # cyan, magenta
DIM = "\033[2m"
RESET = "\033[0m"


def use_color(tty: bool) -> bool:
    """Colour on a TTY only, and never when NO_COLOR is set to anything (https://no-color.org)."""
    return tty and not os.environ.get("NO_COLOR")


def format_tail(line: dict, color: bool = False) -> str:
    speaker = str(line.get("speaker", "?"))
    if color:
        speaker = f"{COLORS.get(speaker, '')}{speaker}{RESET}"
    return f"[{line.get('n')}] {speaker}: {line.get('text', '')}"


class Follower:
    """Reads complete new lines from a growing file; a torn last line waits for the next read."""

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


def tail(run_dir: str | Path, since: int = 0, tty: bool = False, stream: TextIO | None = None, poll_s: float = 0.2,
         sleep: Callable[[float], None] = time.sleep, alive: Callable[[int], bool] = pid_alive,
         width: int | None = None, color: bool | None = None) -> int:
    out = stream or sys.stdout
    color = use_color(tty) if color is None else color
    follower = Follower(Path(run_dir) / LINES)
    drafts: dict[tuple, str] = {}  # (speaker, utt) -> latest draft text, for utterances with no final yet
    shown = False
    pid = None
    if width is None:
        try:
            import shutil

            width = shutil.get_terminal_size().columns
        except OSError:
            width = 100

    def clear() -> None:
        nonlocal shown
        if shown:
            out.write("\r\033[K")
            shown = False

    def show_drafts() -> None:
        nonlocal shown
        if not tty:
            return
        clear()
        if drafts:
            text = "... " + "  |  ".join(f"{k[0]}: {v}" for k, v in drafts.items())
            if len(text) > width - 1:
                text = "... " + text[len(text) - (width - 5):]  # keep the newest words in view
            out.write(f"{DIM}{text}{RESET}" if color else text)
            shown = True
        out.flush()

    while True:
        stopped = False
        for e in follower.read():
            ev = e.get("ev")
            if ev == "start" and e.get("pid"):
                pid = int(e["pid"])
            elif ev == "draft":
                drafts[(e.get("speaker"), e.get("utt"))] = str(e.get("text", ""))
                show_drafts()
            elif ev == "line":
                drafts.pop((e.get("speaker"), e.get("utt")), None)
                if int(e.get("n") or 0) > since:
                    clear()
                    out.write(format_tail(e, color) + "\n")
                    show_drafts()
                    out.flush()
            elif ev in ("echo", "empty"):
                if drafts.pop((e.get("speaker"), e.get("utt")), None) is not None:
                    show_drafts()
            elif ev == "stop":
                stopped = True
        if not stopped and pid is not None and not alive(pid):
            stopped = True
        if stopped:
            clear()
            out.write("--- end of run\n")
            out.flush()
            return 0
        sleep(poll_s)
