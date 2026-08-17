"""Central knobs for BananaGent. Edit these, no code changes needed."""

# --- Hotkey -----------------------------------------------------------------
# The physical backtick/tilde key (the one left of "1"). Held = recording.
HOTKEY = "`"
# Swallow the keypress so holding it doesn't spam ``` into whatever app has
# focus. Set False if you'd rather keep typing backticks normally.
SUPPRESS_HOTKEY = True
# Held for less than this (seconds) is treated as a misfire, not a recording.
MIN_HOLD_SECONDS = 0.25

# --- Audio ------------------------------------------------------------------
# 16 kHz mono is what Gemma's audio encoder expects; we resample if the mic
# insists on its own rate.
SAMPLE_RATE = 16000
CHUNK = 512
# Which microphone to record from. INPUT_DEVICE_NAME is matched (case
# insensitive, substring) against the device list, so it survives the index
# shuffling that happens when you plug things in — unlike a hard-coded number.
# Set INPUT_DEVICE to an int from `python recorder.py` to override completely,
# or set both to None to just use the Windows default.
INPUT_DEVICE_NAME = "INSIGNIA"
INPUT_DEVICE = None
# Preferred host APIs, best first, when a name matches several entries.
HOST_API_ORDER = ("MME", "Windows WASAPI", "Windows DirectSound")
MAX_SECONDS = 60  # hard cap on one utterance, so a stuck key can't eat all RAM
# Below this peak amplitude we assume you didn't actually say anything.
SILENCE_PEAK = 0.012

# --- LLM --------------------------------------------------------------------
# The audio goes straight into this model — no speech-to-text step. If this tag
# isn't pulled, we fall back to the closest installed gemma. Startup logs the pick.
MODEL = "gemma4:e4b"
SYSTEM_PROMPT = (
    "You are a voice assistant. You receive the user's spoken audio directly. "
    "Listen to it and reply to what they actually said. "
    "Be brief and conversational: a couple of sentences unless asked for more."
    "\n\n"
    "You have tools. Each is tagged MAJOR (it performs an action and changes "
    "something) or MINOR (it only reads and reports). Call MINOR tools freely. "
    "Call a MAJOR tool only when the user clearly asked for that action."
    "\n\n"
    "You have no clock, no sensors, and no memory of earlier turns. You cannot "
    "know the time, the date, the battery level, the volume, or anything about "
    "this computer unless a tool tells you. Never state such a fact from memory "
    "— call the tool. Never say you have done something unless a tool actually "
    "returned success; claiming an action you did not take is the worst thing "
    "you can do. If no tool fits, just answer the question normally."
    "\n\n"
    "After a tool returns, say the outcome in one short spoken sentence. Never "
    "read out raw JSON, field names, or tool names. If a tool reports an error, "
    "say plainly what failed."
)
# How many times the model may call endpoints and look at the results before it
# has to produce a final answer. Stops a tool-calling loop from spinning.
MAX_TOOL_ROUNDS = 4
# gemma4 defaults to temperature 1.0, which is loose for deciding whether to
# call a tool. Lower is steadier without making replies robotic.
OPTIONS = {"temperature": 0.3}
# Paste the full endpoint catalog into the system prompt as well. Measured at
# 11/18 correct tool choices with this on versus 17/18 with it off — the model
# starts answering out of the list instead of calling anything. The major/minor
# split is carried on each tool's own description instead, which costs nothing.
# `python -m endpoints` prints the catalog for humans.
INLINE_CATALOG = False
# Sent alongside the audio, since the API wants some text in the user turn.
AUDIO_PROMPT = "Listen to this recording and respond to what I said."
# Gemma's thinking mode hallucinates on audio input, so it's off by default.
THINK = False
# Ollama holds the weights in memory this long between questions. The model is
# ~9.6 GB and takes most of a minute to cold-load, so keep it warm.
KEEP_ALIVE = "30m"
# No conversation history: every utterance is its own question. There's no
# transcript to carry (the model hears audio directly), so past turns could only
# be replayed as a placeholder string, which told the model it had said things
# without any record of what.
# Gemma 4's audio runner wedges every few requests upstream (ollama#15333) and
# the call then never returns. Keep this tight enough that a wedge is caught and
# retried quickly, but loose enough for a cold model load (~60s).
REQUEST_TIMEOUT = 100.0

