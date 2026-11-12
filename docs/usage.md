# copilot: usage reference

The details behind the README's Use section: mute, voice, transcription, speakers in a room, drafts, the output file, `wake`, and every setting.

## Mute

Say "Claude, mute" (or "Claude, stop listening"), or run `copilot mute` (in a session with the skill, typing "mute" does the same, and "unmute" or "resume" undoes it), when you turn to something the session should not read. The listener keeps listening so it can hear "Claude, unmute" (also "resume" or "start listening", or `copilot unmute`), but it writes no lines and no drafts and wakes nobody; only the mute and unmute events and the two command lines reach the file. A misheard name counts ("Claw, mute"). In cloud mode, muted audio is transcribed on this machine when Parakeet is downloaded; otherwise only utterances under 3 seconds (long enough for "Claude, unmute") go to OpenRouter.

## Voice

`copilot say "<text>"` (or text on stdin) speaks through the first engine that works:

- `elevenlabs`, when `ELEVENLABS_API_KEY` is set: the streaming text-to-speech API, model `eleven_flash_v2_5`, voice `JBFqnCBsd6RMkjVDRZzb` (settings `elevenlabs_model`, `elevenlabs_voice`). Text goes to ElevenLabs.
- `kokoro`: Kokoro-82M on the CPU through kokoro-onnx, voice `am_puck` (`kokoro_voice`, `voice_speed`). The model (about 350 MB) downloads on first use or with `copilot download`.
- `system`: `say` on macOS, System.Speech on Windows.

`voice = "kokoro"` in `copilot.toml` or `--voice kokoro` picks one (it still falls back down the list). Each `say` marks the answer in the run's file, and any `me` line or draft that repeats its words while it plays or up to 4 seconds after is dropped as echo, by the same rule as call echo. Nothing heard in that window wakes the copilot. Headphones avoid the echo entirely.

## Transcription: local or cloud

- `local` (the default): NVIDIA Parakeet TDT 0.6B v3 through sherpa-onnx, on the CPU. Nothing leaves the machine.
- `cloud`: OpenRouter's `/api/v1/audio/transcriptions`, one WAV per request. Set `OPENROUTER_API_KEY` in the environment and pass `--transcribe cloud`. The default model is `openai/whisper-large-v3-turbo`; `--cloud-model` picks another (`curl "https://openrouter.ai/api/v1/models?output_modalities=transcription"` lists them). Use it on a machine where local transcription is slow. Audio goes to OpenRouter and the model's provider.

## Speakers in a room

In person, everyone in the room reaches the same mic, so every line would read `me`. `copilot listen --diarize` (or `diarize = true`) tells the voices apart instead: each mic line's `speaker` is a voice label, `s1`, `s2`, ..., numbered in the order the voices are first heard and kept for the whole run. Call lines stay `call`, and the `stream` field on every line still says which source it came from. The labels come from a speaker-embedding model (3D-Speaker ERes2Net-base, about 40 MB, run on the CPU through sherpa-onnx), which downloads on the first `--diarize` run or with `copilot download --diarize`. Labelling adds about 50 ms to each final.

How the labels behave:

- **One label per line.** A line is one stretch of speech between pauses. When two people talk inside one line without a pause, it gets the label of whoever dominates it. Quick back-and-forth ("yeah", "right") is where most wrong labels come from.
- **A new voice shows up on its second line.** The first line from someone new carries the closest known label; their own label starts with their next line of a second or more of speech. A cough or a door slam heard once never becomes a speaker.
- **Short lines never start a voice.** A line with under a second of speech takes the closest label.
- **The end of the run corrects them.** When the run stops, every line is clustered again with the whole meeting in hand, and a `relabel` event lists the lines whose label changed (most often each person's first line). Voices with under 20 seconds of speech (or a twentieth of the run's speech, if that is less) fold into the closest one. The live labels are kept wherever they still fit, so names given during the meeting stay on the right people. `copilot transcript` prints the run with these labels applied.
- **Muted speech** is never labelled and never teaches the model a voice.

Name a voice once you know who it is, during the run or after it:

```sh
copilot name s2 Joe           # tail, wake and transcript show s2 as Joe from now on
copilot name s2               # clears it
copilot transcript            # the run's lines, end-of-run labels and names applied
```

The name goes into the run's file as a `name` event; the lines keep `s2`. `--run <dir>` points either command at an older run.

