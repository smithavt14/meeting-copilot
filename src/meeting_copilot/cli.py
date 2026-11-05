"""copilot: listen to a meeting, write labelled lines, and let a Claude Code session read along."""

from __future__ import annotations

import argparse
import json
import os
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
    "voice": "auto",
    "kokoro_voice": "af_heart",
    "voice_speed": 1.0,
    "elevenlabs_voice": "JBFqnCBsd6RMkjVDRZzb",
    "elevenlabs_model": "eleven_flash_v2_5",
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def user_config() -> Path:
    """The per-user settings file: COPILOT_CONFIG, else the platform's config directory."""
    env = os.environ.get("COPILOT_CONFIG")
    if env:
        return Path(env)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "meeting-copilot" / "copilot.toml"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "meeting-copilot" / "copilot.toml"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "meeting-copilot" / "copilot.toml"


def load_settings(path: str | None) -> dict:
    """Defaults, then the per-user ``copilot.toml``, then ``./copilot.toml`` (or ``--config``) on top, then flags.
    The per-user file is what makes settings hold when the skill runs listen and say from any directory."""
    settings = dict(DEFAULTS)
    local = Path(path) if path else Path("copilot.toml")
    if path and not local.exists():
        raise SystemExit(f"no settings file at {local}")
    user = user_config()
    for p in (user, local):
        if not p.exists() or (p is local and user.exists() and p.resolve() == user.resolve()):
            continue
        with open(p, "rb") as f:
            data = tomllib.load(f)
        unknown = sorted(set(data) - set(DEFAULTS))
        if unknown:
            log(f"{p}: ignoring unknown settings: {', '.join(unknown)}")
        settings.update({k: v for k, v in data.items() if k in DEFAULTS})
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
    from meeting_copilot.sources import FileSource, LoopbackSource, MacTapSource, MicSource

    s = load_settings(a.config)
    for key in ("name", "transcribe", "cloud_model", "language", "draft_ms"):
        if getattr(a, key, None) is not None:
            s[key] = getattr(a, key)
    out = Path(a.out) if a.out else runmod.default_out()
    out.mkdir(parents=True, exist_ok=True)

    # Sources, by name: "me" is the mic, "call" is the computer's audio.
    sources: dict[str, object] = {}
    if a.file:
        if not Path(a.file).is_file():
            log(f"no such file: {a.file}")
            return 1
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

    # Claim the run before the slow model load, so a second listener stops here.
    try:
        runmod.reserve(out)
    except runmod.RunBusy as e:
        log(f"a run is already going (pid {e.pid}); copilot stop ends it")
        return 1
    try:
        return _listen(a, s, out, sources)
    finally:
        runmod.release(out)


def tail_command(a: argparse.Namespace) -> str:
    """The exact ``copilot tail`` command for this run's output directory."""
    if a.out:
        return f'copilot --out "{a.out}" tail'
    if os.environ.get("COPILOT_OUT"):
        return f'COPILOT_OUT="{os.environ["COPILOT_OUT"]}" copilot tail'
    return "copilot tail"


