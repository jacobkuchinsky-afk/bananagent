# BananaGent

Hold the <kbd>`</kbd> key (the tilde key, left of <kbd>1</kbd>), talk, let go. A yellow
water orb bends itself into being in the corner of the screen and pulses to your voice;
the model's reply appears beside it.

The recording goes to `gemma4:e4b` **as audio** — there is no speech-to-text step. That
model is natively multimodal, so skipping transcription cuts a whole stage of latency
(~1.6s per turn instead of ~24s).

## Running it

It starts automatically at login. To run it by hand:

```bash
run.bat
```

Quit with <kbd>ctrl</kbd>+<kbd>shift</kbd>+<kbd>q</kbd> — closing the dashboard only minimizes
it, and the orb has no frame to click. <kbd>Esc</kbd> dismisses the answer without quitting.

### Start at login

Already installed, as a shortcut in your Startup folder. To remove or reinstall:

```bash
powershell -ExecutionPolicy Bypass -File install_startup.ps1 -Remove
```

```bash
powershell -ExecutionPolicy Bypass -File install_startup.ps1
```

At login it runs under `pythonw.exe` so no console window flashes up; anything it would
have printed goes to `bananagent.log` in this folder. Note it preloads the model at
launch (~9.6 GB resident) so the first question isn't a ~50s wait — set
`WARM_ON_START = False` in `config.py` if you'd rather not give that up at login.

## The orb

There's no panel. The window is fully transparent, so only the orb and the answer text
are painted, and the text carries a black outline instead of a backdrop so it stays
readable over anything. The orb sits at a fixed spot and answers grow upward, so it never
jumps around as text streams in.

Waking up plays a 0.75s formation: a sheared, curled swirl with droplets dragged in from
outside, unwinding into a still sphere. The shape is one closed polygon whose points have
their *angle* twisted by how far out they sit — that radius-dependent shear is what makes
it look bent rather than merely wobbly. It keeps breathing afterward so it reads as
liquid, churning faster while the model is thinking.

## Speaking

Replies are spoken aloud with [Kokoro](https://github.com/thewh1teagle/kokoro-onnx),
locally, on the CPU — no network, no API key, and no torch: it runs on ONNX
runtime with espeak-ng bundled in.

Synthesis manages roughly **3x real time**, so waiting for a complete answer
before saying anything would put a second or two of silence in front of every
reply. Instead the text is cut at sentence boundaries the moment each one is
finished, and three stages run at once — tokens arrive and are split, a synth
thread turns sentences into audio, a playback thread drains them into one
long-lived output stream. Speech starts about a tenth of a second after the
first sentence lands, and the rest arrives behind it without gaps.

Sentence splitting guards against digits, so *"version 0.20.7 is running"* is
one sentence rather than three. A long clause with no punctuation in it gets
broken at a word after 220 characters, so speech can't fall behind the text.

Holding the hotkey again **cuts the current answer off mid-word**. Everything is
keyed on a generation number: bumping it makes both threads drop what they were
carrying, and the audio already handed to the sound card is aborted rather than
played out.

### Voices

There are 54, named like `af_heart` or `bm_george` — first letter the accent
(`a` American, `b` British, `e` Spanish, `f` French, `h` Hindi, `i` Italian,
`j` Japanese, `p` Portuguese, `z` Chinese), second the speaker's gender. Hear
them:

```bash
.venv\Scripts\python.exe -m voice af_heart bm_george am_michael
```

Set the default in `config.py` (`VOICE`, `VOICE_SPEED`, `SPEAK`), or just ask —
*"use a British voice"* makes the model call `list_voices` and then `set_voice`.
*"be quiet"* calls `stop_speaking`, and *"stop talking"* turns it off entirely.

### The weights

Not committed — they're 196 MB and not ours. Fetch them into `models/`:

```bash
curl -L -o models/kokoro-v1.0.fp16.onnx https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.fp16.onnx
```

```bash
curl -L -o models/voices-v1.0.bin https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
```

fp16 is half the size of the full-precision file with no audible difference;
`kokoro-v1.0.int8.onnx` is smaller again but you can hear the quantization.
If the files aren't there the app says so once in the log and runs silently —
nothing else notices.

## The dashboard

A second window, in the same process, showing what the model and the assistant
are actually doing. It starts **minimized** — logging in gives you a banana on the
taskbar and nothing else. Click it to look; closing it minimizes again rather than
quitting, and <kbd>Esc</kbd> puts it away. Because it lives inside the assistant, it
autostarts with it and there is nothing separate to install.

The side nav has one page, Diagnostics, which reports the model (parameters,
quantization, context window, size on disk), how much of it the graphics card is
holding versus what spilled to the CPU, which server is answering and when the
weights get unloaded, the microphone and hotkey, and this session's reply times.
Numbers come from a polling thread — never the UI thread, since reading them
touches the network — and are eased toward, so gauges sweep instead of snapping.
Nothing repaints at all while the window is minimized.

Say *"open the dashboard"* and the model calls `open_dashboard`. Everything the
window does by hand has an endpoint; `dashboard_diagnostics` hands back the same
snapshot the window paints, so what you read and what it says out loud can't
disagree.

### How it's drawn

Tk's canvas has no antialiasing — every curve on it is visibly stair-stepped —
so nothing shaped is drawn with it. `paint.py` rasterizes each shape from a
signed distance field in numpy and hands Tk a PNG, which 8.6 decodes natively
with alpha. That's what gives the exact round corners at any radius, outlines
centred on the edge rather than inside it, and gauge arcs with clean round caps.
A gauge-sized image costs about 0.4ms, so re-rasterizing one every frame is
cheaper than it sounds. Only text uses the canvas directly, since Tk draws that
well.

The banana is drawn the same way, by `icon.py` — a circular arc for the spine and
a thickness that swells in the middle, with the body as the union of discs along
it. One routine serves both the window icon and the logo in the nav, so they
can't drift apart. `banana.ico` is generated on first run rather than committed,
because it is entirely derived from that code; delete it and it comes back.

## Agents

An agent is a different shape from an endpoint. An endpoint returns in
milliseconds; an agent goes away for minutes, so it has to be watchable and
interruptible while it runs. Every one carries a status, the task in the words
you gave it, an activity log, and an approval gate — which is exactly what the
dashboard's **Agents** tab renders. Click an agent to roll open every endpoint
that drives it.

Adding one is the same trick as endpoints: drop a module in `agents/`,
subclass `Agent`, implement `_run(task)`, call `register()`. Threading,
cancellation, logging and approvals are already handled. Tag its endpoints
`agent="..."` and they appear on its dropdown with no other wiring.

### The browsing agent

Drives **a real Chrome window** — the one you already have — through
[Stagehand](https://github.com/browserbase/stagehand), with **`qwen3.8:27b`
running locally** deciding each step. Stagehand takes a plain async callback in
place of a hosted provider, so there is no API key and nothing leaves the
machine; `agents/llm.py` is that callback. Structured steps go to Ollama with
`format` set to Stagehand's JSON schema, which constrains decoding rather than
hoping for valid JSON.

It is not a research agent. It is pointed at things that *change* something, so
the design question isn't how to read pages but what it may do unattended:

- it navigates, reads and types into fields on its own;
- it **stops and asks** before submitting a form, creating an account, spending
  money, agreeing to terms, sending something public, or deleting anything;
- it **never types a password or card number** — it opens the page, hands you
  the window, and waits, which works because the browser is real and visible;
- it **never touches a CAPTCHA**. Those exist to establish a person is present,
  and there is one right here.

Two things decide whether a step is gated, and either is enough. The model
labels its own next step, *and* a keyword pass reads the instruction
independently. A model that talks itself into "this Sign up button is harmless"
still gets stopped by the second check — which is the case actually worth
testing, because it's the one that happens. Approve by clicking the button on
the row, or by saying so. With nobody there it refuses rather than proceeds.

The agent keeps its own Chrome profile in `browser-profile/`, so a site you log
into once stays logged in for later tasks. That directory holds your cookies and
is gitignored.

```bash
ollama pull qwen3.8:27b
```

18 GB, and it takes image input, so the model sees a picture of the page as well
as its text. Until it's pulled the agent falls back to the closest **local**
model and says so — never to a `-cloud` tag, since the whole point is that the
pages it reads stay on this machine.

Ask it out loud (*"open the council website and start a bulky waste
collection"*), or call `browser_do`. It returns immediately and works in the
background; `browser_status` says how it's going, `browser_stop` calls it off,
and `browser_answer` is how *"yes, go ahead"* reaches the gate.

### Why there are only four of them

The agent started with eight endpoints and a `list_agents` alongside them. That
took the assistant's own tool choice from 5/5 to **1/5 on questions that have
nothing to do with browsing** — and not by picking the wrong tool. Past a
certain amount of tool text, gemma stops emitting exact names and starts
inventing plausible ones: `read_clipboard`, `system_info`, `get_time()`,
`dashboard_api:open_dashboard`.

| tools in the list | correct |
|---|---|
| 38, before the agent | 5/5 |
| 47, eight browser endpoints | 1/5 |
| 42, four browser endpoints | **14/14** |

So approve and deny are one endpoint with a choice, status returns the activity
log and the pending question rather than those being separate calls, and
`list_agents` went away — the dashboard already tracks agents, which is what it
is for. It costs nothing you can measure and buys back the whole tool list.

One other thing fell out of it: `open_url` used to win *"open the council site
and start a bulky waste collection"*, because "opens a web address" describes
that perfectly. It now says what it **can't** do — no reading, no filling in, no
clicking — and points at the agent instead. Same measurement, one more correct.

## Tuning what it says

The prompt lives in [`prompt.md`](prompt.md) — plain markdown, edit it freely. Changes
apply to your **next question**; there's no restart and no reload command. Notes inside
`<!-- -->` are for you and never reach the model, and section headings are structure only.

Delete the file and the app falls back to the built-in prompt in `config.py`, so a bad
edit degrades instead of breaking. `.venv\Scripts\python.exe -m prompts` prints exactly
what the model is being sent.

Three parts of it are load-bearing — change them knowing what they do:

- the *"no clock, no sensors"* paragraph is what stops it inventing the time and battery
  level instead of calling a tool;
- *"never say you have done something unless a tool returned success"* is what stops it
  claiming it changed the volume when it didn't;
- **don't paste a tool list into it** — see the measurements below.

## Endpoints

The model is the front line: it hears you, decides whether a capability applies, calls it,
sees the result, and then answers. Calls are real Ollama tool calls, which work with audio
input — you can say *"set the volume to thirty percent"* and it invokes
`set_volume(percent=30)` without any transcript in between.

Every endpoint is one decorated function. That single declaration produces all three
things the system needs — the JSON schema the model calls, the catalog entry, and the
dispatch target:

```python
@endpoint(
    tier=MAJOR,
    description="Sets this computer's master output volume to an exact percentage, immediately.",
    params={"percent": param("integer", "Target volume, 0 to 100.", required=True)},
)
def set_volume(percent):
    ...
