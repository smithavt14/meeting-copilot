import numpy as np

from helpers import RATE, energy_vad, silence, tone
from meeting_copilot.pipeline import Handlers, Scheduler, Stream


def run(audio, **kw):
    finals, events = [], []
    seen = []

    def engine(samples):
        seen.append(len(samples))
        return f"utterance {len(seen)}"

    h = Handlers(speech=lambda s, u, t: events.append(("speech", u)), speech_end=lambda s, u, t: events.append(("end", u)),
                 final=finals.append)
    st = Stream("me", energy_vad, Scheduler(engine), h, draft_s=0, sync=True, **kw)
    for i in range(0, len(audio), 1600):
        st.feed(audio[i:i + 1600], wall_end=1000.0 + (i + 1600) / RATE)
    st.close()
    return finals, events, st


def test_short_pause_does_not_cut():
    finals, events, _ = run(np.concatenate([silence(0.5), tone(1.0), silence(0.3), tone(1.0), silence(1.0)]))
    assert len(finals) == 1
    assert events == [("speech", 1), ("end", 1)]


def test_half_second_of_silence_cuts():
    finals, events, _ = run(np.concatenate([silence(0.5), tone(1.0), silence(0.7), tone(1.0), silence(1.0)]))
    assert len(finals) == 2
    assert [f.utt for f in finals] == [1, 2]


def test_utterance_times_and_pre_roll():
    finals, _, st = run(np.concatenate([silence(1.0), tone(2.0), silence(1.0)]))
    f = finals[0]
    # Speech starts at 1.0 s of stream audio and the last voiced chunk ends near 3.0 s; walls are 1000 + stream time.
    assert abs((f.start - 1000.0) - 1.0) < 0.05
    assert abs((f.end - 1000.0) - 3.0) < 0.05


def test_pre_roll_is_included_in_the_samples():
    lens = []
    h = Handlers(final=lambda f: None)
    st = Stream("me", energy_vad, Scheduler(lambda s: lens.append(len(s)) or "x"), h, draft_s=0, sync=True)
    st.feed(np.concatenate([silence(1.0), tone(2.0), silence(1.0)]))
    st.close()
    # 2 s of speech plus about 0.3 s of pre-roll plus the 0.5 s of silence it took to cut.
    assert abs(lens[0] / RATE - 2.8) < 0.08


def test_max_length_cuts_a_long_utterance():
    finals, _, _ = run(np.concatenate([tone(5.0), silence(1.0)]), max_utt_s=2.0)
    assert len(finals) >= 2


def test_close_flushes_an_open_utterance():
    finals, _, _ = run(np.concatenate([silence(0.2), tone(1.0)]))
    assert len(finals) == 1


def test_empty_text_is_not_a_final():
    empties = []
    h = Handlers(final=lambda f: (_ for _ in ()).throw(AssertionError("no final expected")),
                 empty=lambda s, u: empties.append(u))
    st = Stream("me", energy_vad, Scheduler(lambda s: " "), h, draft_s=0, sync=True)
    st.feed(np.concatenate([tone(1.0), silence(1.0)]))
    st.close()
    assert empties == [1]