# --- Speech ------------------------------------------------------------------
# Replies are spoken with Kokoro, locally, off the same CPU. Synthesis runs at
# roughly 3x real time, so speech starts on the first finished sentence rather
# than waiting for the whole answer. Turn this off and the app is silent but
# otherwise unchanged.
SPEAK = True
# One of 54 voices; `python -m voice` lists and demos them. The first letter is
# the accent (a American, b British, e Spanish, f French, i Italian, j Japanese,
# p Portuguese, z Chinese) and the second is the speaker's gender.
VOICE = "af_heart"
VOICE_SPEED = 1.0  # 0.5 to 2.0
VOICE_LANG = "en-us"
# Downloaded rather than committed — see the README. Missing files just mean no
# speech; nothing else notices.
TTS_MODEL = "models/kokoro-v1.0.fp16.onnx"
TTS_VOICES = "models/voices-v1.0.bin"

# --- Which Ollama server -----------------------------------------------------
# Ollama 0.30+ can't decode speech for gemma4 (it hears that you spoke but not
# what you said — see the README). 0.20.7 does. So we run a pinned 0.20.7 on
# its own port and leave the Ollama you have installed completely alone: it
# keeps its default 11434, its tray icon, and every other model you use.
# Both servers read the same model store, so nothing is duplicated on disk.
# To go back to your main install, set this to http://127.0.0.1:11434.
OLLAMA_HOST = "http://127.0.0.1:11435"
# Started on demand if that port isn't answering. Set to None to never autostart.
OLLAMA_SERVE_EXE = r"C:\Users\jacob\ollama-0.20.7\ollama.exe"
OLLAMA_START_TIMEOUT = 45.0
# Load the model into RAM at launch so the first question isn't a ~50s wait.
# It's ~9.6 GB resident, so turn this off if you'd rather not give that up at
# login — the app still works, the first question is just slow.
WARM_ON_START = True

# --- The browsing agent ------------------------------------------------------
# Its model runs on the *system* Ollama, not the pinned 0.20.7 below: that copy
# only exists because old Ollama can still hear speech, and a 27B tool-caller has
# no business on a two-year-old build. If this tag isn't pulled the agent falls
# back to the closest installed model and says so.
AGENT_MODEL = "qwen3.8:27b"
AGENT_OLLAMA_HOST = "http://127.0.0.1:11434"
AGENT_OPTIONS = {"temperature": 0.2}  # picking the next click wants a steady hand
AGENT_KEEP_ALIVE = "30m"
AGENT_THINK = False  # the reasoning trace fights with constrained JSON decoding
# How many steps one task gets before it has to stop and report.
AGENT_MAX_STEPS = 25
# How many past steps the model is reminded of each time.
AGENT_MEMORY = 12
# Chrome, not a downloaded Chromium — the one you already have. None to let
# Stagehand find its own.
AGENT_CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
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

# --- Overlay ----------------------------------------------------------------
# There's no panel: the window is fully transparent and only the orb and the
# answer text are painted. Text carries a drop shadow instead of a backdrop.
WIDTH = 420  # text wrap width; the orb itself is ORB_RADIUS
ORB_RADIUS = 26
MAX_HEIGHT = 460  # past this the answer scrolls its own tail into view
MARGIN = 24  # gap from the screen corner
CORNER = "bottom-right"  # bottom-right | bottom-left | top-right | top-left
OPACITY = 1.0

FG = "#ffffff"
# The orb's water colors live in water.py's PALETTES, keyed by state.
