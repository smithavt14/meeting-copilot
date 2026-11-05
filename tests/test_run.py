import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from meeting_copilot import run as runmod

SRC = str(Path(__file__).resolve().parents[1] / "src")


def holder():
    """A live process that is not this one."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def test_reserve_refuses_while_a_live_process_holds_the_run(tmp_path):
    p = holder()
    try:
        (tmp_path / "current.json").write_text(json.dumps({"pid": p.pid, "dir": None}))
        with pytest.raises(runmod.RunBusy) as e:
            runmod.reserve(tmp_path)
        assert e.value.pid == p.pid
    finally:
        p.kill(); p.wait()


def test_reserve_takes_over_a_dead_holder(tmp_path):
    p = holder(); p.kill(); p.wait()
    (tmp_path / "current.json").write_text(json.dumps({"pid": p.pid, "dir": "/gone"}))
    runmod.reserve(tmp_path)
    assert json.loads((tmp_path / "current.json").read_text()) == {"pid": os.getpid(), "dir": None}
    runmod.publish(tmp_path, tmp_path)
    assert json.loads((tmp_path / "current.json").read_text())["dir"] == str(tmp_path.resolve())
    runmod.release(tmp_path)
    assert not (tmp_path / "current.json").exists()


def test_two_listeners_racing_get_one_run(tmp_path):
    code = ("import sys, time; sys.path.insert(0, sys.argv[2]); from pathlib import Path; "
            "from meeting_copilot import run as r\n"
            "try:\n    r.reserve(Path(sys.argv[1])); print('ok', flush=True); time.sleep(1.5)\n"
            "except r.RunBusy:\n    print('busy', flush=True)\n")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), SRC], stdout=subprocess.PIPE, text=True)
             for _ in range(4)]
    results = sorted(p.communicate(timeout=20)[0].strip() for p in procs)
    assert results == ["busy", "busy", "busy", "ok"]


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_find_run_waits_for_a_run_that_is_starting(tmp_path):
    def start_later():
        time.sleep(0.4)
        runmod.reserve(tmp_path)
        d = runmod.new_run_dir(tmp_path)
        (d / "lines.jsonl").write_text('{"ev": "start"}\n')
        runmod.publish(tmp_path, d)

    old = runmod.new_run_dir(tmp_path)
    (old / "lines.jsonl").write_text('{"ev": "stop"}\n')
    threading.Thread(target=start_later).start()
    found = runmod.find_run(tmp_path, wait_s=5, poll_s=0.05)
    try:
        assert found is not None and found != old
    finally:
        runmod.release(tmp_path)


def test_find_run_falls_back_to_the_newest_finished_run(tmp_path):
    old = runmod.new_run_dir(tmp_path)
    (old / "lines.jsonl").write_text('{"ev": "stop"}\n')
    clock = FakeClock()
    assert runmod.find_run(tmp_path, wait_s=15, clock=clock, sleep=clock.sleep) == old
    assert clock.t >= 15


def test_find_run_with_nothing(tmp_path):
    clock = FakeClock()
    assert runmod.find_run(tmp_path, wait_s=1, clock=clock, sleep=clock.sleep) is None
