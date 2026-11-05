import io
import json
import urllib.error

import numpy as np
import pytest

from meeting_copilot import voice

FAKE = "test-not-a-real-key"


class FakePlayer:
    def __init__(self):
        self.played = []

    def play(self, samples, rate):
        self.played.append((len(samples), rate))

    def close(self):
        pass


class Engine:
    def __init__(self, name, fail_start=False, fail_mid=False):
        self.name, self.fail_start, self.fail_mid = name, fail_start, fail_mid

    def chunks(self, text):
        if self.fail_start:
            raise RuntimeError(f"{self.name} broke")
        yield np.zeros(100, np.float32), 24000
        if self.fail_mid:
            raise RuntimeError("cut off")
        yield np.zeros(100, np.float32), 24000


def make(**engines):
    return {k: (lambda v=v: v() if callable(v) else v) for k, v in engines.items()}


def unavailable(msg):
    def f():
        raise voice.Unavailable(msg)
    return f


def test_order():
    assert voice.order(None) == ["elevenlabs", "kokoro", "system"]
    assert voice.order("auto") == ["elevenlabs", "kokoro", "system"]
    assert voice.order("kokoro") == ["kokoro", "system"]
    assert voice.order("system") == ["system"]
    with pytest.raises(ValueError):
        voice.order("robot")


def test_auto_skips_elevenlabs_without_a_key_and_uses_kokoro(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    logs, p = [], FakePlayer()
    f = voice.factories({})
    m = {"elevenlabs": f["elevenlabs"], "kokoro": lambda: Engine("kokoro"), "system": lambda: Engine("system")}
    r = voice.speak("hi", voice.order(None), m, p, log=logs.append, explicit=False)
    assert r["engine"] == "kokoro" and len(p.played) == 2
    assert logs == []  # a missing key is quiet in auto mode
    voice.speak("hi", voice.order("elevenlabs"), m, p, log=logs.append)
    assert logs == ["elevenlabs: ELEVENLABS_API_KEY is not set"]  # and named when asked for


def test_elevenlabs_first_when_the_key_is_set(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", FAKE)
    m = {"elevenlabs": lambda: Engine("elevenlabs"), "kokoro": lambda: Engine("kokoro"), "system": lambda: Engine("system")}
    assert voice.speak("hi", voice.order(None), m, FakePlayer())["engine"] == "elevenlabs"


def test_a_failing_engine_falls_back_before_any_audio():
    logs = []
    m = {"elevenlabs": lambda: Engine("elevenlabs", fail_start=True), "kokoro": unavailable("not installed"),
         "system": lambda: Engine("system")}
    r = voice.speak("hi", voice.order(None), m, FakePlayer(), log=logs.append)
    assert r["engine"] == "system"
    assert logs == ["elevenlabs: elevenlabs broke", "kokoro: not installed"]


def test_no_second_voice_after_audio_has_played():
    m = {"kokoro": lambda: Engine("kokoro", fail_mid=True), "system": lambda: Engine("system")}
    with pytest.raises(RuntimeError, match="cut off"):
        voice.speak("hi", ["kokoro", "system"], m, FakePlayer())


def test_nothing_works():
    m = {"system": unavailable("wrong OS")}
    with pytest.raises(RuntimeError, match="no voice could speak"):
        voice.speak("hi", ["system"], m, FakePlayer())


def test_on_start_gets_the_time_of_first_audio():
    seen = []
    t = iter([10.0, 10.4, 11.0])
    voice.speak("hi", ["kokoro"], {"kokoro": lambda: Engine("kokoro")}, FakePlayer(),
                on_start=lambda e, at: seen.append((e, at)), clock=lambda: next(t))
    assert seen == [("kokoro", 10.4)]


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_elevenlabs_request_and_pcm_stream(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", FAKE)
    seen = {}
    pcm = (np.arange(6000, dtype=np.int16)).tobytes() + b"\x01"  # an odd trailing byte is held, not misread

    def opener(req, timeout):
        seen["req"] = req
        return FakeResponse(pcm)

    eng = voice.ElevenLabs(opener=opener)
    chunks = list(eng.chunks("Hello there."))
    req = seen["req"]
    assert req.full_url == ("https://api.elevenlabs.io/v1/text-to-speech/JBFqnCBsd6RMkjVDRZzb/stream"
                            "?output_format=pcm_24000")
    assert req.get_method() == "POST" and req.get_header("Xi-api-key") == FAKE
    assert json.loads(req.data) == {"text": "Hello there.", "model_id": "eleven_flash_v2_5"}
    assert sum(len(c) for c, _ in chunks) == 6000 and all(r == 24000 for _, r in chunks)


def test_elevenlabs_error_names_the_status_not_the_key(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", FAKE)

    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"detail":"invalid"}'))

    with pytest.raises(RuntimeError) as e:
        list(voice.ElevenLabs(opener=opener).chunks("hi"))
    assert "401" in str(e.value) and FAKE not in str(e.value)


def test_system_voice_on_mac_and_windows(tmp_path, monkeypatch):
    import wave

    calls = []

    def run(args, input, check, capture_output):
        calls.append((args, input))
        path = next(a for a in args if a.endswith(".wav")) if "say" in args[0] else args[-1].split("'")[1]
        with wave.open(path, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050); w.writeframes(b"\x00\x00" * 2205)

    for platform in ("darwin", "win32"):
        out = list(voice.SystemVoice(run=run, platform=platform).chunks("Hi 'there'"))
        assert out[0][1] == 22050 and len(out[0][0]) == 2205
    assert calls[0][0][0] == "say" and calls[0][1] == b"Hi 'there'"  # text on stdin, never on the command line
    assert calls[1][0][0] == "powershell" and "Hi" not in calls[1][0][-1]
    with pytest.raises(voice.Unavailable):
        voice.SystemVoice(platform="linux")


def test_sentences_split_long_ones_at_commas():
    assert voice.sentences("One. Two? Three!") == ["One.", "Two?", "Three!"]
    long = "word " * 30 + ", " + "more " * 30
    parts = voice.sentences(long, max_chars=160)
    assert len(parts) == 2 and all(len(p) <= 160 for p in parts)