def _listen(a: argparse.Namespace, s: dict, out: Path, sources: dict) -> int:
    from meeting_copilot.models import silero_path
    from meeting_copilot.pipeline import Handlers, Scheduler, Stream
    from meeting_copilot.sources import MacTapSource
    from meeting_copilot.transcribe import make_engine
    from meeting_copilot.vad import SileroVAD

    log(f"transcription: {s['transcribe']}" + (f" ({s['cloud_model']})" if s["transcribe"] == "cloud" else " (Parakeet TDT v3)"))
    try:
        engine = make_engine(s["transcribe"], s["cloud_model"], s["language"] or None)
        vad_model = silero_path(log)
    except Exception as e:
        log(f"cannot start: {e}")
        return 1

    run_dir = runmod.new_run_dir(out)
    names = matcher(s)
    rec = runmod.Recorder(run_dir, names, echo={"me", "call"} <= set(sources), status=log,
                          tty=sys.stdout.isatty())
    scheduler = Scheduler(engine)
    if s["transcribe"] == "cloud":
        # Muted audio stays on this machine when Parakeet is installed; otherwise only short utterances go out.
        from meeting_copilot.models import PARAKEET_FILES, PARAKEET_NAME, cache_dir

        if all((cache_dir() / PARAKEET_NAME / f).exists() for f in PARAKEET_FILES):
            from meeting_copilot.transcribe import LocalParakeet

            scheduler.muted_engine = LocalParakeet()
        else:
            scheduler.muted_max_s = 3.0
    streams: dict = {}

    def on_mute(muted: bool) -> None:
        scheduler.muted = muted
        for st in streams.values():
            st.mark_muted()  # an utterance open across the change is muted whole

    rec.on_mute = on_mute
    handlers = Handlers(speech=rec.speech, speech_end=rec.speech_end, draft=rec.draft, final=rec.final,
                        empty=rec.empty, error=rec.error)
    pumps = {}
    for name in sources:
        streams[name] = Stream(name, SileroVAD(vad_model), scheduler, handlers, threshold=float(s["vad_threshold"]),
                               silence_s=s["silence_ms"] / 1000, pre_roll_s=s["pre_roll_ms"] / 1000,
                               draft_s=max(0, int(s["draft_ms"])) / 1000)
        pumps[name] = Pump(streams[name])
    rec.emit("start", t=runmod.iso(time.time()), pid=os.getpid(), streams=sorted(sources), transcribe=s["transcribe"],
             model=s["cloud_model"] if s["transcribe"] == "cloud" else "parakeet-tdt-0.6b-v3",
             draft_ms=int(s["draft_ms"]), name=s["name"], file=str(a.file) if a.file else None)
    runmod.publish(out, run_dir)

    stopping = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())

    def shutdown(started: list) -> None:
        for src in started:
            src.stop()
        for p in pumps.values():
            p.close()
        for st in streams.values():
            st.close()
        rec.flush()
        rec.emit("stop", t=runmod.iso(time.time()))
        rec.close()

    started = []
    for name, src in sources.items():
        try:
            src.start(pumps[name], log)
        except Exception as e:
            rec.error(f"cannot start {name}: {e}")  # also printed
            shutdown(started)
            return 1
        started.append(src)
    desc = {"me": "me (mic)", "call": "call (" + (a.app or (f"pid {a.pid}" if a.pid else "loopback")) + ")"}
    if a.file:
        desc = {a.file_as: f"{a.file_as} ({Path(a.file).name})"}
    log("listening: " + ", ".join(desc[n] for n in sources))
    log(f"lines {rec.path}")
    log(f"watch it live in another terminal: {tail_command(a)}")
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
                    log("no audio from the tap yet: is the app playing sound, and is System Audio Recording on for "
                        "this terminal (see the README)?")
                    tap_warned = True

    shutdown(started)
    log(f"stopped: {rec.n} lines, {rec.d} drafts, {len(rec.echoes)} echo lines dropped")
    log(f"lines {rec.path}")
    return 0


# wake, stop, download

def cmd_wake(a: argparse.Namespace) -> int:
    from meeting_copilot.wake import wake

    s = load_settings(a.config)
    out = Path(a.out) if a.out else runmod.default_out()
    run_dir = Path(a.run) if a.run else runmod.find_run(out)
    return wake(run_dir, a.since, a.drafts_since, lines=a.lines or int(s["wake_lines"]),
                max_wait=a.max_wait if a.max_wait is not None else float(s["max_wait_s"]), mutes_since=a.mutes_since)


def cmd_stop(a: argparse.Namespace) -> int:
    out = Path(a.out) if a.out else runmod.default_out()
    pid = runmod.running_pid(out)
    run_dir = runmod.find_run(out) if pid else None
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


def cmd_tail(a: argparse.Namespace) -> int:
    from meeting_copilot.tail import tail

    out = Path(a.out) if a.out else runmod.default_out()
    run_dir = Path(a.run) if a.run else runmod.find_run(out)
    if run_dir is None:
        log("no run found")
        return 2
    try:
        return tail(run_dir, a.since, tty=sys.stdout.isatty())
    except KeyboardInterrupt:
        return 0


def live_run(out: Path) -> Path | None:
    """The running listen's directory, without waiting; None when nothing is listening."""
    cur = runmod.read_current(out)
    if cur and cur.get("dir") and runmod.pid_alive(int(cur.get("pid") or 0)):
        d = Path(cur["dir"])
        return d if (d / runmod.LINES).exists() else None
    return None