```

**Tiers.** `MAJOR` performs an action and changes something — the app, the machine, or the
outside world (`set_volume`, `open_url`, `app_quit`; an email sender would go here).
`MINOR` only reads and reports, so calling it is free (`get_time`, `system_stats`,
`app_status`). The tier is stamped onto the front of each description the model reads, and
the tool list is ordered major-first.

**Adding one:** drop a module in `endpoints/` and decorate a function. The package imports
everything beside it at startup, so it appears in the model's tool list with no
registration step. `endpoints.bind(app)` gives handlers a handle on the running app;
`on_ui(fn)` hops to the Tk thread for anything touching the overlay.

Run `.venv\Scripts\python.exe -m endpoints` to print the full catalog.

### Why the catalog isn't in the system prompt

Listing the endpoints as prose in the system prompt is the obvious design, and it measures
badly — the model reads the list and answers *out of it* instead of calling anything:

| | correct tool chosen |
|---|---|
| 25 tools, catalog in prompt | 11/18 |
| 25 tools, no catalog | **17/18** |
| 5 tools, catalog in prompt | 11/18 |
| 5 tools, no catalog | **18/18** |

The number of tools barely matters; the duplicated prose is what does the damage. With the
catalog in the prompt, *"set the volume to 25 percent"* called nothing and the model
replied "I've set your volume to 25 percent" — claiming an action it never took. So the
major/minor split rides on each tool's own description instead, which costs nothing.
`INLINE_CATALOG = True` in `config.py` turns the prose block back on if you want to judge
it yourself.

## Layout

| File | What it does |
|---|---|
| `main.py` | Wires the hotkey, recorder, model, overlay and dashboard together |
| `overlay.py` | The transparent always-on-top window and text layout |
| `water.py` | The orb: formation animation, shape math, palettes |
| `recorder.py` | Push-to-talk mic capture, resampling, WAV encoding |
| `brain.py` | Streams audio to Ollama, runs the tool-call loop |
| `voice.py` | Speaks the reply with Kokoro as it streams; interruption |
| `dashboard.py` | The dashboard window: nav, cards, gauges, animation |
| `diagnostics.py` | One reading of the model, server, machine and session |
| `paint.py` | Antialiased shapes from distance fields, as PNGs for Tk |
| `icon.py` | Draws the banana, and writes `banana.ico` |
| `agents/registry.py` | Agent base class: status, activity, the approval gate |
| `agents/browser.py` | The browsing agent: Chrome, the step loop, what's gated |
| `agents/llm.py` | Stagehand's LLM callback, wired to local Ollama |
| `endpoints/registry.py` | The decorator, schemas, catalog, dispatch |
| `endpoints/app_control.py` | Endpoints for the assistant itself |
| `endpoints/dashboard.py` | Endpoints for the dashboard window |
| `endpoints/speech.py` | Endpoints for the voice (which one, how fast, hush) |
| `endpoints/browser_agent.py` | Endpoints for the browsing agent |
| `endpoints/system.py` | Endpoints for the computer (volume, clipboard, power) |
| `config.py` | Every knob worth turning |

## Configuration

All in `config.py`. The ones you're most likely to touch:

- `HOTKEY` / `SUPPRESS_HOTKEY` — which key, and whether it's swallowed. Suppression is on
  so holding it doesn't type ``` ``` ``` into whatever has focus. Turn it off if that gets
  in the way of writing code.
