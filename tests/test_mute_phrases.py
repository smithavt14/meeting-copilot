import pytest

from meeting_copilot.names import NameMatcher

m = NameMatcher()


@pytest.mark.parametrize("text", ["Claude, mute.", "Hey Claude, mute", "Claude mute", "Claw, mute.", "cloud mute please",
                                  "Claude, stop listening.", "Okay Claude, stop listening for a bit.", "Call, mute."])
def test_mute(text):
    assert m.command(text) == "mute"


@pytest.mark.parametrize("text", ["Claude, unmute.", "Claude un-mute", "Clawed, resume.", "Hey Claude, start listening",
                                  "Claude, you can listen now."])
def test_unmute(text):
    assert m.command(text) == "unmute"


@pytest.mark.parametrize("text", ["mute", "Claude, mute the call for me", "Claude, what does mute do",
                                  "I'll stop listening to Claude", "Let's get on a call, mute everyone",
                                  "Claude, how do I unmute in Zoom"])
def test_not_a_command(text):
    assert m.command(text) is None


@pytest.mark.parametrize("text,want", [
    ("Alright, Claude mute here.", "mute"),
    ("Claude, can you mute the meeting?", "mute"),
    ("Claude, stop listening for a sec", "mute"),
    ("Claude, you can listen", "unmute"),
    ("Claude, mute the rest of the call and email Bob", None),
    ("Okay so Claude what about mute buttons", None),
])
def test_command_phrases_from_the_live_test(text, want):
    assert NameMatcher().command(text) == want


def test_all_right_leader():
    assert NameMatcher().command("All right, Claud. Mute now.") == "mute"
