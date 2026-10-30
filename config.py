"""Central knobs for BananaGent. Edit these, no code changes needed."""

# --- Hotkey -----------------------------------------------------------------
# The physical backtick/tilde key (the one left of "1"). Press it to pop the
# text box up; press it again (or Esc) to put it away.
HOTKEY = "`"
# Swallow the keypress so it doesn't also type a ` into whatever app has focus.
# Set False if you'd rather keep typing backticks normally.
SUPPRESS_HOTKEY = True

# --- The model --------------------------------------------------------------
# qwen3.8:27b on Jacob's DGX Spark, reached over Tailscale. Ollama there is
# stateless, so every request carries the system prompt and the conversation.
OLLAMA_HOST = "http://100.82.77.31:11434"
MODEL = "qwen3.8:27b"
# Always sent. The Spark runs 16 parallel slots, and without an explicit
# context Ollama reserves a huge default for each and runs out of memory.
# Sized to hold the system prompt, long-term memory, the 20k-token short-term
# window, and room for tool results and the reply. Tested loading on the Spark.
# If it ever says "requires more system memory", lower this first.
NUM_CTX = 40960
TEMPERATURE = 0.7
# Qwen's hidden reasoning phase. Off is the fastest first token; on, it's
# discarded rather than shown.
THINK = False
KEEP_ALIVE = -1  # stay loaded, so only the first message after a swap waits
# How many tools one reply may call before it has to answer.
MAX_TOOL_ROUNDS = 6
# A tool result longer than this is cut before the model sees it.
TOOL_RESULT_CHARS = 6000
# Rough English average, used wherever tokens are budgeted.
CHARS_PER_TOKEN = 4

# --- Memory -----------------------------------------------------------------
# Short-term: the most recent conversation, as a sliding window of about this
# many tokens. Older turns fall out of it. Kept on disk, so it survives restarts.
SHORT_TERM_TOKENS = 20000
# Once it's over, slide down to this rather than just under the limit. Every
# slide changes the start of the conversation, so the Spark has to re-read the
# whole window (measured: ~23s, against ~1s when it can reuse its cache).
# Sliding in one bigger step means that happens once every several messages
# instead of on every one. Set equal to SHORT_TERM_TOKENS for a strict window.
SHORT_TERM_SLIDE_TO = 16000
# Earlier tool calls stay in the window with their results, cut to this. Without
# them the model sees itself answering "what time is it" with no tool call, and
# starts doing exactly that — reusing old times and making up the volume.
MEMORY_RESULT_CHARS = 600
# Long-term: memory/MEMORY.md, sent with every message and never trimmed. The
# memory tools refuse to grow it past this, so it can't crowd out the rest.
LONG_TERM_MAX_CHARS = 24000
# Every this many messages — yours and its replies both count — the model is
# told to go back over them and save every lesson and important thing it
# learned to long-term memory. It's a note added to your next message that only
# the model sees. 0 turns it off.
MEMORY_REVIEW_EVERY = 15
# Extra tool calls a review reply gets on top of MAX_TOOL_ROUNDS, since each
# saved fact is a call of its own.
MEMORY_REVIEW_ROUNDS = 10

# --- The business -------------------------------------------------------------
# business/: the company the user is building, kept apart from long-term memory
# and sent after it with every message — the brief (BUSINESS.md) plus open
# tasks, live leads, metrics and the recent log (data.json). The brief is capped
# at this; the lists are trimmed to their most recent when shown (business/).
BUSINESS_MAX_CHARS = 12000

# --- Subagents --------------------------------------------------------------
# A subagent is a fresh model run with its own memory, for jobs too big to do
# inline. It has no step limit: it runs until it answers, or until its
# conversation outgrows NUM_CTX (brain.converse checks that every round).
SUBAGENT_RESULT_CHARS = 16000  # what gets handed back to the main model

# --- Web search -------------------------------------------------------------
# web_search reads search engines' results pages through ddgs — no key.
# Engines ddgs tries, in order, until one answers; "auto" is its own choice.
SEARCH_BACKENDS = "bing, duckduckgo, yahoo"
SEARCH_TIMEOUT = 15
# Minimum gap between two searches. Fire them faster and the engines start
# answering with bot checks instead of results.
SEARCH_GAP_SECONDS = 1.5
SEARCH_SNIPPET_CHARS = 300  # per result
# Load the model when the app starts, so the first message doesn't sit
# through a ~30s load.
WARM_ON_START = True

