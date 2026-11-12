"""Speaker labels: the clustering on synthetic embeddings, the stream hook, the record, and the readers."""

import io
import json

import numpy as np

from helpers import RATE, energy_vad, silence, tone
from meeting_copilot import cli
from meeting_copilot.diarize import Diarizer, Voices
from meeting_copilot.names import NameMatcher
from meeting_copilot.pipeline import Draft, Final, Handlers, Scheduler, Stream
from meeting_copilot.run import Recorder, Speakers, append_event, format_line
from meeting_copilot.tail import tail
from meeting_copilot.wake import wake

DIM = 64
rng = np.random.default_rng(7)
PEOPLE = [rng.normal(size=DIM) for _ in range(3)]


def voice(p, noise=0.3):
    return PEOPLE[p] / np.linalg.norm(PEOPLE[p]) + rng.normal(size=DIM) * noise / np.sqrt(DIM)


def events(path):
    return [json.loads(l) for l in path.read_text().splitlines()]


# Voices

def test_three_people_get_three_stable_labels():
    v = Voices()
    order = [0, 1, 0, 2, 1, 2, 0, 0, 1, 2, 2, 1]
    labels = [v.assign(voice(p), 3.0)[0] for p in order]
    by_person = {p: {lab for q, lab in zip(order, labels) if q == p} for p in range(3)}
    # A new voice is provisional for its first utterance, which carries the closest confirmed label.
    assert labels[0] == "s1"
    assert labels[1] == "s1" and labels[4] == "s2"
    after_confirm = {p: {lab for i, (q, lab) in enumerate(zip(order, labels)) if q == p and i >= 6} for p in range(3)}
    assert [len(after_confirm[p]) for p in range(3)] == [1, 1, 1]
    assert len({next(iter(after_confirm[p])) for p in range(3)}) == 3
    assert by_person[0] == {"s1"}


def test_a_one_off_noise_mints_no_label():
    v = Voices()
    for _ in range(4):
        v.assign(voice(0), 3.0)
    odd = rng.normal(size=DIM)  # a door slam: far from everyone, heard once
    assert v.assign(odd, 2.0)[0] == "s1"
    assert v.assign(voice(0), 3.0)[0] == "s1"
    assert {h[2] for h in v.history} == {"s1"}


def test_short_utterances_never_start_a_voice():
    v = Voices()
    v.assign(voice(0), 3.0)
    for _ in range(3):
        assert v.assign(voice(1), 0.8)[0] == "s1"  # "yeah", "mm-hm": too short to trust
    assert len(v.voices) == 1


def test_relabel_keeps_the_live_labels_and_fixes_provisional_lines():
    v = Voices()
    order = [0, 1, 1, 0, 1, 0, 1, 0] * 4
    live = [v.assign(voice(p), 3.0)[0] for p in order]
    assert live[1] == "s1"  # person 1's first line, before the voice was confirmed
    end = v.relabel(min_voice_s=5)
    assert end[1] == "s2"
    assert {lab for p, lab in zip(order, end) if p == 0} == {"s1"}
    assert {lab for p, lab in zip(order, end) if p == 1} == {"s2"}


def test_relabel_folds_a_voice_with_little_speech():
    v = Voices()
    for p in [0, 1] * 10:
        v.assign(voice(p), 3.0)
    v.assign(voice(2), 1.5)
    v.assign(voice(2), 1.5)  # 3 s from a third voice: under a twentieth of the run's 63 s
    end = v.relabel(min_voice_s=20)
    assert set(end) <= {"s1", "s2"}


def test_relabel_keeps_a_quiet_person_in_a_short_run():
    v = Voices()
    for p in [0] * 10 + [1, 1]:
        v.assign(voice(p), 3.0)  # 6 s of 36: under 20 s, but more than a twentieth
    assert v.relabel()[-2:] == ["s2", "s2"]


def test_relabel_never_hands_a_label_to_the_provisional_newcomer():
    v = Voices()
    alice, bob = np.array([1.0, 0.0]), np.array([0.0, 1.0])
    live = [v.assign(alice, 3.0)[0], v.assign(bob, 15.0)[0], v.assign(bob, 3.0)[0], v.assign(alice, 3.0)[0]]
    assert live == ["s1", "s1", "s2", "s1"]  # Bob's first, long line borrowed s1 while his voice was provisional
    assert v.relabel(min_voice_s=1) == ["s1", "s2", "s2", "s1"]


def test_borrowed_speech_does_not_protect_the_lender_from_a_split():
    v = Voices()
    alice, bob = np.array([1.0, 0.0]), np.array([0.0, 1.0])
    live = [v.assign(alice, 2.0)[0], v.assign(bob, 30.0)[0]] + [v.assign(bob, 3.0)[0] for _ in range(10)]
    assert live[:3] == ["s1", "s1", "s2"]  # Bob's first 30 s borrowed s1
    assert v.relabel(min_voice_s=1) == ["s1"] + ["s2"] * 11


