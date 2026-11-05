import threading
import time

import numpy as np

from helpers import RATE, energy_vad, silence, tone
from meeting_copilot.pipeline import Handlers, Scheduler, Stream


def feed(st, audio, step=512):
    for i in range(0, len(audio), step):
        st.feed(audio[i:i + step])
        st.wait_drafts()


def collect():
    log = []
    h = Handlers(speech=lambda s, u, t: log.append(("speech", s, u)), speech_end=lambda s, u, t: log.append(("end", s, u)),
                 draft=lambda d: log.append(("draft", d.stream, d.utt, d.text)), final=lambda f: log.append(("final", f.stream, f.utt, f.drafts)))
    return log, h


def test_drafts_about_once_a_second_while_speaking():
    log, h = collect()
    st = Stream("me", energy_vad, Scheduler(lambda s: f"{len(s)}"), h, draft_s=1.0, sync=True)
    feed(st, np.concatenate([silence(0.5), tone(3.6), silence(1.0)]))
    st.close()
    drafts = [e for e in log if e[0] == "draft"]
    assert len(drafts) == 3
    final = [e for e in log if e[0] == "final"][0]
    assert final[3] == 3  # the final carries how many drafts its utterance had
    # Every draft comes before its utterance's speech_end, and the final after it.
    assert log.index(("end", "me", 1)) > log.index(drafts[-1])


def test_draft_ms_zero_turns_drafts_off():
    log, h = collect()
    st = Stream("me", energy_vad, Scheduler(lambda s: "x"), h, draft_s=0, sync=True)
    feed(st, np.concatenate([tone(3.0), silence(1.0)]))
    st.close()
    assert not [e for e in log if e[0] == "draft"]


def test_no_draft_in_trailing_silence():
    log, h = collect()
    st = Stream("me", energy_vad, Scheduler(lambda s: "x"), h, draft_s=0.2, sync=True)
    feed(st, np.concatenate([tone(0.5), silence(0.26)]))  # a draft may start in the first 0.25 s of quiet
    n = len([e for e in log if e[0] == "draft"])
    feed(st, silence(0.2))  # still open (under 0.5 s), but quiet for more than 0.25 s: no new draft
    assert st.in_speech
    assert len([e for e in log if e[0] == "draft"]) == n
    st.close()


def test_scheduler_refuses_a_draft_while_a_final_waits_or_a_draft_runs():
    sch = Scheduler(lambda s: "x")
    assert sch.claim_draft()
    assert not sch.claim_draft()  # one draft at a time
    sch.draft(np.zeros(10, np.float32))
    assert sch.claim_draft()
    sch.draft(np.zeros(10, np.float32))
    sch.finals_waiting = 1
    assert not sch.claim_draft()  # a waiting final blocks new drafts


def test_one_call_at_a_time_and_a_final_waits_behind_one_draft_at_most():
    release = threading.Event()
    calls = []

    def engine(samples):
        calls.append(threading.current_thread().name)
        if threading.current_thread().name.startswith("draft"):
            release.wait(2)
        else:
            time.sleep(0.05)
        return "words"

    sch = Scheduler(engine)
    assert sch.claim_draft()
    d = threading.Thread(target=sch.draft, args=(np.zeros(16000, np.float32),), name="draft-x")
    d.start()
    time.sleep(0.05)
    results = []
    finals = [threading.Thread(target=lambda: results.append(sch.final(np.zeros(16000, np.float32))), name=f"final-{i}")
              for i in range(2)]
    for t in finals:
        t.start()
    time.sleep(0.05)
    assert not sch.claim_draft()  # finals are waiting
    release.set()
    d.join()
    for t in finals:
        t.join()
    assert sch.max_concurrent == 1
    assert all(behind for _, behind in results)  # both came in while the one draft ran
    assert calls[0] == "draft-x" and len(calls) == 3  # no second draft got in ahead of them


def test_a_draft_that_returns_after_its_utterance_ended_is_dropped():
    gate = threading.Event()
    started = threading.Event()

    def engine(samples):
        if threading.current_thread().name.startswith("draft"):
            started.set()
            gate.wait(2)
            return "late draft"
        return "final text"

    log, h = collect()
    st = Stream("me", energy_vad, Scheduler(engine), h, draft_s=1.0, sync=False)
    audio = np.concatenate([tone(1.2)])
    for i in range(0, len(audio), 512):
        st.feed(audio[i:i + 512])
    assert started.wait(2)  # the draft call is running
    st.feed(silence(0.7))  # the utterance ends while it runs
    assert ("end", "me", 1) in log
    gate.set()
    st.wait_drafts()
    st.close()
    assert not [e for e in log if e[0] == "draft"]
    assert [e for e in log if e[0] == "final"][0][3] == 0


def test_two_streams_share_one_slot():
    calls = {"now": 0, "max": 0}
    lock = threading.Lock()

    def engine(samples):
        with lock:
            calls["now"] += 1
            calls["max"] = max(calls["max"], calls["now"])
        time.sleep(0.01)
        with lock:
            calls["now"] -= 1
        return "w"

    sch = Scheduler(engine)
    _, h = collect()
    a = Stream("me", energy_vad, sch, h, draft_s=0.3)
    b = Stream("call", energy_vad, sch, h, draft_s=0.3)
    audio = np.concatenate([tone(2.0), silence(0.8)] * 2)
    for i in range(0, len(audio), 512):
        a.feed(audio[i:i + 512])
        b.feed(audio[i:i + 512])
    a.wait_drafts(); b.wait_drafts()
    a.close(); b.close()
    assert calls["max"] == 1
