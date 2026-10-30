"""Endpoints for the dashboard window.

Everything the dashboard does by hand, the model can do through an endpoint: open it,
put it away, switch pages, force a fresh reading, and read out what it says.
The numbers here come from the same snapshot the window paints, so asking
"how much of the model is on the GPU" and looking at the gauge can't disagree.
"""

import diagnostics
from .registry import MAJOR, MINOR, app, endpoint, on_ui, param


def _dash():
    dash = getattr(app(), "dash", None)
    if dash is None:
        raise RuntimeError("the dashboard window isn't running")
    return dash


def _pages():
    from dashboard import PAGES

    return [name for name, _ in PAGES]


# =============================================================================
# MINOR — read only
# =============================================================================

@endpoint(
    tier=MINOR,
    description=(
        "Reports whether the dashboard window is currently on screen or "
        "minimized, and which page it is showing."
    ),
    returns="An object with open (true or false) and page.",
)
def dashboard_status():
    dash = _dash()
    return {"open": on_ui(dash.visible), "page": dash.page, "pages": _pages()}


@endpoint(
    tier=MINOR,
    description=(
        "Returns the full diagnostic reading the dashboard displays: the "
        "model answering messages, its size and quantization and context window, "
        "whether it is loaded and how much of it sits on the graphics card, the "
        "Ollama server's address and version, memory use, and this session's "
        "message count and reply times. Use this for any question about how the "
        "assistant or the model is doing."
    ),
    returns="An object with server, model, loaded, machine and app sections.",
)
def dashboard_diagnostics():
    # Deliberately not on_ui: this reads the network and would stall the window.
    return diagnostics.snapshot(app())


# =============================================================================
# MAJOR — these change what is on screen
# =============================================================================

@endpoint(
    tier=MAJOR,
    description=(
        "Opens the dashboard window and brings it to the front, on whichever "
        "page it was last showing (the chat, unless changed). Call this "
        "whenever the user asks to see, open or show the dashboard."
    ),
)
def open_dashboard():
    on_ui(_dash().show)
    return "Dashboard open."


@endpoint(
    tier=MAJOR,
    description=(
        "Minimizes the dashboard window back to the taskbar. The assistant keeps "
        "running and stays ready for the hotkey."
    ),
)
def close_dashboard():
    on_ui(_dash().hide)
    return "Dashboard minimized."


@endpoint(
    tier=MAJOR,
    description=(
        "Switches the dashboard to one of its pages. 'chat' is the conversation "
        "with the assistant; 'diagnostics' is about the model and this "
        "assistant; 'agents' lists the agents that can be sent off to do a job, "
        "and what each one can do; 'prompts' is where the user edits every "
        "prompt file by hand."
    ),
    params={"page": param("string", "Which page to show.", required=True,
                          enum=["chat", "diagnostics", "agents", "prompts"])},
)
def dashboard_show_page(page):
    dash = _dash()
    chosen = on_ui(lambda: dash.select(str(page).strip().lower()))
    return f"Dashboard is showing {chosen}."


@endpoint(
    tier=MAJOR,
    description=(
        "Takes a fresh diagnostic reading immediately instead of waiting for the "
        "dashboard's next automatic poll, and updates what is on screen."
    ),
)
def dashboard_refresh():
    # Off the UI thread on purpose — this one talks to the Ollama server.
    _dash().refresh()
    return "Dashboard refreshed."