def test_short_replies_by_voiced_seconds_start_no_voice():
    d = Diarizer(embed=lambda samples: voice(0) if len(samples) > 2 * RATE else voice(1))
    d.label(np.zeros(3 * RATE, dtype=np.float32), 2.5)
    for _ in range(2):  # 0.8 s replies in a 1.6 s buffer (pre-roll and trailing silence)
        assert d.label(np.zeros(int(1.6 * RATE), dtype=np.float32), 0.8)[0] == "s1"
    assert len(d.voices.voices) == 1


def test_relabel_of_nothing():
    assert Voices().relabel() == []


# The stream hook

def run_stream(labeler, muted=False):
    finals, errors = [], []
    sched = Scheduler(lambda a: "words")
    sched.muted = muted
    st = Stream("me", energy_vad, sched, Handlers(final=finals.append, error=errors.append), draft_s=0, sync=True,
                labeler=labeler)
    st.feed(np.concatenate([tone(1.0), silence(1.0), tone(1.0), silence(1.0)]))
    st.close()
    return finals, errors


def test_each_final_carries_its_label():
    seen = []

    def labeler(samples, seconds):
        seen.append((len(samples) / RATE, seconds))
        return f"s{len(seen)}", len(seen) - 1

    finals, _ = run_stream(labeler)
    assert [(f.speaker, f.voice) for f in finals] == [("s1", 0), ("s2", 1)]
    # The labeler gets the voiced seconds, not the buffer with its pre-roll and trailing silence.
    assert all(abs(sec - 1.0) < 0.05 and n > sec for n, sec in seen)


def test_muted_speech_is_not_labelled():
    calls = []
    finals, _ = run_stream(lambda s, sec: calls.append(1) or ("s1", 0), muted=True)
    assert calls == [] and all(f.speaker is None for f in finals)


def test_a_failed_label_keeps_the_line():
    def labeler(samples, seconds):
        raise RuntimeError("model gone")

    finals, errors = run_stream(labeler)
    assert len(finals) == 2 and all(f.speaker is None for f in finals)
    assert errors and "speaker label failed" in errors[0]


def test_diarizer_with_a_stand_in_embedder():
    d = Diarizer(embed=lambda samples: voice(0))
    assert d.label(np.zeros(3 * RATE, dtype=np.float32)) == ("s1", 0)
    assert d.relabel() == ["s1"]


# The record

def F(utt, text, t, speaker=None, voice_i=None, stream="me"):
    return Final(stream, utt, text, t, t + 1.0, t + 1.6, 0, False, False, speaker, voice_i)


def test_lines_carry_voice_and_stream_and_the_relabel_event(tmp_path):
    rec = Recorder(tmp_path, NameMatcher(), echo=False, show=lambda s: None)
    rec.draft(Draft("me", 1, "so the", 100, 100.5, 500, 40))
    rec.final(F(1, "So the plan is set.", 100, "s1", 0))
    rec.final(F(2, "I disagree.", 102, "s1", 1))
    rec.final(F(3, "Fine.", 104, None, None))  # no label: the stream
    assert rec.relabel(["s1", "s2"]) == {2: "s2"}
    rec.close()
    ev = events(rec.path)
    lines = [e for e in ev if e["ev"] == "line"]
    assert [(l["speaker"], l["stream"]) for l in lines] == [("s1", "me"), ("s1", "me"), ("me", "me")]
    assert ev[0]["ev"] == "draft" and ev[0]["stream"] == "me"
    assert ev[-1]["ev"] == "relabel" and ev[-1]["lines"] == {"2": "s2"}


def test_no_relabel_event_when_nothing_changed(tmp_path):
    rec = Recorder(tmp_path, NameMatcher(), echo=False, show=lambda s: None)
    rec.final(F(1, "Hello.", 100, "s1", 0))
    assert rec.relabel(["s1"]) == {}
    rec.close()
    assert [e["ev"] for e in events(rec.path)] == ["line"]


# Readers

RUN = [
    {"ev": "start", "pid": 1},
    {"ev": "draft", "d": 1, "speaker": "me", "stream": "me", "utt": 1, "text": "so the"},
    {"ev": "line", "n": 1, "speaker": "s1", "stream": "me", "text": "So the plan is set.", "utt": 1,
     "start": "2026-11-12T10:00:01.000-05:00"},
    {"ev": "draft", "d": 2, "speaker": "me", "stream": "me", "utt": 2, "text": "I"},
    {"ev": "line", "n": 2, "speaker": "s2", "stream": "me", "text": "I disagree.", "utt": 2,
     "start": "2026-11-12T10:00:03.000-05:00"},
]


