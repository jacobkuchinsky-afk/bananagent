# BananaGent

Press the <kbd>`</kbd> key (the tilde key, left of <kbd>1</kbd>). A yellow water orb bends
itself into being in the corner of the screen with a text box beside it, and the box
takes the keyboard. Type, press <kbd>Enter</kbd>, and the reply appears above the box.
<kbd>Esc</kbd> or the key again puts it away.

The dashboard's **Chat** tab is the other way in. Both go through `App.send()` to
the same model, and they share one conversation: a message typed in the corner shows
up in the chat with its reply, and the model sees both as one history.

Replies come from **`qwen3.8:27b` on the DGX Spark**, streamed over Tailscale — see
*The model* below.

## The model

`brain.py` talks to Ollama on the Spark (`http://100.82.77.31:11434`, set in
`config.py`). This machine has to be on the tailnet. Ollama is stateless, so every
request is rebuilt: the system prompt from `prompts/` with long-term memory and the
business record appended, the short-term window of recent conversation, then the new
message (see *Memory* and *The business*).

`NUM_CTX` is 40,960 so all of that fits with room for tool results and the answer —
about 8k tokens of prompt (most of it the tool list), 6k at most of long-term memory,
3k for the business brief plus 1–2k for its tasks, leads and log, and 20k of
conversation. Today that's ~10k before the conversation; with memory and the brief both
full it's ~19k, which leaves little for tool results, so trim them or raise `NUM_CTX`
before that happens. A full window was tested live: a codeword from the oldest of 42
messages came back correctly.

`NUM_CTX` is always sent. The Spark runs 16 parallel slots, and without it Ollama
reserves a huge default context for each and fails with *requires more system memory*
— if that ever appears, lower `NUM_CTX` first. `KEEP_ALIVE = -1` keeps the model
loaded, and the app loads it at startup (`WARM_ON_START`), so only the first message
after someone else's model evicts it pays the ~30s load. Thinking is off: it's the
fastest first token, and with it on the reasoning would be discarded anyway.

**Tools** don't use Ollama's tools API. The model writes a line on its own —

```
[[call get_time {}]]
```

— and `brain.py` runs the first one, sends back `Tool result for get_time: {json}` as a
new turn, and goes again until a reply has no call in it (at most `MAX_TOOL_ROUNDS`).
Bad JSON and unknown tools go back to the model as `{"ok": false, ...}` so it can fix the
call; nothing raises. The call line is held back from the screen as soon as it could be
one, and the stream is closed the moment it's complete.

Now and then the model skips the call and writes a *made-up* result instead — a
confident, wrong time. So text that could begin `Tool result for` is held back too, and
if the model writes one, the stream is cut there, none of it is shown, and it's told to
make the call for real. Measured live: 8/8 time and date questions answered from a real
`get_time` call after that change.

It can also skip the call and just *say* it made one — *"Alright, calling get_time
again."*, *"Let me check…"* and then nothing, or a time with no `get_time` behind it. So
a reply that ran no tool is checked before it's finished: if it names a tool, states a
clock time, or ends on a promise to go and do something, it's sent back (at most twice)
with a note that nothing ran, and the model either makes the call or corrects itself.
This matters more than it looks: an uncaught one is saved to memory, the model copies
its own example on the next message, and within a few turns it stops calling tools at
all. The guard can't catch every invented fact — *"the volume is at 100%"* reads like
any other sentence — which is why keeping those examples out of memory in the first
place (see *Memory*) is the real fix.

```bash
.venv\Scripts\python.exe brain.py what time is it
```

## Running it

It starts automatically at login. To run it by hand:

```bash
run.bat
```

Quit with <kbd>ctrl</kbd>+<kbd>shift</kbd>+<kbd>q</kbd> — closing the dashboard only minimizes
it, and the orb has no frame to click.

### Start at login

Already installed, as a shortcut in your Startup folder. To remove or reinstall:

```bash
powershell -ExecutionPolicy Bypass -File install_startup.ps1 -Remove
```

```bash
powershell -ExecutionPolicy Bypass -File install_startup.ps1
```

At login it runs under `pythonw.exe` so no console window flashes up; anything it would
have printed goes to `bananagent.log` in this folder.

## The orb

There's no panel. The window is fully transparent, so only the orb, the text box and
the answer are painted, and the text carries a black outline instead of a backdrop so it
stays readable over anything. The orb and the box share the bottom row and answers grow
upward, so neither jumps around as text streams in.

The app runs in the background, and Windows won't give the keyboard to a background
process just because it asks. When the box opens it briefly attaches to the foreground
window's input thread to take focus, and hands focus back to that window when the box
closes.

Waking up plays a 0.75s formation: a sheared, curled swirl with droplets dragged in from
outside, unwinding into a still sphere. The shape is one closed polygon whose points have
their *angle* twisted by how far out they sit — that radius-dependent shear is what makes
it look bent rather than merely wobbly. It keeps breathing afterward so it reads as
liquid, churning faster while a reply is being worked out.

## The dashboard

A second window, in the same process, showing what the assistant and the browsing
agent's model are doing. It starts **minimized** — logging in gives you a banana on the
taskbar and nothing else. Click it to look; closing it minimizes again rather than
quitting, and <kbd>Esc</kbd> puts it away. Because it lives inside the assistant, it
autostarts with it and there is nothing separate to install.

