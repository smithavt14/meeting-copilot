"""Model files: downloaded on first use into a per-user cache directory."""

from __future__ import annotations

import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

PARAKEET_NAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
PARAKEET_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{PARAKEET_NAME}.tar.bz2"
PARAKEET_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")

SILERO_URL = "https://github.com/snakers4/silero-vad/raw/v5.1.2/src/silero_vad/data/silero_vad.onnx"


def cache_dir() -> Path:
    """COPILOT_CACHE, else the platform's per-user cache directory."""
    env = os.environ.get("COPILOT_CACHE")
    if env:
        base = Path(env)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "meeting-copilot"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "meeting-copilot" / "Cache"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "meeting-copilot"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _fetch(url: str, dest: Path, log) -> None:
    log(f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "meeting-copilot"})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got, step = 0, 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            got += len(chunk)
            if total and got * 10 // total > step:
                step = got * 10 // total
                log(f"  {got * 100 // total}%")
    tmp.replace(dest)


def silero_path(log=lambda s: print(s, file=sys.stderr)) -> Path:
    path = cache_dir() / "silero_vad.onnx"
    if not path.exists():
        _fetch(SILERO_URL, path, log)
    return path


def parakeet_dir(log=lambda s: print(s, file=sys.stderr)) -> Path:
    root = cache_dir()
    model = root / PARAKEET_NAME
    if all((model / f).exists() for f in PARAKEET_FILES):
        return model
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        archive = Path(tmp) / f"{PARAKEET_NAME}.tar.bz2"
        _fetch(PARAKEET_URL, archive, log)
        log("unpacking")
        with tarfile.open(archive, "r:bz2") as tar:
            tar.extractall(tmp, filter="data")
        if model.exists():
            shutil.rmtree(model)
        shutil.move(str(Path(tmp) / PARAKEET_NAME), str(model))
    missing = [f for f in PARAKEET_FILES if not (model / f).exists()]
    if missing:
        raise RuntimeError(f"model download is missing {', '.join(missing)}")
    return model


SPEAKER_MODEL = "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
SPEAKER_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/{SPEAKER_MODEL}"


def speaker_model_path(log=lambda s: print(s, file=sys.stderr)) -> Path:
    """3D-Speaker ERes2Net-base (about 40 MB), the voice embeddings behind ``--diarize``."""
    path = cache_dir() / SPEAKER_MODEL
    if not path.exists():
        _fetch(SPEAKER_URL, path, log)
    return path


KOKORO_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
KOKORO_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")


def kokoro_paths(log=lambda s: print(s, file=sys.stderr)) -> tuple[Path, Path]:
    """Kokoro-82M (ONNX) and its voices file, downloaded on first use."""
    d = cache_dir() / "kokoro-v1.0"
    d.mkdir(exist_ok=True)
    for name in KOKORO_FILES:
        if not (d / name).exists():
            _fetch(f"{KOKORO_BASE}/{name}", d / name, log)
    return d / KOKORO_FILES[0], d / KOKORO_FILES[1]
