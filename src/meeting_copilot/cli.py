"""copilot: listen to a meeting, write labelled lines, and let a Claude Code session read along."""

from __future__ import annotations

import argparse
import json
import queue
import signal
import sys
import threading
import time
import tomllib
from pathlib import Path

from meeting_copilot import run as runmod
from meeting_copilot.names import DEFAULT_NAME, DEFAULT_OPENING_VARIANTS, DEFAULT_VARIANTS, NameMatcher
from meeting_copilot.transcribe import DEFAULT_CLOUD_MODEL

DEFAULTS = {
    "name": DEFAULT_NAME,
    "variants": DEFAULT_VARIANTS,
    "opening_variants": DEFAULT_OPENING_VARIANTS,
    "transcribe": "local",
    "cloud_model": DEFAULT_CLOUD_MODEL,
    "language": "en",
    "draft_ms": 1000,
    "silence_ms": 500,
    "pre_roll_ms": 300,
    "vad_threshold": 0.5,
    "wake_lines": 8,
    "max_wait_s": 600,
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def load_settings(path: str | None) -> dict:
    """Defaults, then ``copilot.toml`` (``--config`` or the current directory), then flags."""
    settings = dict(DEFAULTS)
    p = Path(path) if path else Path("copilot.toml")
    if p.exists():
        with open(p, "rb") as f:
            data = tomllib.load(f)
        unknown = sorted(set(data) - set(DEFAULTS))
        if unknown:
            log(f"{p}: ignoring unknown settings: {', '.join(unknown)}")
        settings.update({k: v for k, v in data.items() if k in DEFAULTS})
    elif path:
        raise SystemExit(f"no settings file at {p}")
    return settings


def matcher(s: dict) -> NameMatcher:
    return NameMatcher(s["name"], list(s["variants"]), list(s["opening_variants"]))


# listen

class Pump:
    """Moves audio from a source's thread to its stream on a thread of its own, so capture never waits on the VAD."""

    def __init__(self, stream) -> None:
        self.stream = stream
        self.q: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def __call__(self, samples, wall_end) -> None:
        self.q.put((samples, wall_end))

    def _run(self) -> None:
        while (item := self.q.get()) is not None:
            self.stream.feed(*item)

    def close(self) -> None:
        self.q.put(None)
        self.thread.join()


def cmd_listen(a: argparse.Namespace) -> int:
    from meeting_copilot.models import silero_path
    from meeting_copilot.pipeline import Handlers, Scheduler, Stream
    from meeting_copilot.sources import FileSource, LoopbackSource, MacTapSource, MicSource
    from meeting_copilot.transcribe import make_engine
    from meeting_copilot.vad import SileroVAD

    s = load_settings(a.config)
    for key in ("name", "transcribe", "cloud_model", "language", "draft_ms"):
        if getattr(a, key, None) is not None:
            s[key] = getattr(a, key)
    out = Path(a.out) if a.out else runmod.default_out()
    out.mkdir(parents=True, exist_ok=True)
    pid = runmod.running_pid(out)
    if pid:
        log(f"a run is already going (pid {pid}); copilot stop ends it")
        return 1

    # Sources, by name: "me" is the mic, "call" is the computer's audio.
    sources: dict[str, object] = {}
    if a.file:
        sources[a.file_as] = FileSource(a.file, speed=a.speed)
    else:
        if not a.no_mic:
            sources["me"] = MicSource(a.mic_device)
        if sys.platform == "win32" and (a.loopback or a.app):
            if a.app:
                log("Windows loopback hears every app; --app is ignored")
            sources["call"] = LoopbackSource()
        elif a.app or a.pid:
            if sys.platform != "darwin":
                log("--app needs macOS 14.4 or newer (or --loopback on Windows)")
                return 1
            sources["call"] = MacTapSource(app=a.app, pid=a.pid)
        elif a.loopback:
            log("--loopback is for Windows; on a Mac use --app <name>")
            return 1
    if not sources:
        log("nothing to listen to: drop --no-mic, or add --app, --loopback or --file")
        return 1

    log(f"transcription: {s['transcribe']}" + (f" ({s['cloud_model']})" if s["transcribe"] == "cloud" else " (Parakeet TDT v3)"))
    try:
        engine = make_engine(s["transcribe"], s["cloud_model"], s["language"] or None)
        vad_model = silero_path(log)
    except Exception as e:
        log(f"cannot start: {e}")
        return 1

    run_dir = runmod.new_run_dir(out)
    (out / runmod.CURRENT).write_text(json.dumps({"pid": __import__("os").getpid(), "dir": str(run_dir.resolve())}))
    names = matcher(s)
    rec = runmod.Recorder(run_dir, names, echo={"me", "call"} <= set(sources), status=log,
                          tty=sys.stdout.isatty())
    scheduler = Scheduler(engine)
    handlers = Handlers(speech=rec.speech, speech_end=rec.speech_end, draft=rec.draft, final=rec.final,
                        empty=rec.empty, error=rec.error)
    streams, pumps = {}, {}
    for name in sources:
        streams[name] = Stream(name, SileroVAD(vad_model), scheduler, handlers, threshold=float(s["vad_threshold"]),
                               silence_s=s["silence_ms"] / 1000, pre_roll_s=s["pre_roll_ms"] / 1000,
                               draft_s=max(0, int(s["draft_ms"])) / 1000)
        pumps[name] = Pump(streams[name])
    rec.emit("start", t=runmod.iso(time.time()), streams=sorted(sources), transcribe=s["transcribe"],
             model=s["cloud_model"] if s["transcribe"] == "cloud" else "parakeet-tdt-0.6b-v3",
             draft_ms=int(s["draft_ms"]), name=s["name"], file=str(a.file) if a.file else None)

    stopping = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())

    started = []
    try:
        for name, src in sources.items():
            src.start(pumps[name], log)
            started.append(src)
    except Exception as e:
        log(f"cannot start {name}: {e}")
        stopping.set()
    desc = {"me": "me (mic)", "call": "call (" + (a.app or (f"pid {a.pid}" if a.pid else "loopback")) + ")"}
    if a.file:
        desc = {a.file_as: f"{a.file_as} ({Path(a.file).name})"}
    log("listening: " + ", ".join(desc[n] for n in sources))
    log(f"lines {rec.path}")
    log("stop with Ctrl-C or: copilot stop")

    tap_warned = False
    t0 = time.time()
    while not stopping.is_set():
        time.sleep(0.2)
        rec.tick()
        if (run_dir / runmod.STOP).exists():
            break
        if a.file and all(src.done.is_set() for src in sources.values()):
            break
        for src in sources.values():
            if isinstance(src, MacTapSource):
                if src.done.is_set() and not tap_warned:
                    log("the tap stopped; the call stream is gone")
                    rec.error("tap exited")
                    tap_warned = True
                elif not src.got_audio.is_set() and time.time() - t0 > 15 and not tap_warned:
                    log("no audio from the tap yet: is the app playing sound, and does this terminal have audio "
                        "capture permission (README)?")
                    tap_warned = True

    for src in started:
        src.stop()
    for p in pumps.values():
        p.close()
    for st in streams.values():
        st.close()
    rec.flush()
    rec.emit("stop", t=runmod.iso(time.time()))
    rec.close()
    try:
        cur = json.loads((out / runmod.CURRENT).read_text())
        if Path(cur.get("dir", "")) == run_dir.resolve():
            (out / runmod.CURRENT).unlink()
    except (OSError, ValueError):
        pass
    log(f"stopped: {rec.n} lines, {rec.d} drafts, {len(rec.echoes)} echo lines dropped")
    log(f"lines {rec.path}")
    return 0