It opens on **Chat**: the conversation as bubbles (yours in yellow on the right,
replies in white on the left) with a text box under it — the same panel as the
overlay's, drawn by the same `text_box()` — that has the keyboard whenever the tab is
showing. Enter or the key-cap sends. Newest is at the bottom and the wheel scrolls
back; Tk's canvas can't clip, so flat page-coloured masks hide bubbles as they pass
under the title or the box.

When the model uses a tool, the reply shows it where it happened, as a grey line —
*Running get_time…* while it runs, then *Ran get_time* (or *get_time failed*). Click
the line to open the arguments the model passed; click again to close it. They're kept
with the conversation, so they're still there after a restart. The corner overlay
shows the same thing beside the orb — *ran get_volume, get_time* — for the rest of that
answer; most tools finish in milliseconds, too fast for "running" ever to be seen.

Replies are **rich text**. `markup.py` parses them — markdown (bold, italic,
`code`, headings, lists, quotes, tables, fenced code), TeX math (`$x^2$`,
`$$\frac{a}{b}$$`), and things to click — and `richtext.py` lays them out on the
canvas, one block at a time so a streaming reply only re-renders its last block.

| Written as | Shows as | Click does |
|---|---|---|
| `[notes](C:\path\notes.txt)`, a bare `C:\…` path | underlined link | opens it; right-click shows it in Explorer |
| `[docs](https://…)`, a bare `https://…` | underlined link | opens the browser |
| `[Open it](open:C:\path)` | yellow button | opens the file, folder or page |
| `[Count](run:powershell (ls).Count)` — or `run:python`, `run:cmd` | yellow button | runs it; output drops in under the paragraph |
| a ` ```python ` / `powershell` / `cmd` block | code panel with Run and Copy | runs the block; output under it |
| `[Yes](send:Yes, do it)` | yellow button | sends that as your next message |
| `[Copy](copy:text)` | yellow button | puts the text on the clipboard |

Hovering any of them says exactly what it will open or run first. Code only runs
on a click — in your home folder, with a 120s limit — and the model is told a
run button is an offer, never something it did. The overlay can't draw any of
this, so it shows `markup.plain()` of the reply: buttons as `[labels]`, math as
`x² + 1/2`. The prompt's `{{places}}` gives the model this machine's real
folder paths, so its links point somewhere.

The Diagnostics page reports the model answering messages on the Spark
(parameters, quantization, context window, size on disk), how much of it the graphics
card is holding versus what spilled to the CPU, whether the server is answering and when
the weights get unloaded, the text box, and this session's reply times.
Numbers come from a polling thread — never the UI thread, since reading them
touches the network — and are eased toward, so gauges sweep instead of snapping.
Nothing repaints at all while the window is minimized.

The **Prompts** page edits every prompt file a model is sent, each on its own tab:
the three parts in `prompts/`, long-term memory (`memory/MEMORY.md`), each subagent's
`subagents/<kind>.md`, and the browsing agent's `agents/browser.md`. Unsaved edits are
kept per tab (a dot marks them) and <kbd>ctrl</kbd>+<kbd>s</kbd> or **Save** writes them,
through the same code the prompt and memory endpoints use — same size caps, and the old
version is copied to a `history/` folder first. Saves apply from the next message.
`<!-- notes -->` show greyed out, since the model never sees them. If the model edits a
file while it's open, the editor follows along unless you have unsaved edits in it.

Everything the window does by hand has an endpoint; `dashboard_diagnostics` hands back
the same snapshot the window paints, so what you read and what a model reports can't
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

**Agents report back on their own.** When one finishes, fails, or stops to
ask you something, it announces it (`agents.announce`), and the app sends the
model a turn of its own — *"(Automatic notice, not from the user) Coding Agent
finished."* with the agent's report. The model answers you unprompted, in the
corner box and the chat, like any reply: *"Your converter's done — it's in
sandbox\zz-temp-converter, run it with python temp.py 100c; it ran the 13
tests and they pass."* Without this a finished agent just sat there until you
thought to ask. A notice waits for a reply that's still streaming rather than
cutting it off, and won't take over the corner box while you're typing in it.
In the chat it's a small labelled line, not one of your bubbles. A stop you
asked for isn't announced — you already know. What the model is told comes
from each agent's `report()`; the coding agent's includes Claude's final
RECAP and what to open.

### The browsing agent

Drives **a real Chrome window** (or Edge, if Chrome isn't installed) through
[Stagehand](https://github.com/browserbase/stagehand), with **the main
assistant's model on the Spark** deciding each step — same `MODEL`, same
`OLLAMA_HOST`, just the browsing prompt (`agents/browser.md`) instead of the
assistant's. Stagehand takes a plain async callback in place of a hosted
provider, so there is no API key and nothing leaves your own machines;
`agents/llm.py` is that callback. Structured steps go to Ollama with
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
the row, or through `browser_answer`. With nobody there it refuses rather than proceeds.

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

Start it with `browser_do` (*"open the council website and start a bulky waste
collection"*). It returns immediately and works in the
background; `browser_status` says how it's going, `browser_stop` calls it off,
and `browser_answer` is how *"yes, go ahead"* reaches the gate.

### The coding agent

BananaGent's model is a good manager and a middling programmer; Claude Code is
the reverse. So when you ask for code, the model doesn't write it. It starts
the **coding agent**, which runs a loop in the background:

```
look at the project → plan → brief Claude → read Claude's recap → check the work
(read files, run it, run the tests) → tell Claude what's wrong or what's next → …
→ coding_finish with a report for you
```

It goes round until it judges the job done *and checked* — not when Claude says
so — or until it needs you. Everything happens on this computer: Claude Code is
the local `claude.exe`, run headless (`claude -p --output-format stream-json`)
in a project folder under `sandbox/`. The only thing that leaves the machine
is the agent's own thinking, which is the same `qwen3.8:27b` on the Spark as
the rest of BananaGent, and Claude's own calls to Anthropic.

```
sandbox/
    snake-game/            one folder per project — where Claude works
    .banana/
        snake-game.json    Claude session ids, cost, approved permissions
        logs/snake-game/main.md   every exchange with Claude, in full
