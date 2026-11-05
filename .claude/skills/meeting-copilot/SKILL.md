---
name: meeting-copilot
description: Be a copilot in a live meeting or a spoken work session. Starts `copilot listen` on the mic and the call's audio, reads the transcript as it arrives through `copilot wake`, answers questions addressed to "Claude" in text (aloud with `copilot say` only when asked), fact-checks claims, scopes ideas with subagents, and stays quiet otherwise. Mutes and stops when asked. Fires on "copilot this meeting", "listen to this call", "start the meeting copilot", "stop the meeting".
---

# Meeting copilot

`copilot listen` writes every finished utterance of the meeting as a numbered line, labelled `me` (the microphone) or `call` (the computer's audio). This session reads those lines with `copilot wake` and writes in its own chat. Nothing joins the call, and nothing is spoken unless the user asks (section 3). If `copilot` is not on the PATH, run each command below as `uv run --project <path to the meeting-copilot repo> copilot ...`.

## 1. Start

1. Pick the source, asking one short question when it is unclear:
   - A call in a browser or app on a Mac: `copilot listen --app "Google Chrome"` (or `zoom.us`, `"Microsoft Teams"`, a bundle id).
   - A call on Windows: `copilot listen --loopback` (it hears every app the speakers play).
   - In person: `copilot listen` (mic only).
2. Run it with the Bash tool and `run_in_background: true`. Read its output for the `lines <path>` line and tell the user it is listening and where the lines go. If it says a run is already going, say so and stop. If it exits with an error, show the user the error and stop.
3. Give the user a live view they can watch. If the host lets you open a terminal the user can see (a terminal panel or tab tool), run `copilot tail` there. Otherwise give the user the exact command from listen's `watch it live in another terminal:` line to run themselves. Do this every time; the user should never be left without a view of what is being heard.
4. Remind the user in one line to tell the room an AI is taking notes. Some places require every party's consent to record.

## 2. During

1. **The loop.** Run `copilot wake --since <n> --drafts-since <d> --mutes-since <m>` with `run_in_background: true`, all starting at 0. It returns when a line says the name (`CALL`), when a draft of a line still being spoken says the name (`DRAFT`), after a batch of new lines, on a mute or unmute (`MUTE`, `UNMUTE`), when the run stops (`END`), or after a timeout (`TIMEOUT`). Its output is the new lines, then `--- seen=<n> drafts=<d>` (plus ` mutes=<m>` once there has been one) with the markers. Read the lines, act, and run it again with the new `seen`, `drafts` and `mutes`. Every wake ends by re-arming, until `END`.
   - **A `DRAFT` wake** is a question still being asked. Start on it: the search, the files, the first sentence of the answer. Post the answer once the final line arrives (usually the next wake, marked `CALL`), and answer the final's wording. A draft that names Claude but asks nothing gets no note. Quote only final lines.
2. **Silence is the default.** Post only for one of these, each as a short note headed with the line numbers it draws on (`[41]`):
   - **Call:** a `CALL` line asks Claude something. Answer it first, in a few sentences.
   - **Fact check:** a claim that the project files or a quick web search contradicts or sharpens. Check every source before calling a number wrong, and for anything that changes over time, answer from the newest dated source and give its date.
   - **Idea:** something to build or research. Say what is already known, then send a subagent in the background when scoping it takes more than a couple of reads (no external actions, a 250-word cap). Post its result while the topic is still open.
   - **Decision, deadline or risk:** one line each.
   A batch with nothing that meets this bar gets no message.
3. **Labels.** `me` is whoever is at this computer's microphone; `call` is everyone on the call. In a room, every voice the mic hears is `me`. Never name who said something unless the line itself does.
4. **Mute.** A `MUTE` wake means the listener stopped writing (the user said "Claude, mute" or ran `copilot mute`): post one line, `Muted. Say "Claude, unmute" to resume.`, and keep re-arming. On `UNMUTE`, post `Listening again.` A message the user types in this chat that is just a mute or unmute command ("mute", "stop listening", "unmute", "resume", "start listening") is the command itself: run `copilot mute` or `copilot unmute` in the foreground, confirm in one line (`Muted.` or `Listening again.`), and re-arm wake. The wake that returns `MUTE` or `UNMUTE` for that same command needs no second confirmation. For a spoken mute or unmute there is nothing to run; the listener already did it.
   - **Judge it, don't wait for the keyword.** The listener only catches a line that is the name plus a short command. When a line addressed to you asks for privacy in other words ("Claude, give me a minute, I'm talking to someone else", "Claude, don't listen to this part"), run `copilot mute` yourself, then post `Muted. Say "Claude, unmute" to resume.` A line that only talks about muting ("should the mute be smarter?") is not a request. When unsure, ask in one line instead of muting.
5. **Cost.** Each wake is a turn here. A 40-minute meeting is roughly 20 to 30 wakes.

## 3. Speaking

Answer in text by default. Speak only when the user asks on that question ("say it out loud", "answer out loud", "speak your answer") or has turned on speaking for the session ("speak your answers from now on", until they turn it off).

- The spoken answer is two or three plain sentences, no lists or markup. The full answer still goes in the chat.
- Run `copilot say "<the spoken answer>"` in the foreground (not in the background), then re-arm wake. Lines heard while it plays are dropped as echo and never wake you.
- If it prints `cannot speak`, say so in the chat once and keep answering in text.

## 4. Stop

On "stop the meeting" or similar, run `copilot stop`. It prints the lines file. Then give a short summary of the meeting, the decisions, and anything people committed to, quoting line numbers. Write nothing anywhere else unless the user asks.
