"""Endpoints for the assistant's own prompts: main, personality and tools.

One set of endpoints with a `part` argument rather than a set per file. Every
extra tool is more text the model has to wade through, and past a certain
amount of it the old model started inventing tool names instead of calling
real ones; five tools that take a part cost far less than fifteen that don't.

Edits land in prompts/*.md and apply from the next message. Each one saves the
previous version to prompts/history/ first.
"""

import prompts
from .registry import MAJOR, MINOR, endpoint, param

_PARTS = list(prompts.PARTS)
_PART = param("string", "Which prompt: main (what you are and how you do the job), "
                        "personality (how you sound), or tools (how you use tools).",
              required=True, enum=_PARTS)


# =============================================================================
# MINOR — read only
# =============================================================================

@endpoint(
    tier=MINOR,
    description=(
        "Reads one of your own prompts exactly as written, including any notes "
        "in <!-- --> markers, or 'assembled' for the complete system prompt as "
        "you actually receive it. Read a part before editing it."
    ),
    params={"part": param("string", "main, personality, tools, or assembled.",
                          required=True, enum=_PARTS + ["assembled"])},
    returns="The prompt text.",
)
def prompt_read(part):
    part = str(part).strip().lower()
    if part == "assembled":
        import brain

        return brain.system_message()
    return prompts.read(part)


# =============================================================================
# MAJOR — these change how the assistant behaves
# =============================================================================

@endpoint(
    tier=MAJOR,
    description=(
        "Replaces one of your prompts entirely with new text. Use for a rewrite; "
        "for a small change use prompt_edit, and to add something use "
        "prompt_append. Takes effect from the next message."
    ),
    params={"part": _PART,
            "text": param("string", "The complete new prompt text.", required=True)},
)
def prompt_write(part, text):
    prompts.write(str(part).strip().lower(), text)
    return f"The {part} prompt has been rewritten."


@endpoint(
    tier=MAJOR,
    description=(
        "Adds a paragraph to the end of one of your prompts, leaving the rest as "
        "it is. Use when the user wants you to start doing something new, such "
        "as 'always answer in metric' or 'be more sarcastic'."
    ),
    params={"part": _PART,
            "text": param("string", "The paragraph to add.", required=True)},
)
def prompt_append(part, text):
    prompts.append(str(part).strip().lower(), text)
    return f"Added to the {part} prompt."


@endpoint(
    tier=MAJOR,
    description=(
        "Changes one passage of a prompt: finds the exact text given and "
        "replaces it. The text must appear exactly once, so read the prompt "
        "first and copy the wording. To delete a passage, replace it with an "
        "empty string."
    ),
    params={"part": _PART,
            "find": param("string", "The exact existing text to change.", required=True),
            "replace": param("string", "What to put in its place.", required=True)},
)
def prompt_edit(part, find, replace):
    prompts.replace(str(part).strip().lower(), find, replace)
    return f"The {part} prompt has been edited."


@endpoint(
    tier=MAJOR,
    description=(
        "Resets one of your prompts to its original default text, discarding "
        "every change made to it. The old version is kept in the history folder."
    ),
    params={"part": _PART},
)
def prompt_reset(part):
    prompts.reset(str(part).strip().lower())
    return f"The {part} prompt is back to its default."
