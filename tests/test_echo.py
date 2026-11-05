from meeting_copilot.echo import EchoFilter, EchoGate


def test_me_line_repeating_a_recent_call_line_is_echo():
    f = EchoFilter()
    f.played(10.0, 13.0, "We should ship the new pricing page on Monday morning")
    echo, ratio = f.is_echo("we should ship the new pricing page on monday", 10.5, 13.5)
    assert echo and ratio >= 0.7


def test_a_different_line_is_kept():
    f = EchoFilter()
    f.played(10.0, 13.0, "We should ship the new pricing page on Monday morning")
    assert not f.is_echo("I think Tuesday works better for the team", 14.0, 16.0)[0]


def test_old_call_lines_age_out():
    f = EchoFilter(window_s=8.0)
    f.played(10.0, 13.0, "We should ship the new pricing page on Monday morning")
    assert not f.is_echo("we should ship the new pricing page on monday", 30.0, 33.0)[0]


def test_short_lines_need_an_exact_run_heard_just_before():
    f = EchoFilter()
    f.played(10.0, 12.0, "can we get to ninety by June")
    assert f.is_echo("to ninety", 11.5, 12.3)[0]
    assert not f.is_echo("yes", 11.5, 12.0)[0]  # not in the call line
    assert not f.is_echo("to ninety", 20.0, 20.5)[0]  # too long after


def test_me_lines_wait_while_the_call_is_talking():
    g = EchoGate()
    g.call_speech()
    me = {"text": "pricing page on monday"}
    assert g.me_final(me, now=0.0) == []  # held
    g.call_speech_end()
    released = g.call_final(10.0, 13.0, "We should ship the new pricing page on Monday morning")
    assert released == [me]
    assert g.echo.is_echo("we should ship the new pricing page on monday", 10.5, 13.5)[0]


def test_held_lines_go_after_hold_s():
    g = EchoGate(hold_s=8.0)
    g.call_speech()
    me = {"text": "x"}
    g.me_final(me, now=0.0)
    assert g.tick(5.0) == []
    assert g.tick(9.0) == [me]


def test_an_empty_call_final_releases_too():
    g = EchoGate()
    g.call_speech(); g.call_speech_end()
    me = {"text": "x"}
    assert g.me_final(me, now=0.0) == []
    assert g.call_final(0, 0, "") == [me]


def test_recorder_drops_a_me_line_that_echoes_the_call(tmp_path):
    import json

    from meeting_copilot.names import NameMatcher
    from meeting_copilot.pipeline import Final
    from meeting_copilot.run import Recorder

    rec = Recorder(tmp_path, NameMatcher(), echo=True, show=lambda s: None)
    rec.speech("call", 1, 100.0)
    # The mic's copy of the call sentence finishes first and is held while the call utterance is open.
    rec.final(Final("me", 1, "ship the new pricing page on Monday", 100.5, 103.0, 103.6, 0, False))
    assert rec.n == 0
    rec.speech_end("call", 1, 103.2)
    rec.final(Final("call", 1, "We should ship the new pricing page on Monday morning", 100.0, 103.2, 103.8, 0, False))
    rec.final(Final("me", 2, "I think Tuesday works better", 104.0, 106.0, 106.6, 0, False))
    rec.close()
    events = [json.loads(l) for l in (tmp_path / "lines.jsonl").read_text().splitlines()]
    assert [(e["ev"], e.get("speaker")) for e in events] == [("line", "call"), ("echo", "me"), ("line", "me")]
    assert [e["n"] for e in events if e["ev"] == "line"] == [1, 2]
