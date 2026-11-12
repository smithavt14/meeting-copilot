"""Everything that differs between macOS and Windows, in one place. The rest of the package asks these functions and
never reads ``sys.platform`` itself.

- Per-user directories: settings, run output, model cache.
- Whether a process is alive (``os.kill(pid, 0)`` ends the process on Windows, so it is never used there).
- Appending to a file that several processes append to (the run's ``lines.jsonl``).
- The terminal: ANSI escapes need VT mode switched on in a Windows console, and piped output needs UTF-8.
- The call's audio: a Core Audio process tap on macOS, WASAPI loopback on Windows.
- The system voice: ``say`` on macOS, System.Speech through PowerShell on Windows.
- How to put an environment variable in front of a command a user is told to run.

Other platforms (Linux) get XDG directories, the mic only, and no system voice.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, TextIO

APP = "meeting-copilot"


def _os(platform: str | None = None) -> str:
    return platform or sys.platform


# Per-user directories

def config_dir() -> Path:
    """Where the per-user ``copilot.toml`` lives."""
    if _os() == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    if _os() == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP


def data_dir() -> Path:
    """Where runs are written when neither ``--out`` nor ``COPILOT_OUT`` says."""
    if _os() == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    if _os() == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP


def cache_dir() -> Path:
    """Where downloaded models live when ``COPILOT_CACHE`` is not set."""
    if _os() == "darwin":
        return Path.home() / "Library" / "Caches" / APP
    if _os() == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP / "Cache"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / APP


# Processes

def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if _os() == "win32":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_alive_windows(pid: int) -> bool:
    """OpenProcess, then the exit code: a process that has exited stays openable while anyone holds a handle to it."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists, under another user
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


# Files

def open_append(path: str | Path) -> TextIO:
    """Opens ``path`` for appending UTF-8 text with ``\n`` line ends, such that whole writes from several processes
    never overwrite each other. POSIX ``O_APPEND`` does that already. On Windows a plain append is a seek and then a
    write, so two processes can land on the same offset; a handle opened with FILE_APPEND_DATA and no FILE_WRITE_DATA
    makes the system put every write at the end of the file."""
    if _os() != "win32":
        return open(path, "a", encoding="utf-8", newline="\n")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                wintypes.DWORD, wintypes.HANDLE)
    file_append_data, synchronize = 0x0004, 0x00100000
    share_all = 0x1 | 0x2 | 0x4  # read, write, delete: readers and other appenders keep working
    open_always, normal = 4, 0x80
    handle = k32.CreateFileW(str(path), file_append_data | synchronize, share_all, None, open_always, normal, None)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
    return open(fd, "a", encoding="utf-8", newline="\n")


# The terminal

def setup_stdio() -> None:
    """Windows: stdin, stdout and stderr in UTF-8. A console already is; a pipe would otherwise use the ANSI code page,
    and a transcript line in Polish or Czech would raise UnicodeEncodeError on the way out."""
    if _os() != "win32":
        return
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def ansi_tty(stream: TextIO) -> bool:
    """True when ``stream`` is a terminal that renders ANSI escapes (colour, ``\\r\\033[K``). On Windows that takes
    VT processing, which this switches on; a console that refuses it is treated as no terminal, so plain lines print."""
    try:
        if not stream.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if _os() != "win32":
        return True
    return _enable_vt(stream)


def _enable_vt(stream: TextIO) -> bool:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    try:
        handle = msvcrt.get_osfhandle(stream.fileno())
    except (OSError, ValueError, AttributeError):
        return False
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    mode = wintypes.DWORD()
    if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    return bool(mode.value & vt) or bool(k32.SetConsoleMode(handle, mode.value | vt))


def env_prefix(name: str, value: str) -> str | None:
    """``NAME="value" `` to put in front of a command, or None where the usual shell (PowerShell, cmd) has no such
    syntax and the caller should pass the value as a flag instead."""
    if _os() == "win32":
        return None
    return f'{name}="{value}" '


# The call's audio

def call_source(app: str | None, pid: int | None, loopback: bool, log: Callable[[str], None]):
    """The source for the ``call`` stream, or None when no flag asks for one. Raises ValueError with what to do
    instead when the flags do not fit this OS."""
    from meeting_copilot.sources import LoopbackSource, MacTapSource

    if _os() == "win32":
        if app or pid:
            log("Windows loopback hears every app; --app and --pid are ignored")
        return LoopbackSource() if (loopback or app or pid) else None
    if _os() == "darwin":
        if app or pid:
            return MacTapSource(app=app, pid=pid)
        if loopback:
            raise ValueError("--loopback is for Windows; on a Mac use --app <name>")
        return None
    if app or pid or loopback:
        raise ValueError("the call's audio can be captured on macOS (--app) and Windows (--loopback) only")
    return None


# The system voice

def has_system_voice(platform: str | None = None) -> bool:
    return _os(platform) in ("darwin", "win32")


_WINDOWS_SPEECH = (
    "$ErrorActionPreference = 'Stop'; "
    "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; "
    "Add-Type -AssemblyName System.Speech; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo([int]$env:COPILOT_SPEECH_RATE, "
    "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono); "
    "$s.SetOutputToWaveFile($env:COPILOT_SPEECH_WAV, $f); "
    "$s.Speak([Console]::In.ReadToEnd()); $s.Dispose()"
)


def system_speech(text: str, wav: Path, rate: int = 22050, run=subprocess.run, platform: str | None = None) -> None:
    """Writes ``text`` spoken by the OS voice to ``wav``: 16-bit mono at ``rate``. The text goes on stdin and the
    path in the environment, so neither is ever parsed as part of a command."""
    if _os(platform) == "darwin":
        run(["say", "-o", str(wav), f"--data-format=LEI16@{rate}", "-f", "-"], input=text.encode("utf-8"),
            check=True, capture_output=True)
    elif _os(platform) == "win32":
        env = {**os.environ, "COPILOT_SPEECH_WAV": str(wav), "COPILOT_SPEECH_RATE": str(rate)}
        run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_SPEECH],
            input=text.encode("utf-8"), check=True, capture_output=True, env=env)
    else:
        raise RuntimeError("the system voice is macOS say or Windows System.Speech")
