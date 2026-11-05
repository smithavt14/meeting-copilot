import io
import json

import numpy as np

from meeting_copilot.names import NameMatcher
from meeting_copilot.pipeline import Draft, Final, Scheduler
from meeting_copilot.run import Recorder, append_event, iso
from meeting_copilot.tail import tail
from meeting_copilot.wake import wake


def F(stream, utt, text, t):
    return Final(stream, utt, text, t, t + 1.0, t + 1.6, 0, False)


def events(path):
    return [json.loads(l) for l in path.read_text().splitlines()]


def test_nothing_is_written_while_muted_except_the_commands(tmp_path):
    states = []
    rec = Recorder(tmp_path, NameMatcher(), echo=True, show=lambda s: None)
    rec.on_mute = states.append
    rec.final(F("me", 1, "Let's look at the budget.", 100))
    rec.final(F("me", 2, "Hey Claw, mute.", 102))
    rec.draft(Draft("me", 3, "this is private", 104, 105, 1000, 80))
    rec.final(F("me", 3, "This is private, and so is the next part.", 104))
    rec.speech("call", 1, 106); rec.speech_end("call", 1, 107)
    rec.final(F("call", 1, "Sure, the numbers are confidential.", 106))
    rec.speech("call", 2, 108); rec.speech_end("call", 2, 109); rec.empty("call", 2)
    rec.final(F("me", 4, "Claude, what is the date?", 110))  # not a command: dropped while muted
    rec.final(F("me", 5, "Claude, unmute.", 112))
    rec.final(F("me", 6, "Back to the agenda.", 114))
    rec.close()
    ev = events(rec.path)
    assert [(e["ev"], e.get("text")) for e in ev] == [
        ("line", "Let's look at the budget."),
        ("line", "Hey Claw, mute."), ("mute", None),
        ("line", "Claude, unmute."), ("unmute", None),
        ("line", "Back to the agenda."),
    ]
    assert [e["n"] for e in ev if e["ev"] == "line"] == [1, 2, 3, 4]
    assert states == [True, False]


def test_copilot_mute_from_another_process_is_picked_up(tmp_path):
    rec = Recorder(tmp_path, NameMatcher(), echo=False, show=lambda s: None)
    rec.final(F("me", 1, "First.", 100))
    append_event(rec.path, "mute", by="command", t=iso(101))
    rec.final(F("me", 2, "Not written.", 102))
    append_event(rec.path, "unmute", by="command", t=iso(103))
    rec.final(F("me", 3, "Written again.", 104))
    rec.close()
    assert [(e["ev"], e.get("text")) for e in events(rec.path)] == [
        ("line", "First."), ("mute", None), ("unmute", None), ("line", "Written again.")]


def test_scheduler_while_muted():
    calls = []
    sch = Scheduler(lambda s: calls.append("cloud") or "x")
    sch.muted = True
    assert not sch.claim_draft()  # no drafts while muted
    sch.muted_max_s = 3.0
    assert sch.final(np.zeros(16000 * 5, np.float32)) == ("", False)  # long: not sent
    assert sch.final(np.zeros(16000 * 2, np.float32))[0] == "x"  # short: sent
    sch.muted_engine = lambda s: calls.append("local") or "y"
    assert sch.final(np.zeros(16000 * 5, np.float32))[0] == "y"
    assert calls == ["cloud", "local"]
    sch.muted = False
    assert sch.claim_draft()


def test_wake_returns_mute_and_unmute(tmp_path):
    p = tmp_path / "lines.jsonl"
    append_event(p, "line", n=1, speaker="me", text="Hey Claude, mute.", start=iso(100), end=iso(101), utt=1, named=True)
    append_event(p, "mute", by="voice", t=iso(101))
    out = []
    wake(tmp_path, since=0, drafts_since=0, mutes_since=0, out=out.append, sleep=lambda s: None)
    assert out[-1] == "--- seen=1 drafts=0 mutes=1 CALL MUTE"
    append_event(p, "unmute", by="command", t=iso(120))
    out = []
    wake(tmp_path, since=1, drafts_since=0, mutes_since=1, out=out.append, sleep=lambda s: None)
    assert out == ["--- seen=1 drafts=0 mutes=2 UNMUTE"]
    out, t = [], [0.0]
    wake(tmp_path, since=1, drafts_since=0, mutes_since=2, max_wait=3, out=out.append, clock=lambda: t[0],
         sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert out == ["--- seen=1 drafts=0 mutes=2 TIMEOUT"]


def test_tail_shows_the_mute_state_and_hides_drafts(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    p = tmp_path / "lines.jsonl"
    append_event(p, "draft", d=1, speaker="me", utt=1, text="so the")
    append_event(p, "mute", by="command")
    append_event(p, "draft", d=2, speaker="me", utt=2, text="hidden")
    append_event(p, "unmute", by="command")
    append_event(p, "stop")
    out = io.StringIO()
    tail(tmp_path, tty=False, stream=out, sleep=lambda s: None, alive=lambda p: True)
    assert out.getvalue() == ('--- muted: nothing is written until "Claude, unmute"\n--- listening again\n'
                              "--- end of run\n")
    out = io.StringIO()
    tail(tmp_path, tty=True, stream=out, sleep=lambda s: None, alive=lambda p: True, width=80)
    assert "hidden" not in out.getvalue()
