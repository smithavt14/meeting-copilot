---
name: meeting-copilot
description: Be a silent copilot in a live meeting. Starts `copilot listen` on the mic and the call's audio, reads the transcript as it arrives through `copilot wake`, answers questions addressed to "Claude" in text, fact-checks claims, scopes ideas with subagents, and stays quiet otherwise. Stops the run when asked. Fires on "copilot this meeting", "listen to this call", "start the meeting copilot", "stop the meeting".
---

# Meeting copilot

`copilot listen` writes every finished utterance of the meeting as a numbered line, labelled `me` (the microphone) or `call` (the computer's audio). This session reads those lines with `copilot wake` and writes in its own chat. Nothing is spoken and nothing joins the call. If `copilot` is not on the PATH, run each command below as `uv run --project <path to the meeting-copilot repo> copilot ...`.

## 1. Start

1. Pick the source, asking one short question when it is unclear:
   - A call in a browser or app on a Mac: `copilot listen --app "Google Chrome"` (or `zoom.us`, `"Microsoft Teams"`, a bundle id).
   - A call on Windows: `copilot listen --loopback` (it hears every app the speakers play).
   - In person: `copilot listen` (mic only).
2. Run it with the Bash tool and `run_in_background: true`. Read its output for the `lines <path>` line and tell the user it is listening and where the lines go. If it says a run is already going, say so and stop.
3. Remind the user in one line to tell the room an AI is taking notes. Some places require every party's consent to record.

## 2. During

1. **The loop.** Run `copilot wake --since <n> --drafts-since <d>` with `run_in_background: true`, both starting at 0. It returns when a line says the name (`CALL`), when a draft of a line still being spoken says the name (`DRAFT`), after a batch of new lines, when the run stops (`END`), or after a timeout (`TIMEOUT`). Its output is the new lines, then `--- seen=<n> drafts=<d>` with the markers. Read the lines, act, and run it again with the new `seen` and `drafts`. Every wake ends by re-arming, until `END`.
   - **A `DRAFT` wake** is a question still being asked. Start on it: the search, the files, the first sentence of the answer. Post the answer once the final line arrives (usually the next wake, marked `CALL`), and answer the final's wording. A draft that names Claude but asks nothing gets no note. Quote only final lines.
2. **Silence is the default.** Post only for one of these, each as a short note headed with the line numbers it draws on (`[41]`):
   - **Call:** a `CALL` line asks Claude something. Answer it first, in a few sentences.
   - **Fact check:** a claim that the project files or a quick web search contradicts or sharpens. Check every source before calling a number wrong, and for anything that changes over time, answer from the newest dated source and give its date.
   - **Idea:** something to build or research. Say what is already known, then send a subagent in the background when scoping it takes more than a couple of reads (no external actions, a 250-word cap). Post its result while the topic is still open.
   - **Decision, deadline or risk:** one line each.
   A batch with nothing that meets this bar gets no message.
3. **Labels.** `me` is whoever is at this computer's microphone; `call` is everyone on the call. In a room, every voice the mic hears is `me`. Never name who said something unless the line itself does.
4. **Cost.** Each wake is a turn here. A 40-minute meeting is roughly 20 to 30 wakes.

## 3. Stop

On "stop the meeting" or similar, run `copilot stop`. It prints the lines file. Then give a short summary of the meeting, the decisions, and anything people committed to, quoting line numbers. Write nothing anywhere else unless the user asks.