```

**Claude Code** isn't on PATH here. `claude_code.find_exe()` finds the desktop
app's copy, under `LocalAppData\Packages\Claude_*\…\claude-code\<version>` — the
app is an MSIX package, so the `%APPDATA%\Claude` path only exists inside it —
and takes the newest version, so app updates don't break it. It has to be
signed in once, separately from the desktop app:

```bash
.venv\Scripts\python.exe claude_code.py login
```

```bash
.venv\Scripts\python.exe claude_code.py
```

The second prints which `claude.exe` it found and whether it's signed in.

**Sessions.** Each message to Claude resumes its session (`--resume <id>`), so
Claude remembers everything it's read, written and been told. Ids are saved per
project, so asking for changes to `snake-game` next week carries on the same
conversation. The agent can open more than one — a fresh `reviewer` session for
a second opinion, or two sessions working in parallel.

**Recaps.** Claude is told (`--append-system-prompt`) that a program is driving
it, nobody can answer mid-turn, and to end every reply with a RECAP: *Done,
Files, Run it, Verified, Not done / problems, Questions*. Alongside the reply the
agent gets the files that **actually** changed on disk (a before/after diff of
the folder) and anything Claude was refused permission to do, so a recap that
claims more than happened is visible.

**Prompting Claude well** is most of `agents/coder.md`: a complete first brief
(goal, the user's words quoted, requirements, what "done" means and how it'll
be checked), short follow-ups after, bug-report-style feedback with exact
errors, requirements rather than implementations, no invented scope, a
reviewer session for anything that matters. Edit it on the dashboard's
Prompts tab (*Coding agent*); it applies from the next task.

**Permissions.** Headless Claude can't ask, so anything not allowed is refused
and reported. `CLAUDE_PERMISSION_MODE = "acceptEdits"` lets it change files in
the project; `CLAUDE_ALLOWED_COMMANDS` lets it run the everyday ones (python,
pip, pytest, node, npm, local git, listing files…), each through both its Bash
and its PowerShell tool — on Windows it uses PowerShell as often as Bash, and a
Bash-only list got `python add.py` refused. No `git push`. When Claude needs
more, the agent calls `claude_allow`, which asks **you** (Approve/Deny on the
Agents tab, or say so in chat).

**The agent's own tools** (19, in `endpoints/coder_tools.py` and
`endpoints/toolmaking.py`) are scoped to it with `scope=CODER`, so they never
appear in the assistant's tool list:

| | |
|---|---|
| Claude | `claude_send`, `claude_wait`, `claude_sessions`, `claude_history`, `claude_stop`, `claude_allow` |
| Sandbox | `sandbox_list`, `sandbox_read`, `sandbox_search`, `sandbox_write`, `sandbox_edit`, `sandbox_run`, `sandbox_projects` |
| The job | `coding_plan`, `ask_user`, `coding_finish` |
| BananaGent's tools | `tool_make`, `tool_list`, `tool_remove` — see *Tools it makes for itself* |

It can edit files itself, but the prompt keeps that to trivia; real changes go
to Claude. `sandbox_run` runs PowerShell in the project folder to check Claude's
work. Paths outside the sandbox are refused, and commands that look like they
reach outside, delete folders, change the system, install globally or publish
something stop and ask you first. That's a tripwire against mistakes, not a
security boundary.

**Long jobs.** A message to Claude waits up to 10 minutes, then hands back
"still working" with Claude's live activity so the agent can `claude_wait`,
check in, or stop it. Once the agent's own conversation passes
`CODER_CONTEXT_CHARS`, older tool results are shortened and it's handed its plan
again (`coding_plan` exists partly for this). A reply with no tool call doesn't
end the job — it's nudged to carry on or finish.

**From the assistant** it's four tools, for the reason in the next section:

| Endpoint | Does |
|---|---|
| `code_do(task, project)` | Starts it on a job; reuse a project name to keep working on it |
| `code_status()` | Plan, what Claude is doing now, pending question, final report, projects |
| `code_message(text, decision)` | Your answer to its question, approve/deny a step, or new instructions mid-job |
| `code_stop()` | Stops it and any Claude run it has going |

### Why there are only four of them

Measured on the old voice build, with `gemma4:e4b` choosing the tools — worth
rechecking against whatever model the box ends up talking to.

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

### Tools it makes for itself

The coding agent can give BananaGent new tools. Claude writes a script in a
sandbox project, say a USPTO trademark search. The agent checks that it works,
then calls `tool_make`, which declares the tool's arguments and hooks it up to
the script. From the next message on it's an endpoint like any other: in the
assistant's catalog, called with `[[call uspto_search {"query": "nike"}]]`,
and handed to worker subagents if it's MINOR. Ask for it however you like
(*"make yourself a tool that searches trademarks"*). The assistant sends that
to `code_do`, and the coding agent's prompt covers the rest.

A made tool is one JSON file in `sandbox/.banana/tools/`. The script stays in
its project, so when Claude fixes the script, the tool is fixed too:

```json
{
  "description": "Searches USPTO trademarks by name ...",
  "tier": "minor",
  "project": "uspto-search",
  "script": "uspto_search.py",
  "params": {
    "query": {"type": "string", "description": "The mark to search for.", "required": true},
    "limit": {"type": "integer", "description": "How many results.", "default": 10}
  },
  "pass_args": "json",
  "timeout": 60
}
```

**The script's contract.** It runs in its project folder. A `.py` script uses
the project's `.venv` if there is one, otherwise the `python` on PATH, which is
the one Claude tested it with rather than BananaGent's own. `.js` scripts run
with node and `.ps1` scripts with PowerShell. The arguments arrive as a JSON
object on stdin and in `BANANA_TOOL_ARGS`. With `"pass_args": "flags"` they
arrive as `--name value` instead, for an argparse script. The script prints its
result to stdout, which is parsed if it's JSON. On failure it exits non-zero,
and its stderr becomes the error the model reads. Arguments are converted to
their declared types first (`"5"` to `5`, `"true"` to `true`), and a run that
passes its `timeout` is killed. Nothing goes through a shell, so an argument
can't become a command. The script itself runs with your rights, like anything
Claude writes.

**Guard rails.** `tool_make` runs the tool once with real `test_args` and saves
it only if that run works. It refuses a name that's already a built-in, a script
outside the sandbox, an argument without a description, and test arguments the
tool doesn't declare. A MAJOR tool can skip the test, since its test would
really do the thing. The folder is re-read whenever a file in it changes, so a
hand edit, a new tool or a removal applies from the next message with no
restart. `tool_remove` moves the file to `tools/removed/` instead of deleting
it.

Every made tool adds to the list the model reads before each reply, and
*Why there are only four of them* above shows what a long list costs. Keep
descriptions tight, and remove tools that aren't earning their place. Adding or
changing a tool also changes the system prompt, so the Spark re-reads it once
(~20s) on the next message.

### Putting apps online

Ask for an app *"online"*, *"on my phone"* or *"so I can open it anywhere"* and
the coding agent deploys it. Hosting is **Vercel** and the database is **Neon
Postgres**, with one Neon project per app. It runs both CLIs itself, with no
login and no browser, and finishes with a link. When you edit an app that's
already online, the change is redeployed to the same link before the job
counts as done.

```
secrets.env                 VERCEL_TOKEN, NEON_API_KEY — gitignored
tools/node_modules/.bin/    the vercel and neon CLIs
sandbox/.banana/deployments.json   project -> its live URL
```

**Keys.** `secrets.env` is read by `workspace.cloud_env()` and handed only to
Claude's runs and `sandbox_run`. The assistant's model and the browsing agent
never see it. Both accounts are on free plans with no card on file, so a
runaway deploy can hit a limit and be paused, but it can't run up a bill. Neon
(`NEON_ORG_ID`, `NEON_REGION`) and Vercel (`VERCEL_SCOPE`) settings are in
`config.py`.

**The CLIs** are installed into `tools/`, not globally:

```bash
npm install --prefix tools
```

A global `npm i -g` run from inside the Claude desktop app lands in that app's
private copy of AppData. BananaGent, started at login, never sees it.
`cloud_env()` puts `tools/node_modules/.bin` first on PATH instead.

**What it can't do on its own.** Deleting a Vercel project or a Neon
database, and buying a domain, are refused to Claude
(`CLAUDE_DENIED_COMMANDS`). Through `sandbox_run` they stop and ask you. Apps
that hold your own data get a password (`APP_PASSWORD` on Vercel) unless you
say they should be public.

**Two traps, both hit while setting this up:**

- `vercel link` walks up to the nearest `.git` and connects the new Vercel
  project to that repo's GitHub remote. For a sandbox project, the nearest
  one was BananaGent's own repo. So `sandbox/` is now a git repository of its
  own with no remote (`workspace._fence_off_git()`), and the walk stops there.
- The URL `vercel deploy` prints is that one deployment's, and it's behind
  Vercel's login (it answers 302). Your link is the production alias that
  `vercel inspect` lists, such as `https://hello-neon-mu.vercel.app`.

