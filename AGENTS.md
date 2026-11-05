# Agent guide

Instructions for a coding agent (Claude Code, Codex, or similar) setting up meeting-copilot for a user, or working on this repo. Read `README.md` for what each command does.

Before anything else, tell the user that this records and transcribes everyone it hears, and that many places require every party's consent.

## Set it up for a user

1. **Check the prerequisites.**
   - macOS 14.4 or newer (`sw_vers -productVersion`), or Windows 10/11. On Windows, tell the user the Windows path is untested.
   - uv (`uv --version`). If it is missing, point the user to https://docs.astral.sh/uv/ and stop until it is installed.
   - git.
   - On macOS, the Xcode command line tools (`xcode-select -p`). If missing, ask the user to run `xcode-select --install`; it opens a dialog only they can click.
2. **Install.** From the repo root:
   ```sh
   uv tool install --python 3.12 --editable .
   ```
   If `copilot` is not found afterwards, run `uv tool update-shell` and open a new shell, or use `uv run copilot ...` from the repo.
3. **Ask before downloading the models.** They take about 840 MB in the user cache directory: Parakeet (about 490 MB) for local transcription, Kokoro (about 350 MB) for the local voice, and the Silero VAD (2 MB). Ask the user whether they want them, or would rather use hosted models: `transcribe = "cloud"` with `OPENROUTER_API_KEY`, and `voice = "system"` (or ElevenLabs with `ELEVENLABS_API_KEY`), set in step 7. If they want the local models:
   ```sh
   copilot download
   ```
   It ends with `models ready in <dir>`. If they choose hosted, skip it; the 2 MB VAD model downloads on the first `copilot listen`, and step 6's check needs `--transcribe cloud` and `copilot say --voice system`.
4. **Build the tap (macOS only).**
   ```sh
   scripts/build-tap.sh
   ```
   It ends by printing the path `.../tap/build/tap`.
5. **The step only the user can do (macOS).** Ask the user to open System Settings > Privacy & Security > Screen & System Audio Recording, and under "System Audio Recording Only" turn on the app that will run `copilot` (their terminal, or the app hosting their Claude Code session). The microphone permission is asked for by macOS the first time `copilot listen` opens the mic; tell them to click Allow. You cannot grant either one.
6. **Verify** with generated speech (macOS, which has `say`):
   ```sh
   uv run python scripts/make_sample.py /tmp/copilot-sample.wav
   COPILOT_OUT=/tmp/copilot-check copilot listen --file /tmp/copilot-sample.wav
   ```
   Expect six lines like these (the times are the clock when you run it), then the summary line:
   ```
   [1 10:04:35] call: Good morning everyone, thanks for joining the planning call.
   [2 10:04:39] call: The quarterly numbers came in a little higher than we planned.
   [3 10:04:43] call: Claude, what is the boiling point of water at sea level in Fahrenheit? And how does it change at high altitude?
   [4 10:04:50] call: Let's move the design review to Thursday afternoon, and keep the scope small.
   [5 10:04:55] call: I can send the updated budget by the end of the week.
   [6 10:04:59] call: Claude, remind me what we decided about the launch date.
   stopped: 6 lines, 18 drafts, 0 echo lines dropped
   ```
   Small wording differences are fine. Then `copilot say "Setup is done."` should speak and print `said with kokoro (...)`. On Windows, record any short WAV and use that instead of `make_sample.py`.
7. **Settings (optional).** Put the user's settings (wake name, voice, transcription) in the per-user file, since the skill runs `copilot` from whatever directory the session is in: `~/Library/Application Support/meeting-copilot/copilot.toml` on macOS, `%APPDATA%\meeting-copilot\copilot.toml` on Windows. The keys are in the README's Settings section. A `./copilot.toml` is read on top of it, and flags win.
8. **Install the skill** so it works from any project, for the agent you are (and any others the user names). From the repo root, link `skills/meeting-copilot` into that agent's skills folder:
   ```sh
   mkdir -p ~/.claude/skills && ln -s "$PWD/skills/meeting-copilot" ~/.claude/skills/meeting-copilot   # Claude Code
   mkdir -p ~/.agents/skills && ln -s "$PWD/skills/meeting-copilot" ~/.agents/skills/meeting-copilot   # Codex
   ```
   For other agents, `npx skills add smithavt14/meeting-copilot` puts it where each one looks. On Windows, copy the folder instead of linking it. If the user wants to address you by another name than "Claude", set `name` in their `copilot.toml` (step 7).

## Run it

- Start listening, in the background: `copilot listen --app "Google Chrome"` (or `zoom.us`, `"Microsoft Teams"`) on a Mac, `copilot listen --loopback` on Windows, `copilot listen` for the mic only.
- Give the user a live view: `copilot tail` in a terminal they can see. If you cannot open one, give them the exact command listen prints after `watch it live in another terminal:`.
- Read along: `copilot wake --since 0 --drafts-since 0 --mutes-since 0`, then again with the `seen=`, `drafts=` and `mutes=` values from each output.
- Speak, only when the user asks: `copilot say "<two or three sentences>"`, in the foreground, then re-arm wake.
- Mute and unmute: `copilot mute`, `copilot unmute`. When the user types "mute", "unmute" or "resume" to you, run the matching command, confirm in one line and re-arm wake. They can also say "Claude, mute" and "Claude, unmute", and the listener handles that itself.
- Stop: `copilot stop`.

The skill in `skills/meeting-copilot/SKILL.md` runs this loop; follow it when the user says "copilot this meeting".

## Working on this repo

- `src/meeting_copilot/`: `cli.py` (commands and settings), `pipeline.py` (VAD cutting, drafts, the shared transcription slot), `vad.py`, `transcribe.py` (Parakeet, OpenRouter), `sources.py` (mic, Mac tap, Windows loopback, WAV file), `echo.py` (call and said echo), `names.py` (wake name, mute commands), `run.py` (the run file, its lock, the recorder), `wake.py`, `tail.py`, `voice.py` (`copilot say`), `models.py` (downloads).
- `tap/`: the Swift process-tap binary. `scripts/build-tap.sh` builds it.
- `skills/meeting-copilot/SKILL.md`: the skill, for any agent. `.claude/skills/meeting-copilot` and `.agents/skills/meeting-copilot` are links to it, so Claude Code and Codex load it in this repo.
- Tests: `uv run pytest`. Tests use synthetic audio and mocks; one end-to-end test uses macOS `say` and the downloaded models and skips without them. Never add real recordings or transcripts as fixtures.
- Runs go to `$COPILOT_OUT`, else `--out`, else a per-user data directory (`~/Library/Application Support/meeting-copilot/out` on macOS, `%LOCALAPPDATA%\meeting-copilot\out` on Windows). Only one `listen` runs per directory. When testing next to a real run, always set `COPILOT_OUT` (or `--out`) to a scratch directory so you neither collide with it nor write into it. Models live in the user cache (`COPILOT_CACHE` overrides). The per-user settings file is `COPILOT_CONFIG` when set; tests point it at an empty temp path (`tests/conftest.py`) so they never read the real one.
- After changing anything under `tap/`, run `scripts/build-tap.sh`.
