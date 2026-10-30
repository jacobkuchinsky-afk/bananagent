"""Endpoints for the assistant itself — its window and its settings.

Anything the app can do to itself is reachable here, so "make the orb blue" or
"what's the hotkey" are answerable without touching the code.
"""

import os
import time

import config
import water
from .registry import MAJOR, MINOR, app, endpoint, on_ui, param

_STARTED = time.time()


def _here(name):
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)


# =============================================================================
# MINOR — read only
# =============================================================================

@endpoint(
    tier=MINOR,
    description=(
        "Reports the assistant's own runtime state: how long it has been "
        "running, its hotkey, whether the text box is open, and how many "
        "messages it has been sent."
    ),
    returns="An object with uptime_seconds, hotkey, box_open and messages.",
)
def app_status():
    a = app()
    return {
        "uptime_seconds": round(time.time() - _STARTED, 1),
        "hotkey": config.HOTKEY,
        "box_open": a.ui.state == a.ui.TYPING,
        "messages": a.turns,
    }


@endpoint(
    tier=MINOR,
    description=(
        "Lists every capability the assistant has, with the exact description of "
        "each and whether it is major (changes something) or minor (read only). "
        "Use this to answer questions about what you can and cannot do."
    ),
    returns="Counts plus a one-line summary of each capability, grouped by tier.",
)
def app_list_endpoints():
    from .registry import MAJOR, MINOR, by_tier

    def brief(e):
        # First sentence only — handing back 25 full descriptions buries the
        # model and it answers about something else entirely.
        return f"{e.name}: {e.description.split('. ')[0].rstrip('.')}"

    return {
        "actions_i_can_perform": [brief(e) for e in by_tier(MAJOR)],
        "things_i_can_look_up": [brief(e) for e in by_tier(MINOR)],
    }


@endpoint(
    tier=MINOR,
    description=(
        "Returns the text of the answer currently shown on screen, or an empty "
        "string if nothing is displayed."
    ),
    returns="The displayed text.",
)
def app_last_answer():
    return on_ui(lambda: app().ui._text)


@endpoint(
    tier=MINOR,
    description=(
        "Returns the most recent lines of the assistant's own log file, for "
        "diagnosing why something failed."
    ),
    params={"lines": param("integer", "How many trailing lines to return, 1-200.", default=25)},
    returns="The log text.",
)
def app_recent_log(lines=25):
    path = _here("bananagent.log")
    if not os.path.exists(path):
        return "(no log file yet)"
    lines = max(1, min(200, int(lines)))
    with open(path, encoding="utf-8", errors="replace") as f:
        return "".join(f.readlines()[-lines:]) or "(log is empty)"


@endpoint(
    tier=MINOR,
    description=(
        "Reads one of the assistant's configuration values by name, for example "
        "HOTKEY, ORB_RADIUS, CORNER or OPACITY. Names are case insensitive."
    ),
    params={"name": param("string", "The setting to read.", required=True)},
    returns="The setting's current value, or an error naming the valid settings.",
)
def app_get_setting(name):
    key = str(name).strip().upper()
    if not hasattr(config, key) or key.startswith("_"):
        options = ", ".join(k for k in vars(config) if k.isupper())
        raise ValueError(f"No setting called '{name}'. Valid settings: {options}")
    return {key: getattr(config, key)}


# =============================================================================
# MAJOR — these change the app
# =============================================================================

@endpoint(
    tier=MAJOR,
    description=(
        "Shuts the assistant down completely. It stops listening for the hotkey "
        "and will not come back until relaunched or the computer is restarted. "
        "Only call this when explicitly asked to quit, close or exit."
    ),
)
def app_quit():
    a = app()
    a.ui.root.after(400, a.ui.root.quit)
    return "Shutting down."


@endpoint(
    tier=MAJOR,
    description=(
        "Restarts the assistant: it shuts down and immediately starts again, "
        "picking up changes to its code and to settings that say they apply on "
        "restart. The conversation survives. Call this when asked to restart "
        "or reload."
    ),
)
def app_restart():
    a = app()
    # A beat's delay so this reply gets to finish before the window goes.
    a.ui.root.after(400, a.restart)
    return "Restarting."