# --- The browsing agent ------------------------------------------------------
# Same model on the same Spark as the main assistant (MODEL at OLLAMA_HOST, with
# NUM_CTX and KEEP_ALIVE) — only the system prompt differs: agents/browser.md.
AGENT_OPTIONS = {"temperature": 0.2}  # picking the next click wants a steady hand
AGENT_THINK = False  # the reasoning trace fights with constrained JSON decoding
# How many steps one task gets before it has to stop and report.
AGENT_MAX_STEPS = 25
# How many past steps the model is reminded of each time.
AGENT_MEMORY = 12
# The browser to drive. None finds it: Chrome wherever it's installed, else
# Edge (same engine, always on Windows). Set a full path to force one.
AGENT_CHROME = None
# Its own profile directory, so logging into a site once sticks for later tasks.
AGENT_PROFILE = "browser-profile"
# Watch it work. Headless is faster but you can't see what it's about to do,
# and you're the one approving the steps that matter.
AGENT_HEADLESS = False
AGENT_VIEWPORT = (1280, 900)
# Send the model a picture of the page as well as the text. qwen3.8 takes image
# input, and some pages only make sense looked at.
AGENT_SCREENSHOTS = True
# Page text handed to the model per step. Too much buries the actual controls.
AGENT_PAGE_CHARS = 6000
# One step's thinking, and how long an approval waits before it gives up.
AGENT_STEP_SECONDS = 240.0
AGENT_APPROVAL_SECONDS = 600.0
# Leave Chrome open between tasks. Off closes it when each task ends.
AGENT_KEEP_BROWSER = True

# --- The coding agent ---------------------------------------------------------
# BananaGent's model runs the job — plans, briefs Claude, reads its recaps,
# checks the work, goes again — and Claude Code writes the code. Everything
# happens in a project folder under this directory (relative to the app).
SANDBOX_DIR = "sandbox"
# Tool calls one coding task may make before it has to wrap up. A message to
# Claude is one call however long Claude works, so this is plenty.
CODER_MAX_STEPS = 80
# Once the agent's own conversation passes this many characters, older tool
# results are shortened. Kept well under NUM_CTX * CHARS_PER_TOKEN.
CODER_CONTEXT_CHARS = 90000
CODER_KEEP_RECENT = 8  # messages at the end that are never shortened
# How long a question to the user waits before it gives up and carries on.
CODER_QUESTION_SECONDS = 1800.0
CODER_APPROVAL_SECONDS = 900.0

# Claude Code. None finds it: on PATH, ~/.local/bin, or the desktop app's copy.
CLAUDE_EXE = None
CLAUDE_MODEL = None    # "opus", "sonnet", a full model name, or None for its default
CLAUDE_EFFORT = None   # low | medium | high | xhigh | max, or None for its default
CLAUDE_MAX_BUDGET_USD = None  # a cap on one message's spend, or None
# What Claude may do without asking. Nobody can answer a permission prompt in
# headless mode, so anything not allowed is simply refused, and the refusal is
# reported back to the coding agent — which can ask you to allow more with
# claude_allow. "acceptEdits" lets it change files in the project; the list
# lets it run the everyday commands a coding job needs. "auto" (a safety
# classifier decides) or "bypassPermissions" (anything at all) are the
# alternatives if this proves too tight.
CLAUDE_PERMISSION_MODE = "acceptEdits"
# Tools Claude may use freely, and command prefixes it may run — each one
# allowed through both its Bash and its PowerShell tool, since on Windows it
# reaches for PowerShell as often as Bash.
CLAUDE_ALLOWED_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "TodoWrite", "Skill"]
CLAUDE_ALLOWED_COMMANDS = [
    "vercel *", "neon *", "curl *", "Invoke-WebRequest *", "Invoke-RestMethod *",
    "python *", "py *", "python3 *", "pip *", "pytest *", "uv *",
    ".venv/Scripts/*", "./.venv/Scripts/*", r".venv\Scripts\*", r".\.venv\Scripts\*",
    "node *", "npm *", "npx *", "pnpm *", "yarn *",
    "git init*", "git status*", "git diff*", "git add *", "git commit *", "git log*",
    "git checkout *", "git restore *",
    "ls*", "dir*", "cat *", "type *", "mkdir *", "cd *", "echo *", "pwd", "which *",
    "head *", "tail *", "wc *", "Get-ChildItem*", "Get-Content *", "Get-Location",
    "Test-Path *", "New-Item *", "Set-Location *", "Write-Output *",
    "cargo *", "go *", "dotnet *",
]
# Refused even though the lines above would allow them — deny beats allow.
# Taking an app or its data down, or spending money, is the user's call; the
# coding agent can still ask for it through sandbox_run, which asks the user.
CLAUDE_DENIED_COMMANDS = [
    "vercel remove*", "vercel rm*", "vercel project rm*", "vercel project remove*",
    "vercel projects rm*", "vercel projects remove*", "vercel domains buy*",
    "vercel login*", "vercel logout*",
    "neon projects delete*", "neon project delete*", "neon databases delete*",
    "neon database delete*", "neon db delete*", "neon branches delete*",
    "neon branch delete*", "neon auth*", "neon login*", "neon profile*", "neon api-keys*",
]

# One message's hard limit; past it Claude is stopped.
CLAUDE_TURN_SECONDS = 1800
# How long claude_send waits for a reply before handing control back to the
# coding agent (which can then claude_wait again, check in, or stop it).
CLAUDE_WAIT_SECONDS = 600
# How much of Claude's reply the coding agent is shown at once.
CLAUDE_REPLY_CHARS = 9000

