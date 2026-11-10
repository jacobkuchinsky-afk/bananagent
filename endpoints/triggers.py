"""Endpoints for messaging your future self (see triggers/).

A reminder goes off at a time; a watch goes off when a file or folder changes.
Either way the note comes back as an automatic notice that gets a reply of its
own, so the model can set one up now and act on it later — tell the user,
re-check something, carry on a job — with nobody there to ask.
"""

import triggers
from .registry import MAJOR, MINOR, endpoint, param


# =============================================================================
# MINOR — read only
# =============================================================================

@endpoint(
    tier=MINOR,
    description="Lists every reminder and file watch you've set that hasn't finished.",
    returns="One line per trigger: its id, when it goes off, and its note.",
)
def list_triggers():
    items = triggers.load()
    return "\n".join(triggers.describe(t) for t in items) if items else "No triggers set."


# =============================================================================
# MAJOR — changes something
# =============================================================================

@endpoint(
    tier=MAJOR,
    description=(
        "Sends a note to your future self at a set time: when it comes due you "
        "get it as a new message and act on it with nobody having to ask — tell "
        "the user, check on something, carry on a job. Use it for 'remind me…', "
        "for following up later, and for anything that has to happen at a time. "
        "Give at or in_minutes; add repeat or every_minutes to make it recur."
    ),
    params={
        "note": param("string", "What to tell or ask yourself when it goes off. Write it "
                                "so it makes sense on its own, hours or days from now.",
                      required=True),
        "at": param("string", "Local date and time, e.g. '2026-10-31 15:30', or a clock "
                              "time like '3:30 PM' for the next time it comes round."),
        "in_minutes": param("number", "Minutes from now, instead of at."),
        "repeat": param("string", "Make it recur after its first time.",
                        enum=triggers.REPEATS),
        "every_minutes": param("number", "Recur every this many minutes instead."),
    },
    returns="The trigger, with its id and when it will go off.",
)
def set_reminder(note, at=None, in_minutes=None, repeat=None, every_minutes=None):
    return "Set: " + triggers.describe(
        triggers.add_time(note, at, in_minutes, repeat, every_minutes))


@endpoint(
    tier=MAJOR,
    description=(
        "Watches a file or folder on this computer and sends a note to your future "
        "self when it changes — written, created, deleted, or for a folder anything "
        "inside it. You get the note as a new message, with what changed, and act "
        "on it then. The path doesn't have to exist yet: watching for it to appear "
        "works too."
    ),
    params={
        "path": param("string", "Full path of the file or folder.", required=True),
        "note": param("string", "What to tell or ask yourself when it changes.",
                      required=True),
        "once": param("boolean", "Stop watching after the first change; otherwise it "
                                 "keeps going until cancelled.", default=False),
    },
    returns="The trigger, with its id.",
)
def watch_path(path, note, once=False):
    return "Watching: " + triggers.describe(triggers.add_file(path, note, once))


@endpoint(
    tier=MAJOR,
    description="Cancels a reminder or file watch by its id, from list_triggers.",
    params={"trigger_id": param("string", "The id, e.g. 'a3f9c1'.", required=True)},
    returns="The trigger that was cancelled.",
)
def cancel_trigger(trigger_id):
    return "Cancelled: " + triggers.describe(triggers.cancel(trigger_id))
