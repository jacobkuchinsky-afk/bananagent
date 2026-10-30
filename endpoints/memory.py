"""Endpoints for the assistant's memory.

Long-term memory is memory/MEMORY.md, sent with every message and kept
forever; these read it, add to it, fix it, and open it for editing by hand.
Short-term memory is the sliding window of recent conversation; the only thing
to do to it by hand is start over.
"""

import os
import re

import business
import memory
from .registry import MAJOR, MINOR, app, endpoint, on_ui, param


# =============================================================================
# MINOR — read only
# =============================================================================

@endpoint(
    tier=MINOR,
    description=(
        "Reads your long-term memory file exactly as written, with its headings. "
        "You already see its contents in every conversation; read it before "
        "editing so you can copy exact wording."
    ),
    returns="The memory file's text.",
)
def memory_read():
    return memory.read()


# =============================================================================
# MAJOR — these change what the assistant remembers
# =============================================================================

@endpoint(
    tier=MAJOR,
    description=(
        "Saves one fact to long-term memory, where it's kept forever and shown to "
        "you in every future conversation. Use when the user tells you something "
        "worth knowing later — their name, a preference, a person, a project, a "
        "decision — or asks you to remember something. One short fact per call. "
        "Never for the business: use the business tools."
    ),
    params={
        "fact": param("string", "The fact, as one short standalone sentence.", required=True),
        "section": param("string", "Which heading to put it under, e.g. 'About the user', "
                                   "'Preferences', 'People', 'Projects'. Made if missing."),
    },
)
def memory_add(fact, section=None):
    # The model's habit from before the business had a record of its own is to
    # file business facts here. One naming the business is turned away.
    name = business.name()
    if name != "the business" and re.search(rf"\b{re.escape(name)}\b", str(fact), re.I):
        raise ValueError(f"That's about {name}, which has its own record. Save it with "
                         f"the business tools instead: business_note, business_lead, "
                         f"business_lead_touch, business_log, business_task_add or "
                         f"business_metric.")
    memory.add(fact, section)
    return "Saved to long-term memory."


@endpoint(
    tier=MAJOR,
    description=(
        "Changes or removes one passage of long-term memory: finds the exact text "
        "given and replaces it. Use when a saved fact is out of date or wrong, or "
        "the user asks you to forget something — replace it with an empty string "
        "to delete it. The text must appear exactly once; read the memory first."
    ),
    params={
        "find": param("string", "The exact existing text to change.", required=True),
        "replace": param("string", "What to put in its place; empty to delete.", required=True),
    },
)
def memory_edit(find, replace):
    memory.replace(find, replace)
    return "Long-term memory updated." if replace else "Removed from long-term memory."


@endpoint(
    tier=MAJOR,
    description=(
        "Rewrites the whole long-term memory file. Only for reorganising or "
        "tidying it when asked; to add or change one fact use memory_add or "
        "memory_edit. Keep every fact that still matters."
    ),
    params={"text": param("string", "The complete new memory file, in markdown.", required=True)},
)
def memory_write(text):
    memory.write(text)
    return "Long-term memory rewritten."


@endpoint(
    tier=MAJOR,
    description=(
        "Opens the long-term memory file in the computer's text editor so the "
        "user can read or change it by hand."
    ),
)
def memory_open_file():
    memory.read()  # makes sure it exists
    os.startfile(memory.LONG_TERM)
    return "Opened the memory file."


@endpoint(
    tier=MAJOR,
    description=(
        "Clears the recent conversation — the short-term memory — so the next "
        "message starts fresh. Long-term memory is kept. Only when the user asks "
        "to start over or forget this conversation."
    ),
)
def conversation_clear():
    memory.clear_conversation()

    def trim():
        # The exchange asking for this is still being answered; keep it on
        # screen so the reply has somewhere to go.
        chat = app().chat
        chat[:] = chat[-2:] if chat and not chat[-1].get("done") else []

    on_ui(trim)
    return "The recent conversation is cleared."