# sandbox_run: the coding agent's own commands, for checking Claude's work.
SANDBOX_RUN_SECONDS = 120
SANDBOX_RUN_MAX_SECONDS = 600
SANDBOX_OUTPUT_CHARS = 6000

# --- Deploying ------------------------------------------------------------------
# Apps go online on Vercel, with Postgres from Neon — one Neon project per app.
# The keys are in secrets.env (gitignored); see workspace.cloud_env(). Both
# accounts are on free plans with no card, so the worst a runaway deploy can
# do is hit a limit and get paused.
SECRETS_FILE = "secrets.env"
VERCEL_SCOPE = "mrbananas-projects"
NEON_ORG_ID = "org-square-king-02516159"
NEON_REGION = "aws-us-east-2"

# --- Email ----------------------------------------------------------------------
# send_email (endpoints/mail.py) sends only from BananaGent's own Gmail, over
# Gmail's SMTP server with an app password kept in secrets.env as
# GMAIL_APP_PASSWORD. Every message is marked as BananaGent's: the From name
# below, and the footer under the body, which the model can't leave off.
EMAIL_ADDRESS = "bananabotbuilds@gmail.com"
EMAIL_NAME = "BananaGent (AI assistant)"
EMAIL_FOOTER = ("--\nSent by BananaGent, an AI assistant, from "
                f"{EMAIL_ADDRESS}. Replies come back to it.")
EMAIL_SMTP_HOST = "smtp.gmail.com"
EMAIL_SMTP_PORT = 465
EMAIL_TIMEOUT = 30

# --- Outside platforms ------------------------------------------------------------
# integrations/: services BananaGent signs in to on the user's behalf. Each one
# connects once (integration_connect opens a sign-in page in the browser) and
# its login is kept in integrations/tokens/ (gitignored).
INTEGRATION_TOKENS_DIR = "integrations/tokens"
# How long integration_connect waits for the user to finish signing in.
INTEGRATION_CONNECT_SECONDS = 300
INTEGRATION_TIMEOUT = 30  # one request to a platform's API

# Google Classroom. Signs in with a Google Cloud "Desktop app" OAuth client
# whose ID and secret are in secrets.env as GOOGLE_CLIENT_ID and
# GOOGLE_CLIENT_SECRET (see the README). Read-only: BananaGent can see courses,
# assignments, announcements and your own submissions, and change nothing.
# Changing these means connecting again.
CLASSROOM_SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.students.readonly",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/classroom.topics.readonly",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
    "https://www.googleapis.com/auth/classroom.profile.emails",
]
# What "due" looks ahead by default, in days, and how far back "announcements"
# looks when no course is named.
CLASSROOM_DUE_DAYS = 7
CLASSROOM_RECENT_DAYS = 7
CLASSROOM_TEXT_CHARS = 3000  # one assignment's or announcement's text

# --- Discord ------------------------------------------------------------------
# Text BananaGent from anywhere: it signs in as a Discord bot with
# DISCORD_BOT_TOKEN from secrets.env (see the README) and answers like the `
# box does, in the same conversation (integrations/discord_bot.py). Off here,
# it only signs in when integration_connect asks it to.
DISCORD_ENABLED = True
# Discord user ids it takes messages from (right-click your name > Copy User
# ID, with Developer Mode on). Empty: whoever owns the bot in the developer
# portal. Anyone else is ignored.
DISCORD_ALLOWED_USERS = []
# Answer in servers too — when mentioned, replied to, or followed up within
# this many seconds of its last text there. DMs are always answered.
DISCORD_ANSWER_IN_SERVERS = True
DISCORD_FOLLOW_UP_SECONDS = 120
# Files people send are saved here (relative to the app), a folder per day.
DISCORD_FILES_DIR = "discord-files"
DISCORD_DOWNLOAD_MAX_MB = 50
DISCORD_UPLOAD_MAX_MB = 10  # what Discord takes from a bot without boosts
# Show pictures to the model (qwen3.8 has vision), up to four per message,
# and read small text files straight into the message.
DISCORD_SHOW_IMAGES = True
DISCORD_INLINE_TEXT_CHARS = 6000
DISCORD_CONNECT_SECONDS = 30
DISCORD_SEND_SECONDS = 30  # one send, before an endpoint gives up on it

# --- Overlay ----------------------------------------------------------------
# There's no panel: the window is fully transparent and only the orb, the text
# box and the answer text are painted. Text carries an outline instead of a
# backdrop.
WIDTH = 420  # text wrap width, and the text box fills what the orb leaves
ORB_RADIUS = 26
MAX_HEIGHT = 460  # past this the answer scrolls its own tail into view
MARGIN = 24  # gap from the screen corner
CORNER = "bottom-right"  # bottom-right | bottom-left | top-right | top-left
OPACITY = 1.0

FG = "#ffffff"
# The orb's water colors live in water.py's PALETTES, keyed by state.
