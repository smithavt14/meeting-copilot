"""``copilot wake``: block until a run has something for the copilot session, then print the new lines.

It returns on the first of:

- a final line after ``since`` that says the name (marked ``CALL``);
- a named draft: the first draft of an utterance that says the name, newer than ``drafts_since``, whose utterance
  has no final line yet (printed marked ``DRAFT``);
- ``lines`` new final lines after ``since``;
- the run's ``stop`` event, or the listener's process gone without one (``END``);
- ``max_wait`` seconds (``TIMEOUT``).

- a ``mute`` or ``unmute`` event after ``mutes_since`` (``MUTE`` or ``UNMUTE``; the closing line then carries
  ``mutes=<m>``, the cursor for ``--mutes-since``).

A line or draft heard while ``copilot say`` was playing, or within a few seconds after, never wakes it.

The output is the new final lines, then any waking drafts, then ``--- seen=<n> drafts=<d>`` and the markers. Pass
``seen`` and ``drafts`` back as ``--since`` and ``--drafts-since`` on the next call. The file can still be growing:
only lines that end in a newline are read, so a torn last line waits for the next read.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from meeting_copilot.echo import SaidWindows
from meeting_copilot.run import LINES, format_draft, format_line, parse_iso, pid_alive, run_pid


def read_events(path: str | Path) -> list[dict[str, Any]]:
    data = Path(path).read_bytes().decode("utf-8", errors="replace")
    out: list[dict[str, Any]] = []
    for raw in data.split("\n")[:-1]:
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def last_draft(events: list[dict[str, Any]]) -> int:
    return max([0, *(int(e.get("d") or 0) for e in events if e.get("ev") == "draft")])


def said_windows(events: list[dict[str, Any]]) -> SaidWindows:
    w = SaidWindows()
    for e in events:
        if e.get("ev") == "said":
            w.event(e, parse_iso)
    return w


def in_said(e: dict[str, Any], said: SaidWindows) -> bool:
    """- a ``mute`` or ``unmute`` event after ``mutes_since`` (``MUTE`` or ``UNMUTE``; the closing line then carries
  ``mutes=<m>``, the cursor for ``--mutes-since``).

A line or draft heard while ``copilot say`` played, or just after: it never wakes."""
    t0 = parse_iso(e.get("start"))
    t1 = parse_iso(e.get("end")) or parse_iso(e.get("t")) or t0
    return t0 is not None and said.covers(t0, t1 or t0)


def draft_hits(events: list[dict[str, Any]], drafts_since: int) -> list[dict[str, Any]]:
    """The first named draft of each utterance, newer than ``drafts_since``, whose utterance has no final yet, and
    that was not heard during a spoken answer."""
    said = said_windows(events)
    done = {(e.get("speaker"), e.get("utt")) for e in events if e.get("ev") == "line"}
    first: dict[tuple[Any, Any], dict[str, Any]] = {}
    for e in events:
        if e.get("ev") == "draft" and e.get("named"):
            first.setdefault((e.get("speaker"), e.get("utt")), e)
    return [e for key, e in first.items()
            if int(e.get("d") or 0) > drafts_since and key not in done and not in_said(e, said)]


def new_lines(events: list[dict[str, Any]], since: int) -> list[dict[str, Any]]:
    return [e for e in events if e.get("ev") == "line" and int(e.get("n") or 0) > since]


def wake(run_dir: str | Path | None, since: int, drafts_since: int | None = None, lines: int = 8,
         max_wait: float = 600.0, poll_s: float = 0.2, out: Callable[[str], None] = print,
         clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
         appear_s: float = 15.0, alive: Callable[[int], bool] = pid_alive, mutes_since: int | None = None) -> int:
    path = Path(run_dir) / LINES if run_dir is not None else None
    started = clock()
    while path is not None and not path.exists() and clock() - started < appear_s:
        sleep(poll_s)  # a run that just started may not have written its file yet
    if path is None or not path.exists():
        print(f"no run found{f' at {run_dir}' if run_dir else ''}", file=sys.stderr)
        return 2
    started = clock()
    while True:
        events = read_events(path)
        if drafts_since is None:
            drafts_since = last_draft(events)  # no cursor given: drafts already written are old
        mutes = [e for e in events if e.get("ev") in ("mute", "unmute")]
        if mutes_since is None:
            mutes_since = len(mutes)
        toggled = mutes[mutes_since:]
        fresh = new_lines(events, since)
        stopped = any(e.get("ev") == "stop" for e in events)
        if not stopped:
            pid = run_pid(events)
            stopped = pid is not None and not alive(pid)  # the listener is gone without a stop event
        early = draft_hits(events, drafts_since)
        said = said_windows(events)
        calls = {int(e["n"]) for e in fresh if e.get("named") and not in_said(e, said)}
        called = bool(calls)
        timed_out = clock() - started >= max_wait
        if called or early or stopped or toggled or len(fresh) >= max(1, lines) or timed_out:
            break
        sleep(poll_s)
    for line in fresh:
        out(format_line(line, call=int(line["n"]) in calls))
    for draft in early:
        out(format_draft(draft))
    seen = max([since, *(int(e["n"]) for e in fresh)])
    cursor = max(drafts_since, last_draft(events))
    marks = (" CALL" if called else "") + (" DRAFT" if early else "") + (" END" if stopped else "") + \
            (f" {toggled[-1]['ev'].upper()}" if toggled else "") + \
            (" TIMEOUT" if timed_out and not (called or early or stopped or toggled or len(fresh) >= max(1, lines)) else "")
    out(f"--- seen={seen} drafts={cursor}{f' mutes={len(mutes)}' if mutes else ''}{marks}")
    return 0
