"""Endpoints for the coding agent — what the assistant sees of it.

Four, on the same reasoning as the browsing agent's (see browser_agent.py and
the README): every tool in the assistant's list costs it attention on every
message, so start, check, talk and stop are all there is. The dozen-plus tools
the coding agent works with (endpoints/coder_tools.py) are scoped to it and
never appear here.

code_message does double duty: it answers the question the agent is waiting
on, approves or refuses a step it asked about, or — if it isn't waiting —
passes the user's words on at its next step.
"""

import agents
import claude_code
import workspace
from .registry import MAJOR, MINOR, endpoint, param

AGENT = "coder"


def _coder():
    return agents.get(AGENT)


@endpoint(
    tier=MAJOR,
    agent=AGENT,
    description=(
        "Starts the coding agent on a programming job: building an app, script, game or "
        "website, or changing or fixing code in an earlier project — and putting web apps "
        "online with a database when asked, so the user gets a link. It works in its "
        "sandbox folder with Claude Code, in the background, going back and forth until "
        "the job is done, and can take many minutes. Use this for any request to write "
        "or change code rather than writing code yourself — including making a new tool "
        "for yourself, which it builds and then adds to your tool list."
    ),
    params={
        "task": param("string", "The user's whole request in their own words, with every "
                                "detail and preference they gave. Don't shorten it.",
                      required=True),
        "project": param("string", "A short folder name for the project, like "
                                   "'snake-game'. Reuse an existing name to keep working on "
                                   "earlier work; leave out to name it from the task."),
    },
    returns="The project it's working in and where the folder is.",
)
def code_do(task, project=None):
    coder = _coder()
    coder.start(task, project)
    return {"started": True, "project": coder.project,
            "folder": workspace.project_dir(coder.project),
            "note": "It's working in the background. Check on it with code_status."}


@endpoint(
    tier=MINOR,
    agent=AGENT,
    description=(
        "Reports on the coding agent: what it's working on, its plan, what Claude is "
        "doing right now, whether it's waiting for the user, and its final report when "
        "it's done. Also lists the projects in the sandbox."
    ),
    returns="Status, project, plan, recent steps, any question, and the result.",
)
def code_status():
    coder = _coder()
    state = coder.snapshot()
    state["recent"] = [line["text"] for line in coder.activity][-10:]
    state["projects"] = [f"{p['name']} ({p['url']})" if p.get("url") else p["name"]
                         for p in workspace.projects()][:15]
    if not claude_code.auth_status()["ok"]:
        state["claude_problem"] = claude_code.auth_status()["problem"]
    return state


@endpoint(
    tier=MAJOR,
    agent=AGENT,
    description=(
        "Passes the user's words to the coding agent while it works: the answer to a "
        "question it asked, a yes or no to a step it's waiting to be approved, or new "
        "instructions and changes of mind. Pass along exactly what the user said."
    ),
    params={
        "text": param("string", "What the user said, in their words.", required=True),
        "decision": param("string", "Only when it's waiting for approval of a step: "
                                    "approve or deny.", enum=["approve", "deny"]),
    },
)
def code_message(text, decision=None):
    coder = _coder()
    if decision and coder.pending is not None and coder.pending.kind != "question":
        choice = str(decision).strip().lower()
        if choice not in ("approve", "deny"):
            raise ValueError("decision is approve or deny.")
        question = coder.answer(choice == "approve")
        return f"{'Approved' if choice == 'approve' else 'Refused'}: {question}"
    coder.tell(text)
    if coder.pending is not None and coder.pending.kind == "question":
        return "Passed on as the answer to its question."
    return "Passed on; it'll read it at its next step."


@endpoint(
    tier=MAJOR,
    agent=AGENT,
    description=(
        "Stops the coding agent and any Claude session it has running. Files already "
        "written stay in the project folder."
    ),
)
def code_stop():
    _coder().stop()
    return "Stopped the coding agent."