def cmd_say(a: argparse.Namespace) -> int:
    import uuid

    from meeting_copilot import voice

    text = " ".join(a.text).strip() if a.text else sys.stdin.read().strip()
    if not text:
        log("nothing to say: pass the text, or pipe it in")
        return 1
    s = load_settings(a.config)
    try:
        choice = a.voice or s["voice"]
        engines = voice.order(choice)
    except ValueError as e:
        log(str(e))
        return 1
    out = Path(a.out) if a.out else runmod.default_out()
    run_dir = live_run(out)
    lines = run_dir / runmod.LINES if run_dir else None
    said_id = uuid.uuid4().hex[:8]
    started: dict = {}

    def on_start(engine: str, t: float) -> None:
        started["t"] = t
        if lines:
            runmod.append_event(lines, "said", id=said_id, phase="start", text=text, start=runmod.iso(t), engine=engine)

    try:
        result = voice.speak(text, engines, voice.factories(s), voice.Player(), on_start=on_start, log=log,
                             explicit=choice not in (None, "", "auto"))
    except Exception as e:
        log(f"cannot speak: {e}")
        return 1
    finally:
        if lines and "t" in started:
            runmod.append_event(lines, "said", id=said_id, phase="end", text=text, start=runmod.iso(started["t"]),
                                end=runmod.iso(time.time()))
    log(f"said with {result['engine']} (first audio after {result['first_audio_ms']} ms)")
    return 0


def cmd_mute(a: argparse.Namespace) -> int:
    out = Path(a.out) if a.out else runmod.default_out()
    run_dir = live_run(out)
    if run_dir is None:
        log("no run is going")
        return 1
    ev = "mute" if a.cmd == "mute" else "unmute"
    runmod.append_event(run_dir / runmod.LINES, ev, by="command", t=runmod.iso(time.time()))
    print("muted" if ev == "mute" else "unmuted")
    return 0


def cmd_download(a: argparse.Namespace) -> int:
    from meeting_copilot.models import cache_dir, kokoro_paths, parakeet_dir, silero_path

    silero_path(log)
    parakeet_dir(log)
    kokoro_paths(log)
    print(f"models ready in {cache_dir()}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="copilot", description=__doc__)
    p.add_argument("--config", help="settings file, read over the per-user copilot.toml (default: ./copilot.toml if present)")
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
    wk.add_argument("--mutes-since", dest="mutes_since", type=int, help="mute events already read (mutes=)")
    wk.add_argument("--lines", type=int, help="return after this many new lines (default 8)")
    wk.add_argument("--max-wait", dest="max_wait", type=float, help="return after this many seconds (default 600)")
    wk.add_argument("--run", help="a run directory (default: the current or newest run)")
    wk.set_defaults(func=cmd_wake)

    tl = sub.add_parser("tail", help="show the current run's lines as they land, with open drafts on a TTY")
    tl.add_argument("--since", type=int, default=0, help="start after this line number")
    tl.add_argument("--run", help="a run directory (default: the current or newest run)")
    tl.set_defaults(func=cmd_tail)

    sy = sub.add_parser("say", help="speak a short answer aloud (and mark it so the mic's copy is dropped)")
    sy.add_argument("text", nargs="*", help="what to say (default: read from stdin)")
    sy.add_argument("--voice", choices=["auto", "elevenlabs", "kokoro", "system"],
                    help="voice engine (default auto: elevenlabs with a key, else kokoro, else system)")
    sy.set_defaults(func=cmd_say)

    mu = sub.add_parser("mute", help="stop writing lines until unmute (it keeps listening for \"Claude, unmute\")")
    mu.set_defaults(func=cmd_mute)
    um = sub.add_parser("unmute", help="start writing lines again")
    um.set_defaults(func=cmd_mute)

    st = sub.add_parser("stop", help="stop the running listen cleanly")
    st.set_defaults(func=cmd_stop)

    dl = sub.add_parser("download", help="fetch the VAD, Parakeet and Kokoro models now instead of on first use")
    dl.set_defaults(func=cmd_download)
    return p


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
