"""Endpoints for the browsing agent — what the model sees of the Agents tab.

Kept deliberately few and deliberately short. The assistant's own model is a
small one, and adding nine endpoints here took its tool choice from 5/5 to 1/5
on questions that had nothing to do with browsing: past a certain amount of tool
text it stops emitting exact names and starts inventing plausible ones
("read_clipboard", "system_info"). Four tools with one-line descriptions cost it
nothing measurable, so anything that can be folded into another tool is.

That's also why approve and deny are one endpoint with a choice rather than two,
and why status returns the activity and the pending question instead of them
being separate calls.

Every one is tagged `agent="browser"`, which is what puts it in the agent's
dropdown on the dashboard.
"""

import agents
from .registry import MAJOR, MINOR, endpoint, param

AGENT = "browser"


def _browser():
    return agents.get(AGENT)


@endpoint(
    tier=MINOR,
    agent=AGENT,
    description=(
        "Reports what the web browsing agent is doing, what it has done so far, "
        "and whether it is waiting for you to approve a step."
    ),
    returns="Its status, task, current page, recent steps, and any pending question.",
)
def browser_status():
    state = _browser().snapshot()
    state["recent"] = [line["text"] for line in _browser().activity][-10:]
    return state


@endpoint(
    tier=MAJOR,
    agent=AGENT,
    description=(
        "Sends the web browsing agent off to do something on the internet in a "
        "real Chrome window — opening sites, filling in forms, working through a "
        "signup. Describe the whole job in one sentence. It works in the "
        "background and asks before anything that cannot be undone."
    ),
    # The model likes to compress "go to X and tell me Y" down to "go to X",
    # which loses the half that says what to come back with.
    params={"task": param(
        "string",
        "The user's whole request in their own words, including what they want "
        "reported back. Do not shorten it or leave any part of it out.",
        required=True)},
)
def browser_do(task):
    return f"Working on it: {_browser().start(task)}"


@endpoint(
    tier=MAJOR,
    agent=AGENT,
    description=(
        "Answers the step the browsing agent is waiting on — approve to let it "
        "submit the form, create the account or accept the terms it asked about, "
        "deny to refuse and stop it. Call this whenever the user answers yes, "
        "go ahead, do it, approve — or no, don't, stop that, cancel it, refuse."
    ),
    params={"decision": param("string", "approve or deny.", required=True,
                              enum=["approve", "deny"])},
)
def browser_answer(decision):
    choice = str(decision).strip().lower()
    if choice not in ("approve", "deny"):
        raise ValueError("Say approve or deny.")
    question = _browser().answer(choice == "approve")
    return f"{'Approved' if choice == 'approve' else 'Refused'}: {question}"


@endpoint(
    tier=MAJOR,
    agent=AGENT,
    description=(
        "Stops the browsing agent immediately, abandoning the task it is part "
        "way through. Its Chrome window stays open where it got to."
    ),
)
def browser_stop():
    _browser().stop()
    return "Stopped the browsing agent."