## Memory

Two kinds, in `memory/`. Both files hold personal details and are gitignored.

**Short-term** is the recent conversation: every message and reply, kept as a sliding
window of the last ~20,000 tokens (`SHORT_TERM_TOKENS`) and sent with every request.
It's saved to `memory/conversation.json`, so a restart picks up where you left off and
the Chat tab reopens on it. Only finished replies go in — one that failed or was cut
off isn't something the model should think it said.

Replies that used tools are replayed to the model as they happened — each
`[[call …]]`, its result (cut to `MEMORY_RESULT_CHARS`), then the answer — and that
counts toward the window. Keeping only the answers looked tidier and broke the model:
seeing itself say the time with no `get_time` call, it stopped calling tools. Measured
on the same history, *"what's the volume and the time?"*: answers only, 0/5 called a
tool and all five invented a volume; with the calls replayed, 5/5 called both and got
it right.

The window slides in steps: once it's over 20k it drops the oldest exchanges down to
16k (`SHORT_TERM_SLIDE_TO`). Every slide changes the start of the conversation, which
makes the Spark re-read the whole thing — measured at ~23s, against ~1s when it can
reuse its cache. Sliding a little further each time means that happens once every
several messages instead of on every one (60 long exchanges: 6 re-reads instead of
~35). Set `SHORT_TERM_SLIDE_TO` equal to `SHORT_TERM_TOKENS` for a strict window.

