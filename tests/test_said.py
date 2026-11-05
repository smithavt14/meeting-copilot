import io
import json

from meeting_copilot.names import NameMatcher
from meeting_copilot.pipeline import Draft, Final
from meeting_copilot.run import Recorder, append_event, iso
from meeting_copilot.tail import tail
from meeting_copilot.wake import wake

ANSWER = "The boiling point of water at sea level is two hundred and twelve degrees Fahrenheit."


def said(path, start, end=None):
    append_event(path, "said", id="a1", phase="start", text=ANSWER, start=iso(start))
    if end is not None:
        append_event(path, "said", id="a1", phase="end", text=ANSWER, start=iso(start), end=iso(end))


def events(path):
    return [json.loads(l) for l in path.read_text().splitlines()]


def test_the_mic_hearing_the_spoken_answer_is_dropped(tmp_path):
    rec = Recorder(tmp_path, NameMatcher(), echo=False, show=lambda s: None)
    said(rec.path, 100.0, 105.0)
    rec.draft(Draft("me", 1, "the boiling point of water at sea", 100.2, 102.0, 1000, 80))
    rec.final(Final("me", 1, "the boiling point of water at sea level is two hundred and twelve", 100.2, 104.8, 105.4, 1, False))
    rec.final(Final("me", 2, "point of water at sea level", 106.0, 108.0, 108.6, 0, False))  # within 4 s after
    rec.final(Final("me", 3, "thanks, can you also check the freezing point", 106.0, 108.5, 109.0, 0, False))
    rec.final(Final("me", 4, "the boiling point of water at sea level", 120.0, 122.0, 122.6, 0, False))  # long after
    rec.final(Final("call", 1, "two hundred and twelve degrees Fahrenheit", 101.0, 104.0, 104.5, 0, False))
    rec.close()
    ev = events(rec.path)
    lines = [(e["speaker"], e["text"]) for e in ev if e["ev"] == "line"]
    assert lines == [("me", "thanks, can you also check the freezing point"),
                     ("me", "the boiling point of water at sea level"),
                     ("call", "two hundred and twelve degrees Fahrenheit")]
    assert [e["of"] for e in ev if e["ev"] == "echo"] == ["said", "said"]
    assert not [e for e in ev if e["ev"] == "draft"]


def line(n, text, start, end, named=False, utt=None):
    return {"ev": "line", "n": n, "speaker": "me", "text": text, "start": iso(start), "end": iso(end),
            "utt": utt or n, "named": named}


def test_no_wake_inside_a_said_window(tmp_path):
    p = tmp_path / "lines.jsonl"
    said(p, 100.0, 105.0)
    with open(p, "a") as f:
        f.write(json.dumps(line(1, "Claude said the boiling point", 103.0, 104.0, named=True)) + "\n")
        f.write(json.dumps({"ev": "draft", "d": 1, "speaker": "me", "utt": 2, "text": "Claude is", "start": iso(106.0),
                            "t": iso(107.0), "named": True}) + "\n")
    out = []
    t = [0.0]
    wake(tmp_path, since=0, drafts_since=0, lines=8, max_wait=2, out=out.append, clock=lambda: t[0],
         sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert out[0] == "[1 " + iso(103.0)[11:19] + "] me: Claude said the boiling point"  # printed, no CALL
    assert out[-1] == "--- seen=1 drafts=1 TIMEOUT"


def test_a_call_after_the_window_still_wakes(tmp_path):
    p = tmp_path / "lines.jsonl"
    said(p, 100.0, 105.0)
    with open(p, "a") as f:
        f.write(json.dumps(line(1, "Claude, one more question", 115.0, 117.0, named=True)) + "\n")
    out = []
    wake(tmp_path, since=0, drafts_since=0, out=out.append, sleep=lambda s: None)
    assert out[-1].endswith("CALL")


def test_tail_shows_what_was_said(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    p = tmp_path / "lines.jsonl"
    with open(p, "w") as f:
        f.write(json.dumps(line(1, "Claude, how hot is boiling", 90.0, 92.0, named=True)) + "\n")
    said(p, 100.0, 105.0)
    append_event(p, "stop")
    out = io.StringIO()
    tail(tmp_path, stream=out, sleep=lambda s: None, alive=lambda p: True)
    assert out.getvalue() == f"[1] me: Claude, how hot is boiling\nclaude (said): {ANSWER}\n--- end of run\n"
    out = io.StringIO()
    tail(tmp_path, tty=True, stream=out, sleep=lambda s: None, alive=lambda p: True, width=80)
    assert f"\033[33mclaude (said)\033[0m: {ANSWER}" in out.getvalue()
