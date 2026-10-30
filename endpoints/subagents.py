"""The endpoint that hands a job to a subagent (see subagents/)."""

import config
import subagents
from .registry import MINOR, endpoint, param


# MINOR while every kind's tools are read-only: a run can't change anything.
# Make it MAJOR when a kind gets tools that do (writing files, say).
@endpoint(
    tier=MINOR,
    description=(
        "Hands a big job to a subagent: a separate run of the model with its own "
        "memory that does the whole task and reports back. Use for work that would "
        "take a lot of text or many steps — writing something long, summarising "
        "or reworking a lot of material, several look-ups in a row. It can't see "
        "this conversation or your memory, so put everything it needs in the task. "
        "Kinds: " + "; ".join(f"{k.name} — {k.description}" for k in subagents.KINDS.values())
    ),
    params={
        "task": param("string", "The complete job, with all the context the subagent "
                                "needs, as if briefing someone who knows nothing.",
                      required=True),
        "kind": param("string", "Which kind of subagent.", enum=list(subagents.KINDS),
                      default="worker"),
    },
    returns="The subagent's result, how many tools it called, and how long it took.",
    result_chars=config.SUBAGENT_RESULT_CHARS,
)
def subagent_run(task, kind="worker"):
    return subagents.run(task, kind)
