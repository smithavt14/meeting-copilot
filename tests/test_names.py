from meeting_copilot.names import NameMatcher


def test_name_and_variants():
    m = NameMatcher()
    assert m("Claude, what's the date?")
    assert m("hey cloud can you check that")
    assert m("ask Claude's opinion")
    assert m("Claudia what did we say")
    assert not m("we should clone the repo")


def test_opening_only_variant():
    m = NameMatcher()
    assert m("Call, what's the status")
    assert m("okay so call what's next")
    assert not m("let's get on a call tomorrow")


def test_custom_name():
    m = NameMatcher("Robin", variants=["robbin"], opening_variants=[])
    assert m("Robin, check the numbers")
    assert m("robbin what time is it")
    assert not m("Claude, what time is it")