# wake, stop, download

def cmd_wake(a: argparse.Namespace) -> int:
    from meeting_copilot.wake import wake

    s = load_settings(a.config)
    out = Path(a.out) if a.out else runmod.default_out()
    run_dir = Path(a.run) if a.run else runmod.current_run(out)
    return wake(run_dir, a.since, a.drafts_since, lines=a.lines or int(s["wake_lines"]),
                max_wait=a.max_wait if a.max_wait is not None else float(s["max_wait_s"]))


def cmd_stop(a: argparse.Namespace) -> int:
    out = Path(a.out) if a.out else runmod.default_out()
    pid = runmod.running_pid(out)
    run_dir = runmod.current_run(out)
    if not pid or run_dir is None:
        log("no run is going")
        return 1
    (run_dir / runmod.STOP).write_text("")
    deadline = time.time() + 30
    while time.time() < deadline and runmod.pid_alive(pid):
        time.sleep(0.2)
    if runmod.pid_alive(pid):
        log(f"the run (pid {pid}) has not stopped after 30 s")
        return 1
    print(f"stopped {run_dir / runmod.LINES}")
    return 0


def cmd_download(a: argparse.Namespace) -> int:
    from meeting_copilot.models import cache_dir, parakeet_dir, silero_path

    silero_path(log)
    parakeet_dir(log)
    print(f"models ready in {cache_dir()}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="copilot", description=__doc__)
    p.add_argument("--config", help="settings file (default: ./copilot.toml if present)")
    p.add_argument("--out", help="where runs are written (default: $COPILOT_OUT, else a per-user data directory)")
    sub = p.add_subparsers(dest="cmd", required=True)

    li = sub.add_parser("listen", help="listen and write lines until stopped")
    li.add_argument("--app", help="macOS: the app whose audio is the call (\"Google Chrome\", zoom.us, a bundle id)")
    li.add_argument("--pid", type=int, help="macOS: tap one process by pid instead of an app")
    li.add_argument("--loopback", action="store_true", help="Windows: everything the speakers play is the call")
    li.add_argument("--no-mic", action="store_true", help="no microphone stream")
    li.add_argument("--mic-device", help="microphone name or index (default: the system input)")
    li.add_argument("--file", help="a WAV file fed through the same path instead of live audio")
    li.add_argument("--as", dest="file_as", choices=["call", "me"], default="call", help="the stream --file plays as")
    li.add_argument("--speed", type=float, default=1.0, help="--file pace: 1 is real time, 0 as fast as possible")
    li.add_argument("--transcribe", choices=["local", "cloud"], help="local Parakeet (default) or OpenRouter")
    li.add_argument("--cloud-model", dest="cloud_model", help=f"OpenRouter model (default {DEFAULT_CLOUD_MODEL})")
    li.add_argument("--language", help="ISO-639-1 code sent to the cloud model (default en; empty to auto-detect)")
    li.add_argument("--draft-ms", dest="draft_ms", type=int, help="draft interval in ms; 0 turns drafts off (default 1000)")
    li.add_argument("--name", help="the name that calls the copilot (default Claude)")
    li.set_defaults(func=cmd_listen)

    wk = sub.add_parser("wake", help="wait for the next batch of lines for the copilot session")
    wk.add_argument("--since", type=int, default=0, help="the last line already read (seen= from the last wake)")
    wk.add_argument("--drafts-since", dest="drafts_since", type=int, help="the last draft already read (drafts=)")
    wk.add_argument("--lines", type=int, help="return after this many new lines (default 8)")
    wk.add_argument("--max-wait", dest="max_wait", type=float, help="return after this many seconds (default 600)")
    wk.add_argument("--run", help="a run directory (default: the current or newest run)")
    wk.set_defaults(func=cmd_wake)

    st = sub.add_parser("stop", help="stop the running listen cleanly")
    st.set_defaults(func=cmd_stop)

    dl = sub.add_parser("download", help="fetch the VAD and Parakeet models now instead of on first listen")
    dl.set_defaults(func=cmd_download)
    return p


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
