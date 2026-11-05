"""Does a line say the copilot's name? Speech recognition drops punctuation and mishears the name, so a few
variants count too. A variant that is also a common word ("call") counts only when it opens the line."""

from __future__ import annotations

import re

DEFAULT_NAME = "Claude"
DEFAULT_VARIANTS = ["cloud", "clod", "claud", "clawed", "claw", "clog", "claudia"]
DEFAULT_OPENING_VARIANTS = ["call"]

# Words that may come before the name when it opens a line ("hey Claude", "okay so Claude").
LEADERS = {"hey", "hi", "hello", "ok", "okay", "yo", "so", "and", "um", "uh", "oh", "alright", "right", "well",
           "quick", "question", "sorry", "actually"}

WORD = re.compile(r"[a-z0-9']+")


def words(text: str) -> list[str]:
    return WORD.findall(text.lower())


class NameMatcher:
    def __init__(self, name: str = DEFAULT_NAME, variants: list[str] | None = None,
                 opening_variants: list[str] | None = None) -> None:
        self.name = name.lower()
        self.anywhere = {self.name, *(v.lower() for v in (DEFAULT_VARIANTS if variants is None else variants))}
        self.opening = {v.lower() for v in (DEFAULT_OPENING_VARIANTS if opening_variants is None else opening_variants)}

    def __call__(self, text: str) -> bool:
        toks = words(text)
        for w in toks:
            if w in self.anywhere or (w.endswith("'s") and w[:-2] in self.anywhere):
                return True
        i = 0
        while i < len(toks) and i < 4 and toks[i] in LEADERS:
            i += 1
        return i < len(toks) and toks[i] in self.opening

    def command(self, text: str) -> str | None:
        """"mute" or "unmute" when the whole line is the name plus that command ("hey Claude, mute", "Claw mute",
        "Claude, stop listening", "Claude unmute", "Claude, resume"), else None."""
        toks = words(text)
        i = 0
        while i < len(toks) and toks[i] in LEADERS:
            i += 1
        rest = toks[i:]
        if not rest:
            return None
        head = rest[0]
        if not (head in self.anywhere or head in self.opening):
            return None
        tail = tuple(w for w in rest[1:] if w not in FILLER)
        return COMMANDS.get(tail)


FILLER = {"please", "now", "for", "a", "bit", "second", "minute", "thanks"}
COMMANDS = {
    ("mute",): "mute", ("mute", "yourself"): "mute", ("stop", "listening"): "mute", ("go", "quiet"): "mute",
    ("unmute",): "unmute", ("un", "mute"): "unmute", ("resume",): "unmute", ("start", "listening"): "unmute",
    ("keep", "listening"): "unmute", ("you", "can", "listen"): "unmute",
}