Measured on the AMI meeting corpus (real meetings recorded by a far-field mic in the middle of the table, which is close to a laptop mic, and by headsets), through `copilot listen --file --as me --diarize`, counting the share of speech whose line got its main speaker's label after the end-of-run pass: 81 and 100% in two 4-minute stretches where two people talk, 78 and 87% in two 4-minute stretches with three, and 84 to 93% over two whole 15 to 20 minute four-person meetings. Longer meetings label better, because each voice has more speech behind it. Most errors are short replies and lines where two people overlap.

## Drafts

While someone is still talking, the audio so far is re-transcribed about once a second and written as a `draft` event. When the utterance ends (500 ms of silence), one final pass replaces it; the final is the record. A draft that contains the name wakes the copilot before the question is finished. Only one transcription runs at a time across both streams, and a final never waits behind more than one draft, so a slow machine makes fewer drafts and the finals stay on time. In cloud mode each draft is a request; `--draft-ms 0` turns drafts off.

## Output

Each run writes `<out>/<YYYY-MM-DD_HHMMSS>/lines.jsonl`, where `<out>` is `--out`, `COPILOT_OUT`, or a per-user data directory (`~/Library/Application Support/meeting-copilot/out` on macOS, `%LOCALAPPDATA%\meeting-copilot\out` on Windows). One JSON object per line:

```json
{"ev": "line", "n": 12, "speaker": "call", "stream": "call", "text": "Claude, what did we decide about the launch date?", "start": "2026-11-05T14:03:21.412-05:00", "end": "2026-11-05T14:03:24.020-05:00", "utt": 7, "named": true, "latency_ms": 655, "drafts": 2}
{"ev": "line", "n": 13, "speaker": "s2", "stream": "me", "text": "Thursday, if the copy is ready.", "start": "2026-11-05T14:03:25.010-05:00", "end": "2026-11-05T14:03:26.400-05:00", "utt": 8, "named": false, "latency_ms": 540, "drafts": 1}
{"ev": "draft", "d": 31, "speaker": "me", "stream": "me", "utt": 9, "text": "so the next step", "start": "2026-11-05T14:03:26.100-05:00", "named": false}
```

`stream` is the audio source (`me` or `call`). `speaker` is the same, except on a `--diarize` run, where mic lines carry their voice label (`s2` above). A draft's `speaker` is always its stream; the final with the same `stream` and `utt` replaces it.

There are also `start`, `echo` (a dropped speaker-echo line), `said` (what `copilot say` played, and when), `mute` and `unmute`, `empty` (an utterance with no words), `name` (from `copilot name`), `relabel` (a diarized run's end-of-run labels, by line number), `error` and `stop` events.

## wake

```sh
copilot wake --since 0 --drafts-since 0
```

Blocks until one of these, then prints the new lines and `--- seen=<n> drafts=<d>` with markers:

- a final line says the name (`CALL`);
- a draft says the name: the first draft of an utterance with no final yet (`DRAFT`);
- 8 new lines (`--lines`);
- a mute or unmute (`MUTE`, `UNMUTE`; pass `mutes=` back as `--mutes-since`);
- the run stops, or its listener process is gone (`END`);
- 10 minutes pass (`TIMEOUT`, `--max-wait` in seconds).

Pass `seen` and `drafts` back as `--since` and `--drafts-since` next time. Started a moment before `listen`, it waits up to 15 seconds for the run to appear.

## Settings

Settings come from up to two `copilot.toml` files. The per-user one applies wherever `copilot` runs, which is what you want when the skill starts `listen` and `say` from any project:

- macOS: `~/Library/Application Support/meeting-copilot/copilot.toml`
- Windows: `%APPDATA%\meeting-copilot\copilot.toml`
- elsewhere: `~/.config/meeting-copilot/copilot.toml`
- or the path in `COPILOT_CONFIG`

A `copilot.toml` in the current directory (or `--config <file>`) is read on top of it, and flags win over both. Either file can set any of:

```toml
name = "Claude"
variants = ["cloud", "clod", "claud", "clawed", "claw", "clog", "claudia"]  # mishearings that count anywhere
opening_variants = ["call"]   # count only when they open the line
transcribe = "local"          # or "cloud"
diarize = false               # true: label the mic's voices s1, s2, ... (see Speakers in a room)
cloud_model = "openai/whisper-large-v3-turbo"
language = "en"               # sent to the cloud model; "" to auto-detect
draft_ms = 1000
silence_ms = 500
pre_roll_ms = 300
vad_threshold = 0.5
wake_lines = 8
max_wait_s = 600
voice = "auto"                # or "elevenlabs", "kokoro", "system"
kokoro_voice = "am_puck"
voice_speed = 1.0
elevenlabs_voice = "JBFqnCBsd6RMkjVDRZzb"
elevenlabs_model = "eleven_flash_v2_5"
```