**Long-term** is `memory/MEMORY.md`: plain markdown, one fact per line under a few
headings, kept forever and sent with every message. The model saves facts on its own
when you mention something worth keeping (tested: a sister, a brother, a new hobby
saved in passing; small talk and arithmetic not), fixes them when they change, and
removes them when you ask it to forget. Edit it by hand whenever you like — the next
message sees it. Every change through the tools backs up the old file to
`memory/history/` first, and it's capped at `LONG_TERM_MAX_CHARS` so it can't crowd out
the conversation.

| Endpoint | Does |
|---|---|
| `memory_read()` | The file as written, to copy exact wording before an edit |
| `memory_add(fact, section)` | One fact under a heading (made if missing; *Notes* if none given) |
| `memory_edit(find, replace)` | Changes one exact passage; an empty replace deletes it |
| `memory_write(text)` | Rewrites the whole file, for tidying up |
| `memory_open_file()` | Opens `MEMORY.md` in your text editor |
| `conversation_clear()` | Empties the short-term window; long-term is kept |

If the model answers first and saves a fact afterwards, it would otherwise write its
whole answer out again once the save returns. The tool result tells it you've already
seen what it wrote, and the prompt asks for calls before the answer.

## The business

The business you're building gets a record of its own in `business/`, apart from
long-term memory: `MEMORY.md` is one-line facts about you, and a business needs more than
that — people to reach and when to chase them, a to-do list, a dated record of what was
decided and why, and numbers that change. Both files hold business plans and contacts and
are gitignored.

- `business/BUSINESS.md` — the brief: what the business is, its product, customers,
  positioning, market, competitors, pricing, name, open questions. Plain markdown, the
  title line is the business's name. Edit it by hand or in the dashboard's *Business*
  tab; capped at `BUSINESS_MAX_CHARS`.
- `business/data.json` — leads (with every touch), tasks, the log (decisions,
  milestones, learnings, setbacks) and metrics (every value kept, dated).

All of it is sent with every message, after long-term memory: the brief, open tasks,
live leads (up to 25, soonest follow-up first), the latest of each metric and the last
12 log entries. Customers, lost leads, closed tasks and older history are a
`business_leads` or `business_history` call away. Dates are shown with how they stand
against today — *"Mon Sep 28 (3 days overdue)"* — since that's arithmetic the model gets
wrong. Every write backs the old file up to `business/history/` (the last 200 of each).
The memory review also sends business facts here rather than to `MEMORY.md`.

| Endpoint | Does |
|---|---|
| `business_read(section)` | The brief, or one section of it, as written |
| `business_leads(stage, search)` | Every lead in full, with each touch, closed ones included |
| `business_history(search, kind)` | The whole log, closed tasks, and every metric's history |
| `business_note(fact, section)` | One lasting fact into the brief |
| `business_edit(find, replace)` | Changes one exact passage of the brief; empty deletes it |
| `business_log(entry, kind, date)` | A dated decision, milestone, learning, setback or note |
| `business_lead(name, …)` | Adds a lead or updates the one with that exact name |
| `business_lead_touch(name, what, stage, follow_up)` | Records contact, moves the stage, sets the follow-up |
| `business_task_add(task, due, priority, notes)` | A to-do, shown until it's closed |
| `business_task_update(task, state, …)` | Done, dropped, reopened, or a new due date/priority/wording |
| `business_metric(name, value, note)` | Today's value of a number such as MRR or signups |
| `business_open_file()` | Opens `BUSINESS.md` in your text editor |

Leads and tasks are picked out by name loosely — case doesn't matter, and part of a
task's wording will do if it matches only one — except that `business_lead` only updates
on an exact name, so adding "Sam" never overwrites "Samantha Lee".

## Subagents

For jobs that would swamp the conversation — writing something long, working through a
lot of material, many look-ups in a row — the model calls `subagent_run(task, kind)`.
A subagent is a fresh run of the same model with:

- its own prompt (`subagents/<kind>.md`) and its own set of tools;
- its own memory — every message and tool result of the run, kept whole, no window —
  and none of the assistant's, so the task has to carry its context;
- no step limit — it runs until it answers or its conversation outgrows the context.

When it finishes, its final reply goes back to the model as the tool result and the
subagent's conversation is thrown away. It goes through the same `brain.converse()` tool
loop, so the call protocol and its safeguards are shared. Subagents can't start
subagents, or touch the assistant's memory or prompts.

There are two kinds. **worker** is general-purpose, with the read-only (MINOR) tools.
Tested live: *"write me a 400-word story"* went to a worker, which came back in 30s with
a 380-word story. The main model then relays it, which for long writing doubles the
time — file-writing tools, when they exist, are the fix: the subagent writes the file and
reports back a line. Adding a kind is a `Kind(...)` and `register()` in
`subagents/__init__.py` plus its `.md` prompt; when a kind gets MAJOR tools, make
`subagent_run` MAJOR too.

**research** is for research projects. Its only tools are `web_search` and `get_time`. Its prompt
(`subagents/research.md`) has it break the question into angles, search broad and then
deep, follow leads, cross-check anything important, and stop only once new searches keep
turning up what it's already seen — then write one report: bottom line, the landscape,
key facts with sources, what to watch out for, next steps, and links. `tools.md` tells
the assistant to send any "research this" request here rather than searching itself, and
to write the task as a full brief: the question, why the user wants to know, what they
need back, the constraints, and what they already know or have ruled out.

