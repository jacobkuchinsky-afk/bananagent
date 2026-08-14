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

There's no window to close, so quit with <kbd>ctrl</kbd>+<kbd>shift</kbd>+<kbd>q</kbd>.
<kbd>Esc</kbd> dismisses the answer without quitting.

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
| `main.py` | Wires the hotkey, recorder, model, and overlay together |
| `overlay.py` | The transparent always-on-top window and text layout |
| `water.py` | The orb: formation animation, shape math, palettes |
| `recorder.py` | Push-to-talk mic capture, resampling, WAV encoding |
| `brain.py` | Streams audio to Ollama, runs the tool-call loop |
| `endpoints/registry.py` | The decorator, schemas, catalog, dispatch |
| `endpoints/app_control.py` | Endpoints for the assistant itself |
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