@endpoint(
    tier=MAJOR,
    description=(
        "Hides the on-screen overlay immediately, discarding the answer being "
        "shown. The assistant keeps running and stays ready for the hotkey."
    ),
)
def app_hide_overlay():
    on_ui(app()._hide)
    return "Overlay hidden."


@endpoint(
    tier=MAJOR,
    description=(
        "Displays a line of text on the overlay, replacing whatever is there. "
        "Use for reminders or notes the user asks to be put on screen."
    ),
    params={"text": param("string", "The text to display.", required=True)},
)
def app_show_message(text):
    def do():
        ui = app().ui
        ui.clear()
        ui.append(str(text))
        ui.set_state(ui.ANSWERING)
    on_ui(do)
    return f"Showing: {text}"


@endpoint(
    tier=MAJOR,
    description=(
        "Changes the color of the orb. Accepts a named color (yellow, orange, "
        "red, green, blue, purple, pink, white) and applies it to every state "
        "except errors, which stay red."
    ),
    params={"color": param(
        "string", "The color name.", required=True,
        enum=["yellow", "orange", "red", "green", "blue", "purple", "pink", "white"])},
)
def app_set_orb_color(color):
    schemes = {
        "yellow": ("#e8a317", "#ffc93c", "#fff3c4"),
        "orange": ("#e2701a", "#ff9b3d", "#ffdcb8"),
        "red": ("#c0392b", "#ff6b5b", "#ffd5cf"),
        "green": ("#1f9254", "#3ddc84", "#d2ffe4"),
        "blue": ("#1c6fd4", "#4da3ff", "#d4e9ff"),
        "purple": ("#6b3fc4", "#a97bff", "#e8dcff"),
        "pink": ("#c23d7a", "#ff77b0", "#ffdcec"),
        "white": ("#9aa0a8", "#e4e8ee", "#ffffff"),
    }
    key = str(color).strip().lower()
    if key not in schemes:
        raise ValueError(f"Unknown color '{color}'. Choose from: {', '.join(schemes)}")
    for state in ("typing", "thinking", "answering"):
        water.PALETTES[state] = schemes[key]
    return f"Orb is now {key}."


@endpoint(
    tier=MAJOR,
    description=(
        "Moves the overlay to a different corner of the screen. Takes effect on "
        "the next time the overlay appears."
    ),
    params={"corner": param(
        "string", "Which corner.", required=True,
        enum=["bottom-right", "bottom-left", "top-right", "top-left"])},
)
def app_set_corner(corner):
    choice = str(corner).strip().lower()
    valid = ("bottom-right", "bottom-left", "top-right", "top-left")
    if choice not in valid:
        raise ValueError(f"Unknown corner '{corner}'. Choose from: {', '.join(valid)}")
    config.CORNER = choice
    return f"Overlay will appear in the {choice}."


@endpoint(
    tier=MAJOR,
    description=(
        "Changes the size of the orb in pixels. Normal is 26; smaller is subtler, "
        "larger is easier to see. Takes effect when the assistant is restarted."
    ),
    params={"radius": param("integer", "Orb radius in pixels, 8-80.", required=True)},
)
def app_set_orb_size(radius):
    r = int(radius)
    if not 8 <= r <= 80:
        raise ValueError("Orb radius must be between 8 and 80 pixels.")
    config.ORB_RADIUS = r
    return f"Orb radius set to {r}px; restart to see it."


@endpoint(
    tier=MAJOR,
    description=(
        "Controls whether the hotkey is swallowed. When on, pressing the key "
        "never types its character into other programs. Turn it off if you "
        "need to type that character normally."
    ),
    params={"enabled": param("boolean", "True to swallow the key.", required=True)},
)
def app_set_hotkey_suppression(enabled):
    config.SUPPRESS_HOTKEY = bool(enabled)
    state = "swallowed" if config.SUPPRESS_HOTKEY else "passed through"
    return f"The {config.HOTKEY} key will be {state}; restart to apply."
