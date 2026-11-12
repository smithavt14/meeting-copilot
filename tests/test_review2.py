"""Codex round-2 findings: an utterance that overlapped a mute is dropped whole, every call final settles the echo
gate, and a draft whose utterance ended empty or as echo does not wake."""

import io
import json

import numpy as np

from helpers import energy_vad, silence, tone
from meeting_copilot.names import NameMatcher
from meeting_copilot.pipeline import Draft, Final, Handlers, Scheduler, Stream
from meeting_copilot.run import Recorder, append_event, iso
from meeting_copilot.wake import draft_hits, wake


def feed(st, audio, step=512):
    for i in range(0, len(audio), step):
        st.feed(audio[i:i + step])
        st.wait_drafts()


def events(path):
    return [json.loads(l) for l in path.read_text().splitlines()]


def F(stream, utt, text, t, muted=False):
    return Final(stream, utt, text, t, t + 1.0, t + 1.6, 0, False, muted)


# 1. Muted speech written after an unmute that lands before the cut

def wired(engine_calls, cloud_limit=None, muted_engine=None):
    finals, drafts = [], []
    sched = Scheduler(lambda s: engine_calls.append("cloud") or "private words")
    sched.muted_engine = muted_engine
    sched.muted_max_s = cloud_limit
    st = Stream("me", energy_vad, sched, Handlers(final=finals.append, draft=drafts.append), draft_s=0.3, sync=True)

    def set_muted(m):
        sched.muted = m
        st.mark_muted()
    return st, sched, finals, drafts, set_muted


def test_utterance_open_across_an_unmute_is_muted_whole():
    calls = []
    st, sched, finals, drafts, set_muted = wired(calls, muted_engine=lambda s: calls.append("local") or "private words")
    set_muted(True)
    feed(st, np.concatenate([silence(0.3), tone(1.5)]))
    set_muted(False)  # copilot unmute before the 500 ms cut
    feed(st, np.concatenate([tone(1.0), silence(1.0)]))
    st.close()
    assert drafts == []  # no drafts, before or after the unmute
    assert [f.muted for f in finals] == [True]
    assert calls == ["local"]  # transcribed on the muted path, never the cloud engine


def test_utterance_open_when_a_mute_lands_is_muted_whole():
    calls = []
    st, sched, finals, drafts, set_muted = wired(calls, cloud_limit=3.0)
    feed(st, np.concatenate([silence(0.3), tone(2.0)]))
    n_before = len(drafts)
    set_muted(True)  # typed mute mid-sentence
    feed(st, np.concatenate([tone(1.5), silence(1.0)]))  # 3.5 s in all: over the cloud limit for muted audio
    set_muted(False)
    st.close()
    assert len(drafts) == n_before  # drafts stop at the mute
    assert finals == [] and calls[n_before:] == []  # the muted 3.5 s utterance never went to the cloud


def test_utterance_after_the_unmute_is_normal():
    calls = []
    st, sched, finals, drafts, set_muted = wired(calls)
    set_muted(True)
    set_muted(False)
    feed(st, np.concatenate([silence(0.3), tone(1.0), silence(1.0)]))
    st.close()
    assert [f.muted for f in finals] == [False]


def test_recorder_drops_a_muted_final_after_unmute(tmp_path):
    rec = Recorder(tmp_path, NameMatcher(), echo=True, show=lambda s: None)
    rec.draft(Draft("me", 1, "Claude, the private", 100, 100.5, 500, 50))  # drafted before the mute
    rec.mute(True, by="command")
    rec.mute(False, by="command")
    rec.final(F("me", 1, "Claude, the private plan is this.", 100, muted=True))
    rec.speech("call", 1, 103); rec.speech_end("call", 1, 104)
    rec.final(F("call", 1, "Something said on the call while muted.", 103, muted=True))
    rec.final(F("me", 2, "Back to it.", 106))
    rec.close()
    ev = events(rec.path)
    assert [e for e in ev if e["ev"] == "line"][0]["text"] == "Back to it."
    assert len([e for e in ev if e["ev"] == "line"]) == 1
    assert {"ev": "empty", "speaker": "me", "stream": "me", "utt": 1} in ev  # closes the pre-mute draft
    assert rec.gate.call_pending == 0
    assert draft_hits(ev, 0) == []


# 3. Every call final settles the echo gate

def test_call_side_mute_command_settles_the_gate(tmp_path):
    rec = Recorder(tmp_path, NameMatcher(), echo=True, show=lambda s: None)
    rec.speech("call", 1, 100); rec.speech_end("call", 1, 101)
    rec.final(F("call", 1, "Claude, mute.", 100))
    assert rec.muted and rec.gate.call_pending == 0
    rec.final(F("me", 1, "Claude, unmute.", 103))
    rec.final(F("me", 2, "A mic line after the unmute.", 105))
    rec.close()
    texts = [e["text"] for e in events(rec.path) if e["ev"] == "line"]
    assert texts == ["Claude, mute.", "Claude, unmute.", "A mic line after the unmute."]


def test_mic_line_held_before_a_call_mute_is_released(tmp_path):
    rec = Recorder(tmp_path, NameMatcher(), echo=True, show=lambda s: None)
    rec.speech("call", 1, 100)
    rec.final(F("me", 1, "I said this before the mute.", 99))  # held while the call talks
    rec.speech_end("call", 1, 102)
    rec.final(F("call", 1, "Claude, mute.", 100))
    rec.close()
    texts = [e["text"] for e in events(rec.path) if e["ev"] == "line"]
    assert "I said this before the mute." in texts


# 4. A draft whose utterance ended empty or as echo does not wake

def named_draft(utt):
    return {"ev": "draft", "d": utt, "speaker": "me", "utt": utt, "text": "Claude, what", "start": iso(100),
            "t": iso(100.5), "named": True}


def test_draft_with_empty_or_echo_final_does_not_wake():
    ev = [named_draft(1), {"ev": "empty", "speaker": "me", "utt": 1},
          named_draft(2), {"ev": "echo", "speaker": "me", "utt": 2, "text": "Claude, what"},
          named_draft(3)]
    assert [e["utt"] for e in draft_hits(ev, 0)] == [3]


def test_wake_cli_path_ignores_voided_drafts(tmp_path):
    p = tmp_path / "lines.jsonl"
    ev = [{"ev": "start", "t": iso(99)}, named_draft(1), {"ev": "empty", "speaker": "me", "utt": 1},
          {"ev": "stop", "t": iso(101)}]
    p.write_text("".join(json.dumps(e) + "\n" for e in ev))
    out = []
    wake(tmp_path, since=0, drafts_since=0, out=out.append, max_wait=1)
    assert "DRAFT" not in "\n".join(out) and out[-1].endswith("END")
