import json
import threading

from meeting_copilot.wake import wake


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def write(path, events, torn=None):
    with open(path / "lines.jsonl", "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
        if torn:
            f.write(torn)


def line(n, text, utt=None, speaker="call", named=False):
    return {"ev": "line", "n": n, "speaker": speaker, "text": text, "start": f"2026-01-01T10:00:{n:02d}.000-05:00",
            "end": "", "utt": utt or n, "named": named}


def draft(d, text, utt, speaker="call", named=False):
    return {"ev": "draft", "d": d, "speaker": speaker, "utt": utt, "text": text,
            "start": "2026-01-01T10:00:00.000-05:00", "named": named}


def run(tmp_path, **kw):
    out = []
    clock = FakeClock()
    rc = wake(tmp_path, out=out.append, clock=clock, sleep=clock.sleep, **kw)
    return rc, out, clock


def test_n_new_lines_wake(tmp_path):
    write(tmp_path, [{"ev": "start"}] + [line(i, f"line {i}") for i in range(1, 4)])
    rc, out, _ = run(tmp_path, since=0, lines=3)
    assert rc == 0
    assert out[-1] == "--- seen=3 drafts=0"
    assert len(out) == 4


def test_since_cursor_skips_read_lines(tmp_path):
    write(tmp_path, [line(i, f"line {i}") for i in range(1, 6)])
    rc, out, _ = run(tmp_path, since=3, lines=2)
    assert [o.split("]")[0] for o in out[:-1]] == ["[4 10:00:04", "[5 10:00:05"]
    assert out[-1] == "--- seen=5 drafts=0"


def test_named_line_wakes_at_once(tmp_path):
    write(tmp_path, [line(1, "Claude, what is the date", named=True)])
    _, out, clock = run(tmp_path, since=0, lines=8)
    assert out[0].endswith("CALL")
    assert out[-1] == "--- seen=1 drafts=0 CALL"
    assert clock.t == 0.0


def test_named_draft_wakes_before_the_final(tmp_path):
    write(tmp_path, [line(1, "hello"), draft(1, "so the plan", 2), draft(2, "Claude, what is", 3, named=True),
                     draft(3, "Claude, what is the", 3, named=True)])
    _, out, _ = run(tmp_path, since=1, drafts_since=0, lines=8)
    assert out == ["[d2 10:00:00] call: Claude, what is DRAFT", "--- seen=1 drafts=3 DRAFT"]


def test_a_named_draft_wakes_once(tmp_path):
    write(tmp_path, [draft(1, "Claude, what is", 1, named=True), draft(2, "Claude, what is the", 1, named=True)])
    rc, out, clock = run(tmp_path, since=0, drafts_since=2, lines=8, max_wait=5)
    assert out[-1] == "--- seen=0 drafts=2 TIMEOUT"


def test_a_named_draft_whose_final_is_in_does_not_wake_as_a_draft(tmp_path):
    write(tmp_path, [draft(1, "Claude, what is", 1, named=True), line(1, "Claude, what is the date", utt=1, named=True)])
    _, out, _ = run(tmp_path, since=0, drafts_since=0)
    assert "DRAFT" not in out[-1] and out[-1].endswith("CALL")


def test_no_cursor_means_old_drafts_are_old(tmp_path):
    write(tmp_path, [draft(1, "Claude, what is", 1, named=True)])
    _, out, _ = run(tmp_path, since=0, lines=8, max_wait=3)
    assert out[-1] == "--- seen=0 drafts=1 TIMEOUT"


def test_stop_ends_with_every_line(tmp_path):
    write(tmp_path, [line(1, "a"), line(2, "b"), {"ev": "stop"}])
    _, out, _ = run(tmp_path, since=0, lines=8)
    assert out[-1] == "--- seen=2 drafts=0 END"
    assert len(out) == 3


def test_timeout_returns_what_there_is(tmp_path):
    write(tmp_path, [line(1, "a")])
    _, out, clock = run(tmp_path, since=0, lines=8, max_wait=30)
    assert out[-1] == "--- seen=1 drafts=0 TIMEOUT"
    assert 30 <= clock.t < 31


def test_a_torn_last_line_waits(tmp_path):
    write(tmp_path, [line(1, "a")], torn='{"ev": "line", "n": 2, "named": tr')
    _, out, _ = run(tmp_path, since=0, lines=2, max_wait=2)
    assert out[-1] == "--- seen=1 drafts=0 TIMEOUT"


def test_missing_run(tmp_path):
    rc, out, _ = run(tmp_path / "nope", since=0, appear_s=1)
    assert rc == 2 and out == []


def test_wakes_when_a_line_lands_later(tmp_path):
    import time

    write(tmp_path, [line(1, "a")])

    def later():
        time.sleep(0.3)
        write(tmp_path, [line(1, "a"), line(2, "Claude, help", named=True)])

    threading.Thread(target=later).start()
    out = []
    wake(tmp_path, since=1, drafts_since=0, out=out.append, poll_s=0.05, max_wait=5)
    assert out[-1] == "--- seen=2 drafts=0 CALL"