Only what the subagent writes after its last tool call goes back as its result. Told not
to, the model still narrates between searches ("Let me try another angle…"), and twenty
of those lines ahead of the report buried it. Every subagent's task is also stamped with
the date. Without that, a research run in 2026 searched for "best … 2025".

Tested live with *"best standing desks under $600, 55 inches or wider"*: 21 searches in
5 minutes. It came back with three picks, each with price, height range, capacity and
warranty; flagged where sources disagreed and that one pick's realistic build price is
over budget; and linked ten review and retailer pages. `subagent_run` blocks while it
works, so the assistant's reply waits on it, with the tool line showing "running".

## Web search

`web_search(query, count)` (`endpoints/search.py`) returns the top results — title, link
and a snippet — with no API key and no browser. The [ddgs](https://pypi.org/project/ddgs/)
package reads Bing's results page, falling back to DuckDuckGo and then Yahoo
(`SEARCH_BACKENDS`). It has to be ddgs rather than a plain HTTP client, because ddgs
presents a real browser's TLS fingerprint. Without it the engines spot a script and hand
back junk that *looks* like results: Bing matches only the first word (*"standing desk
55x30 dual motor"* came back as MLB standings and dictionary pages), DuckDuckGo answers
202, and Mojeek and Brave show a captcha or a 429. A research run did 26 searches against
that and got nothing usable. Searches are spaced at least `SEARCH_GAP_SECONDS` apart,
an empty answer is retried once, and a repeated query is answered from memory. A result is
about 2 KB, so 30 of them fit in a research subagent's context with room to spare. The
assistant has the tool too, for quick questions.

## Email

`send_email(to, subject, body, cc)` (`endpoints/mail.py`) sends plain-text email to any
address, always from BananaGent's own Gmail (`EMAIL_ADDRESS` in `config.py`). The sender
isn't an argument, so the model can't send as anyone else, and every message is marked as
BananaGent's: the From name reads *BananaGent (AI assistant)* and a footer
(`EMAIL_FOOTER`) says an AI assistant sent it. Gmail keeps a copy in the account's Sent
folder, and replies come back to that inbox.

It goes through Gmail's SMTP server, which won't take the account password — it needs an
**app password**. Once: turn on 2-Step Verification for the account, make one at
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords), and paste
it into `secrets.env` as `GMAIL_APP_PASSWORD=`. Only `send_email` reads it; `GMAIL_*` keys
are kept out of the environment Claude Code's runs get. Until it's set, the tool answers
that it isn't set up rather than failing silently.

## Outside platforms

BananaGent can sign in to services on your behalf and use them as you. Each one is a
module in `integrations/`; today there's one, **Google Classroom**.

| Endpoint | Does |
|---|---|
| `integrations_status()` | Every platform, what it gives, and whether it's connected and as whom |
| `integration_connect(platform, disconnect)` | Opens the platform's sign-in page in your browser and waits for you to approve; or signs out |
| `classroom(action, course, assignment, days)` | Reads Classroom — see below |

Ask *"connect to Google Classroom"* and a Google sign-in page opens in your default
browser. Pick the account Classroom is on (your school account), approve, and the tab
says it's connected. The login is kept in `integrations/tokens/classroom.json`
(gitignored — it's as good as a password to what it covers), so it survives restarts.
*"Disconnect Classroom"* revokes it with Google and deletes the file.

`classroom` is one tool taking an `action`, for the same reason the prompt tools are
(see *Why there are only four of them*):

| Action | Gives |
|---|---|
| `due` | What's due in the next `days` (default 7) and what's overdue, across every class, with your status on each — *"what homework do I have?"* |
| `courses` | Your active classes, and whether you teach or take each |
| `work` | A class's assignments, newest first |
| `assignment` | One assignment in full: instructions, attached files and links, due date, your submission and grade (as a teacher, how many have turned it in) |
| `announcements` | One class's recent announcements, or every class's from the last `days` |
| `materials` | A class's posted materials |
| `people` | A class's teachers and students |

Classes and assignments can be named loosely — *"math"*, *"lab report"* — and are
matched against their titles. Due dates come back in local time with how long until
(or how overdue), so the model doesn't do date arithmetic.

It's **read-only** (`CLASSROOM_SCOPES` in `config.py`). That's partly caution and partly
Google: Classroom only lets an app change coursework and submissions *that app created*,
so "turn in my essay" could never work on a teacher's real assignment.

### Setting up Google sign-in (once)

Google needs to know which app is asking, so BananaGent needs its own OAuth client:

1. In [console.cloud.google.com](https://console.cloud.google.com), make a project
   (or use one), and in **APIs & Services → Library** enable the **Google Classroom API**.
2. **APIs & Services → OAuth consent screen**: if you can choose **Internal** (the project
   is under your school's Google Workspace), do. Otherwise choose **External**, and add your
   school address under **Test users**.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID**, type
   **Desktop app**. Copy the client ID and secret into `secrets.env`:

   ```
   GOOGLE_CLIENT_ID=…apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=…
   ```

Then ask BananaGent to connect. Two things that can get in the way:

- An **External** app left in *Testing* mode gives logins that expire after **7 days**.
  When that happens the tool says so and BananaGent offers to reconnect; publishing the
  app (or using Internal) stops it.
- A school's Workspace admin can block third-party apps from Classroom. If Google's page
  says the app is blocked, only the admin can allow it.

`GOOGLE_*` keys are only read by `integrations/google.py`; they're kept out of the
environment Claude Code's runs get, like `GMAIL_*`.

### Adding another platform

A module in `integrations/` that calls `register(Platform(...))` with a name, a title,
a one-line description, and `connect`, `disconnect` and `status` functions — it then
shows up in `integrations_status` and `integration_connect` with no other change. Then
one endpoint in `endpoints/` for what it can do once connected. A Google product (Drive,
Calendar, Gmail) is a `GoogleLogin(name, title, scopes)` from `integrations/google.py`,
which does the sign-in, keeps the token, refreshes it, and turns Google's errors into
sentences the model can act on.

## Texting on Discord

BananaGent signs in as a Discord bot and you text it like a person: DM it from your
phone and it answers in the same conversation as the ` box and the Chat tab (a texted
message shows up there too, with its reply). `integrations/discord_bot.py` is the bot;
it's also a platform, so `integrations_status` shows it and `integration_connect` with
`discord` signs it in or out.

- **Who it listens to.** Only `DISCORD_ALLOWED_USERS`, or when that's empty, whoever
  owns the bot in the developer portal — everyone else is ignored, since the tools
  behind it can touch this computer. Every DM is answered; in a server, a message that
  mentions it, replies to it, or follows up within `DISCORD_FOLLOW_UP_SECONDS` of its
  last text there.
- **Replies are texts.** Each blank line in the model's reply ends one Discord message,
  sent the moment it's written, so an answer arrives as a couple of short texts rather
  than one block. App-only markup (buttons, local paths) is turned into plain text.
- **The typing indicator** is on from the moment your message lands until the last text
  is out, and only then. Discord clears it whenever the bot sends, so it's put back after
  every text except the last, and never re-sent once the reply is done.
- **Follow-ups steer the reply already under way.** A text that arrives while it's still
  answering you in that chat isn't queued and doesn't start over: it goes into the
  reply's `brain.Inbox`. Mid-stream, the stream is cut there (what was already texted
  stands; the half-written text is dropped) and the model goes again with your new text
  in front of it, marked as arriving mid-reply, so "actually make it Saturday" changes
  the answer at once. Between tool calls it's added before the next round; just as the
  reply ends, the reply carries on to answer it. None of that uses up tool rounds.
- **Files both ways.** What you send is saved under `discord-files/<date>/`; pictures are
  shown to the model (qwen3.8 has vision), small text files are read into the message,
  and anything else is a path its other tools can use. It sends files with `discord_send`.
- **Agents' news follows you.** If you last wrote from Discord, an agent finishing or
  needing you is texted to you there instead of popping up in the corner.

The texting rules are their own prompt part, `prompts/texting.md`, in every system
prompt rather than swapped in for Discord messages — a system message that changed with
where you wrote from would stop the Spark reusing its cache. Its tools are in
`endpoints/texting.py`: `discord_send` (text and/or a file, to here or any chat),
`discord_chats`, `discord_read`, `discord_react` and `discord_get_files`.

### Setting up the bot (once)

1. At [discord.com/developers/applications](https://discord.com/developers/applications),
   **New Application**, name it, then on its **Bot** page **Reset Token** and copy it.
2. On that same page, switch on **Message Content Intent** — without it the bot can't
   read what you send.
3. Paste the token into `secrets.env` as `DISCORD_BOT_TOKEN=...` (Claude Code's runs
   never get it; `workspace.cloud_env()` leaves `DISCORD_` keys out).
4. To DM it, it has to share a server with you: on **OAuth2 > URL Generator** tick `bot`,
   then *Send Messages*, *Read Message History*, *Attach Files* and *Add Reactions*, open
   the URL and add it to a server of yours. Then DM it from that server's member list.
5. Restart BananaGent (it signs in at startup when the token is there), or ask it to
   connect to Discord.

## Endpoints

Endpoints are the tools the model can call. Every endpoint is one decorated function.
That single declaration produces everything the system needs — the catalog entry the
model reads in `tools.md`, and the dispatch target `brain.py` runs:

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
outside world (`set_volume`, `open_url`, `app_quit`, `send_email`).
`MINOR` only reads and reports, so calling it is free (`get_time`, `system_stats`,
`app_status`). The tier is on each catalog entry the model reads, and the list is ordered
major-first.

**Adding one:** drop a module in `endpoints/` and decorate a function. The package imports
everything beside it at startup, so it appears in the model's tool list with no
registration step. `endpoints.bind(app)` gives handlers a handle on the running app;
`on_ui(fn)` hops to the Tk thread for anything touching the overlay.

Run `.venv\Scripts\python.exe -m endpoints` to print the full catalog.

### The catalog in the system prompt

With the text protocol the catalog *has* to be in the prompt — it's the only place the
model learns what exists. That's a reversal from the old voice build, which used
Ollama's native tools API and measured the prose catalog as harmful with
`gemma4:e4b` — the model read the list and answered *out of it* instead of calling
anything:

| | correct tool chosen |
|---|---|
| 25 tools, catalog in prompt | 11/18 |
| 25 tools, no catalog | **17/18** |
| 5 tools, catalog in prompt | 11/18 |
| 5 tools, no catalog | **18/18** |

The number of tools barely matters; the duplicated prose is what does the damage. With the
catalog in the prompt, *"set the volume to 25 percent"* called nothing and the model
replied "I've set your volume to 25 percent" — claiming an action it never took.
`qwen3.8:27b` with the text protocol hasn't shown that (see *The model*), but it's the
thing to re-measure if tool choice ever gets worse.

## Prompts

The system prompt is three markdown files in `prompts/`, joined in this order:

| File | What goes in it |
|---|---|
| `main.md` | What BananaGent is, where the user reaches it, how to answer |
| `personality.md` | How it sounds — change freely, nothing depends on it |
| `tools.md` | How it uses its tools, including the rules that stop it inventing facts and actions |

Edit them by hand or through the endpoints; either way they're re-read on change, so
the next message uses the new text with no restart. Notes inside `<!-- -->` are for
you and never reach the model. `.venv\Scripts\python.exe -m prompts` prints exactly
what the model receives.

| Endpoint | Does |
|---|---|
| `prompt_read(part)` | Reads `main`, `personality` or `tools` as written, or `assembled` for the whole prompt |
| `prompt_write(part, text)` | Replaces a part outright |
| `prompt_append(part, text)` | Adds a paragraph to the end — *"always answer in metric"* |
| `prompt_edit(part, find, replace)` | Swaps one exact passage; it must match once |
| `prompt_reset(part)` | Back to the stock text in `prompts/defaults/` |

It's one set taking a `part` rather than a set per file on purpose: five tools cost the
model far less attention than fifteen (see *Why there are only four of them* above).

Every write first copies the old version to `prompts/history/` (gitignored), so a bad
edit can always be recovered. A part whose file is deleted comes back from its default.
`{{tools}}` in `tools.md` expands to the live tool catalog, so new endpoints appear in
the prompt on their own. Keep it: without it the model doesn't know any tools exist.

## Layout

| File | What it does |
|---|---|
| `main.py` | Wires the hotkey, text box, overlay, chat and dashboard together |
| `brain.py` | Streams from the Spark's Ollama; the `[[call …]]` tool loop; history trimming |
| `overlay.py` | The transparent always-on-top window, the text box, and text layout |
| `water.py` | The orb: formation animation, shape math, palettes |
| `dashboard.py` | The dashboard window: nav, cards, gauges, animation |
| `diagnostics.py` | One reading of the agent's model, server, machine and session |
| `paint.py` | Antialiased shapes from distance fields, as PNGs for Tk |
| `icon.py` | Draws the banana, and writes `banana.ico` |
| `agents/registry.py` | Agent base class: status, activity, the approval gate |
| `agents/browser.py` | The browsing agent: Chrome, the step loop, what's gated |
| `agents/llm.py` | Stagehand's LLM callback, wired to the Spark's Ollama |
| `endpoints/registry.py` | The decorator, schemas, catalog, dispatch |
| `toolbox.py` | Tools the coding agent made: loading, declaring, running their scripts |
| `endpoints/toolmaking.py` | `tool_make`, `tool_list`, `tool_remove`, for the coding agent |
| `endpoints/app_control.py` | Endpoints for the assistant itself |
| `endpoints/dashboard.py` | Endpoints for the dashboard window |
| `endpoints/browser_agent.py` | Endpoints for the browsing agent |
| `endpoints/system.py` | Endpoints for the computer (volume, clipboard, power) |
| `endpoints/prompts.py` | Endpoints for reading and editing the prompts |
| `endpoints/memory.py` | Endpoints for long-term memory and clearing the conversation |
| `endpoints/business.py` | Endpoints for the business record: brief, leads, tasks, log, metrics |
| `endpoints/subagents.py` | `subagent_run`, which hands a job to a subagent |
| `endpoints/search.py` | `web_search`, through ddgs: Bing, then DuckDuckGo, then Yahoo |
| `endpoints/integrations.py` | `integrations_status` and `integration_connect`, for every outside platform |
| `endpoints/classroom.py` | `classroom`, which reads Google Classroom |
| `integrations/` | Outside platforms: the registry, Google sign-in (`google.py`), Classroom |
| `endpoints/coding.py` | The assistant's four coding-agent endpoints |
| `endpoints/coder_tools.py` | The coding agent's own tools (scope `coder`) |
| `agents/coder.py` | The coding agent: the plan → brief → check → repeat loop |
| `agents/coder.md` | Its prompt, including how to brief Claude |
| `claude_code.py` | Runs Claude Code headless: sessions, live activity, cost, what changed |
| `workspace.py` | The sandbox: projects, confined file access, `sandbox_run` and its tripwire |
| `memory/` | Short-term window and long-term `MEMORY.md` |
| `business/` | The business record: `BUSINESS.md` and `data.json` |
| `subagents/` | The subagent framework, and each kind's prompt |
| `prompts/` | The prompt files, their defaults, and the loader that assembles them |
| `config.py` | Every knob worth turning |

## Configuration

All in `config.py`. The ones you're most likely to touch:

- `HOTKEY` / `SUPPRESS_HOTKEY` — which key, and whether it's swallowed. Suppression is on
  so pressing it doesn't also type a backtick into whatever has focus. Turn it off if that
  gets in the way of writing code.
- `OLLAMA_HOST`, `MODEL`, `NUM_CTX`, `TEMPERATURE` — which model answers, where, and with
  how much context.
- `ORB_RADIUS`, `CORNER`, `WIDTH`, `MAX_HEIGHT` — size and placement.
- `AGENT_CHROME` — the browser the browsing agent drives (None: Chrome if installed, else Edge).
- `CLAUDE_MODEL`, `CLAUDE_PERMISSION_MODE`, `CLAUDE_ALLOWED_COMMANDS` — which Claude
  model the coding agent works with, and what it may run without asking.
  `CODER_MAX_STEPS` bounds one job.

Orb colors live in `water.py`'s `PALETTES`, keyed by state.

## Notes

- Needs 64-bit Python. The system-wide `python` here is 32-bit, which is why the project
  ships its own venv — see `requirements.txt`.
