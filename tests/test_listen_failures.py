import json

import numpy as np

from meeting_copilot import cli


def fake_models(monkeypatch):
    import meeting_copilot.models as models
    import meeting_copilot.transcribe as tr
    import meeting_copilot.vad as vad

    monkeypatch.setattr(tr, "make_engine", lambda *a, **k: (lambda s: "words"))
    monkeypatch.setattr(models, "silero_path", lambda log=None: "unused")
    monkeypatch.setattr(vad, "SileroVAD", lambda path: (lambda chunk: 0.0))


def test_missing_file_fails_before_listening(tmp_path, capsys):
    rc = cli.main(["--out", str(tmp_path), "listen", "--file", str(tmp_path / "missing.wav")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "listening" not in err and "no such file" in err
    assert not (tmp_path / "current.json").exists()


def test_a_source_that_fails_to_start_exits_nonzero(tmp_path, monkeypatch, capsys):
    import meeting_copilot.sources as sources

    fake_models(monkeypatch)
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")  # exists; reading it is what fails

    def boom(self, on_audio, log):
        raise OSError("device unavailable")

    monkeypatch.setattr(sources.FileSource, "start", boom)
    rc = cli.main(["--out", str(tmp_path / "out"), "listen", "--file", str(wav)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "listening" not in err and "cannot start call: device unavailable" in err
    assert not (tmp_path / "out" / "current.json").exists()
    events = [json.loads(l) for l in next((tmp_path / "out").glob("*/lines.jsonl")).read_text().splitlines()]
    assert [e["ev"] for e in events] == ["start", "error", "stop"]


def test_a_second_listener_is_refused(tmp_path, capsys):
    import subprocess
    import sys

    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (tmp_path / "current.json").write_text(json.dumps({"pid": p.pid, "dir": None}))
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"")
        rc = cli.main(["--out", str(tmp_path), "listen", "--file", str(wav)])
        assert rc == 1
        assert f"a run is already going (pid {p.pid})" in capsys.readouterr().err
        assert json.loads((tmp_path / "current.json").read_text())["pid"] == p.pid  # left alone
    finally:
        p.kill(); p.wait()