def write(path, evs):
    with open(path / "lines.jsonl", "a") as f:
        for e in evs:
            f.write(json.dumps(e) + "\n")


def test_tail_clears_a_draft_whose_final_has_a_voice_label(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    write(tmp_path, RUN + [{"ev": "stop"}])
    out = io.StringIO()
    tail(tmp_path, tty=True, stream=out, sleep=lambda s: None, alive=lambda p: True, width=80)
    v = out.getvalue()
    assert "[1] s1: So the plan is set.\n" in v and "[2] s2: I disagree.\n" in v
    assert v.endswith("\r\033[K[2] s2: I disagree.\n--- end of run\n")  # no draft redrawn after its final


def test_tail_shows_a_name_from_then_on(tmp_path):
    write(tmp_path, RUN[:3] + [{"ev": "name", "speaker": "s2", "name": "Joe"}] + RUN[3:] + [{"ev": "stop"}])
    out = io.StringIO()
    tail(tmp_path, stream=out, sleep=lambda s: None, alive=lambda p: True)
    assert out.getvalue() == "[1] s1: So the plan is set.\n--- s2 is Joe\n[2] Joe: I disagree.\n--- end of run\n"


def test_tail_colours_each_voice(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    write(tmp_path, RUN + [{"ev": "stop"}])
    out = io.StringIO()
    tail(tmp_path, tty=True, stream=out, sleep=lambda s: None, alive=lambda p: True, width=80)
    assert "\033[36ms1\033[0m" in out.getvalue() and "\033[32ms2\033[0m" in out.getvalue()


def test_wake_names_the_speakers_and_still_sees_drafts_end(tmp_path):
    write(tmp_path, RUN + [{"ev": "name", "speaker": "s1", "name": "Alex"}])
    got = []
    wake(tmp_path, since=0, drafts_since=0, lines=2, out=got.append, alive=lambda p: True)
    assert got[0] == "[1 10:00:01] Alex: So the plan is set."
    assert got[1] == "[2 10:00:03] s2: I disagree."
    assert got[-1].startswith("--- seen=2 drafts=2")


def test_speakers_apply_relabel_then_names():
    sp = Speakers([{"ev": "relabel", "lines": {"2": "s1"}}, {"ev": "name", "speaker": "s1", "name": "Alex"}])
    assert sp.show({"n": 2, "speaker": "s2"}) == "Alex"
    assert sp.show({"n": 3, "speaker": "s2"}) == "s2"
    sp.event({"ev": "name", "speaker": "s1", "name": ""})
    assert format_line({"n": 2, "speaker": "s2", "text": "Hi."}, speakers=sp) == "[2 --:--:--] s1: Hi."


def test_name_and_transcript_commands(tmp_path, capsys):
    run = tmp_path / "2026-11-12_100000"
    run.mkdir()
    write(run, RUN + [{"ev": "relabel", "lines": {"1": "s2"}}, {"ev": "stop"}])
    assert cli.main(["--out", str(tmp_path), "name", "s3", "Nobody"]) == 1
    assert "its speakers are: s2" in capsys.readouterr().err
    assert cli.main(["--out", str(tmp_path), "name", "s2", "Joe", "Comly"]) == 0
    assert capsys.readouterr().out == "s2 is Joe Comly\n"
    assert cli.main(["--out", str(tmp_path), "transcript", "--run", str(run)]) == 0
    assert capsys.readouterr().out == ("[1 10:00:01] Joe Comly: So the plan is set.\n"
                                       "[2 10:00:03] Joe Comly: I disagree.\n")


def test_old_files_without_stream_still_pair_drafts(tmp_path):
    old = [{"ev": "start", "pid": 1}, {"ev": "draft", "d": 1, "speaker": "me", "utt": 1, "text": "so"},
           {"ev": "line", "n": 1, "speaker": "me", "text": "So.", "utt": 1}, {"ev": "stop"}]
    write(tmp_path, old)
    out = io.StringIO()
    tail(tmp_path, tty=True, stream=out, sleep=lambda s: None, alive=lambda p: True, width=80, color=False)
    assert out.getvalue().endswith("\r\033[K[1] me: So.\n--- end of run\n")


def test_diarize_setting_and_flag():
    a = cli.parser().parse_args(["listen", "--diarize"])
    assert a.diarize is True
    assert cli.parser().parse_args(["listen"]).diarize is None
    assert cli.DEFAULTS["diarize"] is False


def test_relabel_never_merges_two_voices_that_talked_enough():
    v = Voices(threshold=0.4)
    for p in [0, 1] * 8:
        v.assign(voice(p), 3.0)
    live = [h[2] for h in v.history]
    v.threshold = -1.0  # the end pass would now merge everything into one group
    assert v.relabel(min_voice_s=5) == live
