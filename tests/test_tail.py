import io
import json

from meeting_copilot.tail import tail


def write(path, events):
    with open(path / "lines.jsonl", "a") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


EVENTS = [
    {"ev": "start", "pid": 1},
    {"ev": "draft", "d": 1, "speaker": "me", "utt": 1, "text": "so the"},
    {"ev": "draft", "d": 2, "speaker": "me", "utt": 1, "text": "so the plan is"},
    {"ev": "line", "n": 1, "speaker": "me", "text": "So the plan is set.", "utt": 1},
    {"ev": "draft", "d": 3, "speaker": "call", "utt": 1, "text": "sounds"},
    {"ev": "line", "n": 2, "speaker": "call", "text": "Sounds good.", "utt": 1},
    {"ev": "stop"},
]


def test_non_tty_prints_finals_only_and_exits_on_stop(tmp_path):
    write(tmp_path, EVENTS)
    out = io.StringIO()
    assert tail(tmp_path, stream=out, sleep=lambda s: None, alive=lambda p: True) == 0
    assert out.getvalue() == "[1] me: So the plan is set.\n[2] call: Sounds good.\n--- end of run\n"


def test_since_skips_earlier_lines(tmp_path):
    write(tmp_path, EVENTS)
    out = io.StringIO()
    tail(tmp_path, since=1, stream=out, sleep=lambda s: None, alive=lambda p: True)
    assert out.getvalue() == "[2] call: Sounds good.\n--- end of run\n"


def test_exits_when_the_listener_is_gone(tmp_path):
    write(tmp_path, EVENTS[:4])
    out = io.StringIO()
    assert tail(tmp_path, stream=out, sleep=lambda s: None, alive=lambda p: False) == 0
    assert out.getvalue().endswith("--- end of run\n")


def test_follows_lines_written_later(tmp_path):
    write(tmp_path, EVENTS[:2])
    out = io.StringIO()
    calls = {"n": 0}

    def sleep(s):
        calls["n"] += 1
        if calls["n"] == 1:
            write(tmp_path, EVENTS[2:4])
        elif calls["n"] == 2:
            with open(tmp_path / "lines.jsonl", "a") as f:
                f.write('{"ev": "line", "n": 2, "speaker": "call", "te')  # torn: waits
        elif calls["n"] == 3:
            with open(tmp_path / "lines.jsonl", "a") as f:
                f.write('xt": "Later.", "utt": 2}\n{"ev": "stop"}\n')

    tail(tmp_path, stream=out, sleep=sleep, alive=lambda p: True)
    assert out.getvalue() == "[1] me: So the plan is set.\n[2] call: Later.\n--- end of run\n"


def test_tty_shows_drafts_in_place_and_clears_them(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    write(tmp_path, EVENTS)
    out = io.StringIO()
    tail(tmp_path, tty=True, stream=out, sleep=lambda s: None, alive=lambda p: True, width=80)
    v = out.getvalue()
    assert "... me: so the plan is" in v
    assert "\r\033[K[1] me: So the plan is set.\n" in v
    assert v.endswith("\r\033[K[2] call: Sounds good.\n--- end of run\n")


def test_tty_colours_speakers_and_dims_drafts(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    write(tmp_path, EVENTS)
    out = io.StringIO()
    tail(tmp_path, tty=True, stream=out, sleep=lambda s: None, alive=lambda p: True, width=80)
    v = out.getvalue()
    assert "[1] \033[36mme\033[0m: So the plan is set." in v
    assert "[2] \033[35mcall\033[0m: Sounds good." in v
    assert "\033[2m... me: so the plan is\033[0m" in v


def test_no_color_and_non_tty_get_plain_text(tmp_path, monkeypatch):
    write(tmp_path, EVENTS)
    monkeypatch.setenv("NO_COLOR", "1")
    out = io.StringIO()
    tail(tmp_path, tty=True, stream=out, sleep=lambda s: None, alive=lambda p: True, width=80)
    assert "\033[3" not in out.getvalue() and "\033[2m" not in out.getvalue()
    monkeypatch.delenv("NO_COLOR")
    out = io.StringIO()
    tail(tmp_path, tty=False, stream=out, sleep=lambda s: None, alive=lambda p: True)
    assert "\033" not in out.getvalue()
