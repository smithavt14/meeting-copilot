"""``copilot tail``: a readable live view of a run. Final lines print as ``[n] speaker: text``. On a TTY the open
drafts (utterances with no final yet) show on one line under the transcript, rewritten in place and cleared when
the final arrives, and the speakers are colour-coded (``me`` and ``call`` in two colours, drafts dimmed) unless
NO_COLOR is set. It exits on the run's ``stop`` event, or when the listener's process is gone."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Callable, TextIO

from meeting_copilot.run import LINES, Follower, pid_alive


COLORS = {"me": "\033[36m", "call": "\033[35m", "said": "\033[33m"}  # cyan, magenta, yellow
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def format_said(e: dict, color: bool = False) -> str:
    label = "claude (said)"
    if color:
        label = f"{COLORS['said']}{label}{RESET}"
    return f"{label}: {e.get('text', '')}"


def use_color(tty: bool) -> bool:
    """Colour on a TTY only, and never when NO_COLOR is set to anything (https://no-color.org)."""
    return tty and not os.environ.get("NO_COLOR")


def format_tail(line: dict, color: bool = False) -> str:
    speaker = str(line.get("speaker", "?"))
    if color:
        speaker = f"{COLORS.get(speaker, '')}{speaker}{RESET}"
    return f"[{line.get('n')}] {speaker}: {line.get('text', '')}"


def tail(run_dir: str | Path, since: int = 0, tty: bool = False, stream: TextIO | None = None, poll_s: float = 0.2,
         sleep: Callable[[float], None] = time.sleep, alive: Callable[[int], bool] = pid_alive,
         width: int | None = None, color: bool | None = None) -> int:
    out = stream or sys.stdout
    color = use_color(tty) if color is None else color
    follower = Follower(Path(run_dir) / LINES)
    drafts: dict[tuple, str] = {}  # (speaker, utt) -> latest draft text, for utterances with no final yet
    shown = False
    muted = False
    pid = None

    def cols() -> int:
        """The terminal width now, read on every redraw: a pane resized after start must not wrap a draft."""
        if width is not None:
            return width
        import shutil

        return shutil.get_terminal_size((100, 24)).columns

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
        if drafts and not muted:
            # One row only: "\r\033[K" clears just the cursor's row, so a wrapped draft would leave its top row behind.
            room = max(cols() - 2, 10)
            text = "... " + "  |  ".join(f"{k[0]}: {v}" for k, v in drafts.items())
            if len(text) > room:
                text = "... " + text[len(text) - (room - 4):]  # keep the newest words in view
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
            elif ev == "said" and e.get("phase") == "start":
                clear()
                out.write(format_said(e, color) + "\n")
                show_drafts()
                out.flush()
            elif ev in ("mute", "unmute"):
                muted = ev == "mute"
                if muted:
                    drafts.clear()
                clear()
                msg = '--- muted: nothing is written until "Claude, unmute"' if muted else "--- listening again"
                out.write((f"{BOLD}{msg}{RESET}" if color else msg) + "\n")
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