- `INPUT_DEVICE_NAME` — matched as a case-insensitive substring against the device list,
  so it survives index reshuffling when you plug things in. Run
  `.venv\Scripts\python.exe recorder.py` to list devices.
- `ORB_RADIUS`, `CORNER`, `WIDTH`, `MAX_HEIGHT` — size and placement.
- `SILENCE_PEAK` — below this the clip is treated as "you didn't say anything".

Orb colors live in `water.py`'s `PALETTES`, keyed by state.

## Why there are two Ollama servers

Ollama 0.30+ can't decode **speech** for `gemma4:e4b` — it hears that you spoke but not
what you said. On 0.32.9 the same clip that transcribes perfectly on 0.20.7 came back as
*"rhythmic sound resembling chanting"*, and feeding it pure silence produced a
hallucinated *"lo lo lo"*. A 440 Hz tone was described correctly throughout, which is how
we know the WAV encoding and API format were never the problem. Tracked upstream at
[ollama#16584](https://github.com/ollama/ollama/issues/16584).

So this project pins its own copy:

| Port | Version | Who uses it |
|---|---|---|
| 11434 | whatever you have installed (0.32.9) | everything else, untouched |
| 11435 | 0.20.7, at `C:\Users\jacob\ollama-0.20.7` | BananaGent only |

`main.py` starts the pinned server on demand if port 11435 isn't answering, so you don't
have to think about it. Both read the same model store in `~/.ollama/models` — nothing is
duplicated on disk, and your tray icon, autostart, and other models are all unchanged.

To undo it: set `OLLAMA_HOST` back to `http://127.0.0.1:11434` in `config.py` and delete
`C:\Users\jacob\ollama-0.20.7`. Speech recognition stops working until upstream fixes it,
but nothing else notices.

### When it stops answering

Gemma 4's audio runner wedges every few requests ([ollama#15333](https://github.com/ollama/ollama/issues/15333)) —
the request simply never returns. `brain.py` handles this: if a request times out before
any text has arrived, it bounces the pinned server and retries once, which clears it.
Once tokens have started streaming it lets the error through instead of replaying half an
answer. Only processes under `C:\Users\jacob\ollama-0.20.7` are ever killed, matched on
executable path, so your own Ollama is never touched.

If you want to see why it wedged, the pinned server's own output is in
`ollama-pinned.log` next to the app, and the app's is in `bananagent.log`.

## Notes

- Needs 64-bit Python. The system-wide `python` here is 32-bit, which is why the project
  ships its own venv — see `requirements.txt`.
- Audio is sent in the message's `images` array. That field is generic media despite the
  name; a dedicated `audio` field is accepted by the API and silently ignored.
- There's no conversation history — every utterance is its own question. Since the model
  hears audio directly there's no transcript to carry, so past turns could only be replayed
  as a placeholder, which told the model it had said things without any record of what.
