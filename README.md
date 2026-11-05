# meeting-copilot

**Recording consent comes first.** This tool records and transcribes everyone it hears. Tell the room, and the people on the call, before you start it. Many places (including several US states and much of Europe) require every party's consent to record a conversation; check the rules where you and the other people are.

meeting-copilot listens to two audio streams, your microphone (`me`) and the computer's audio (`call`), cuts them into utterances, transcribes them, and appends numbered lines to a JSONL file. A Claude Code session reads those lines with `copilot wake` and answers in text when someone says "Claude, ...". Nothing joins the call and nothing speaks. It runs on macOS and Windows; the Windows path is untested so far.

```
mic ──────────────┐
                  ├─ Silero VAD ─ transcribe (drafts, then a final) ─ echo backstop ─ lines.jsonl ─ copilot wake ─ Claude Code
app or loopback ──┘
```

## Install

You need [uv](https://docs.astral.sh/uv/) and git. uv installs the pinned Python (3.12) by itself; the speech and VAD wheels are not built for the newest Python releases yet.

```sh
git clone https://github.com/smithavt14/meeting-copilot.git
cd meeting-copilot
uv tool install --python 3.12 --editable .
copilot download
```

`uv tool install` puts `copilot` in `~/.local/bin` (run `uv tool update-shell` once if that is not on your PATH). `copilot download` fetches the Silero VAD model (2 MB) and Parakeet TDT v3 (about 490 MB) into your user cache directory; without it, the first `copilot listen` does the same. If you would rather not install the command, run `uv sync` and use `uv run copilot ...` from the repo.

### macOS: the call's audio

The call's audio comes from a Core Audio process tap in a small Swift binary, `tap`. It needs macOS 14.4 or newer and the Xcode command line tools (`xcode-select --install`). Build it once:

```sh
scripts/build-tap.sh
```

Two permissions, both for the app you run `copilot` from (Terminal, iTerm, Ghostty, or the app hosting your Claude Code session):

- **Microphone**: macOS asks the first time `copilot listen` opens the mic.
- **System audio recording**: System Settings > Privacy & Security > Screen & System Audio Recording, under "System Audio Recording Only", turn on the terminal app. macOS may ask on the first tap instead. Without it the tap delivers silence, and `copilot listen` says it has heard no audio from the tap after 15 seconds.

`tap/build/tap --list` shows the processes Core Audio knows about, and which are playing.

### Windows (untested)

No extra build step: the call's audio is WASAPI loopback through PyAudioWPatch, installed with the rest. Loopback hears **everything** the default speakers play, including notifications and music, so mute other apps during a call. Capturing a single app is a later step. Nobody has run this path on a Windows machine yet; reports welcome.

## Use

```sh
copilot listen --app "Google Chrome"   # macOS: your mic plus Chrome's audio (Google Meet); also zoom.us, "Microsoft Teams", a bundle id
copilot listen --loopback              # Windows: your mic plus everything the speakers play
copilot listen                         # in person: the mic only
copilot stop                           # from any other shell: stop the running listen cleanly (or Ctrl-C)
```

Each finished line prints as it lands (`[12 14:03:21] call: ...`), and the run's directory is printed at the start. Other flags: `--no-mic`, `--mic-device <name or index>`, `--name <wake name>`, `--transcribe local|cloud`, `--draft-ms <ms>`. `copilot listen --help` lists them all.

**Wear headphones.** On laptop speakers the mic hears the call, and call lines come back as `me` lines. A backstop drops a `me` line that mostly repeats a `call` line from the last few seconds, but some will slip through.

### Transcription: local or cloud

- `local` (the default): NVIDIA Parakeet TDT 0.6B v3 through sherpa-onnx, on the CPU. Nothing leaves the machine.
- `cloud`: OpenRouter's `/api/v1/audio/transcriptions`, one WAV per request. Set `OPENROUTER_API_KEY` in the environment and pass `--transcribe cloud`. The default model is `openai/whisper-large-v3-turbo`; `--cloud-model` picks another (`curl "https://openrouter.ai/api/v1/models?output_modalities=transcription"` lists them). Use it on a machine where local transcription is slow. Audio goes to OpenRouter and the model's provider.

### Drafts

While someone is still talking, the audio so far is re-transcribed about once a second and written as a `draft` event. When the utterance ends (500 ms of silence), one final pass replaces it; the final is the record. A draft that contains the name wakes the copilot before the question is finished. Only one transcription runs at a time across both streams, and a final never waits behind more than one draft, so a slow machine makes fewer drafts and the finals stay on time. In cloud mode each draft is a request; `--draft-ms 0` turns drafts off.

### Output

Each run writes `<out>/<YYYY-MM-DD_HHMMSS>/lines.jsonl`, where `<out>` is `--out`, `COPILOT_OUT`, or a per-user data directory (`~/Library/Application Support/meeting-copilot/out` on macOS, `%LOCALAPPDATA%\meeting-copilot\out` on Windows). One JSON object per line:

```json
{"ev": "line", "n": 12, "speaker": "call", "text": "Claude, what did we decide about the launch date?", "start": "2026-11-05T14:03:21.412-05:00", "end": "2026-11-05T14:03:24.020-05:00", "utt": 7, "named": true, "latency_ms": 655, "drafts": 2}
{"ev": "draft", "d": 31, "speaker": "me", "utt": 9, "text": "so the next step", "start": "2026-11-05T14:03:26.100-05:00", "named": false}
```

There are also `start`, `echo` (a dropped speaker-echo line), `error` and `stop` events.

### wake

```sh
copilot wake --since 0 --drafts-since 0
```

Blocks until one of these, then prints the new lines and `--- seen=<n> drafts=<d>` with markers:

- a final line says the name (`CALL`);
- a draft says the name: the first draft of an utterance with no final yet (`DRAFT`);
- 8 new lines (`--lines`);
- the run stops (`END`);
- 10 minutes pass (`TIMEOUT`, `--max-wait` in seconds).

Pass `seen` and `drafts` back as `--since` and `--drafts-since` next time.

### Settings

Flags win; otherwise `copilot.toml` in the current directory (or `--config <file>`) sets any of:

```toml
name = "Claude"
variants = ["cloud", "clod", "claud", "clawed", "claw", "clog", "claudia"]  # mishearings that count anywhere
opening_variants = ["call"]   # count only when they open the line
transcribe = "local"          # or "cloud"
cloud_model = "openai/whisper-large-v3-turbo"
language = "en"               # sent to the cloud model; "" to auto-detect
draft_ms = 1000
silence_ms = 500
pre_roll_ms = 300
vad_threshold = 0.5
wake_lines = 8
max_wait_s = 600
```

## The Claude Code skill

`.claude/skills/meeting-copilot/SKILL.md` is a project skill: Claude Code loads it when you start a session in this repo. Say "copilot this meeting" and it starts `copilot listen` in the background, runs `wake` in a loop, answers "Claude, ..." questions in text, fact-checks, scopes ideas with subagents, stays quiet otherwise, and stops the run when you say "stop the meeting".

To use it from any project, link it into your personal skills:

```sh
mkdir -p ~/.claude/skills
ln -s "$PWD/.claude/skills/meeting-copilot" ~/.claude/skills/meeting-copilot
```

(On Windows, copy the folder to `%USERPROFILE%\.claude\skills\meeting-copilot` instead.)

## Test without a meeting

`copilot listen --file <wav>` plays a WAV file through the same VAD, transcription and echo path, at real time (`--speed 0` for as fast as possible), as the `call` stream (`--as me` for the mic stream).

```sh
uv run python scripts/make_sample.py sample.wav   # macOS: invented sentences spoken by `say`
copilot listen --file sample.wav
uv run pytest
```

## License

MIT. See [LICENSE](LICENSE).
