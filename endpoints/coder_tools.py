"""The coding agent's tools: talking to Claude Code, the sandbox, and the job.

All scope=CODER, so none of them appear in the assistant's own tool list —
the assistant starts the coding agent with code_do (endpoints/coding.py) and
these are what the coding agent then works with. They act on the project the
current task is in; nothing here takes a folder outside the sandbox.

    Claude     claude_send, claude_wait, claude_sessions, claude_history,
               claude_stop, claude_allow
    sandbox    sandbox_list, sandbox_read, sandbox_search, sandbox_write,
               sandbox_edit, sandbox_run, sandbox_projects
    the job    coding_plan, ask_user, coding_finish

Claude does the coding. The sandbox file tools exist so the agent can check
what Claude says it did, and fix something trivial itself when a round trip
to Claude isn't worth it.
"""

import time

import agents
import claude_code
import config
import workspace
from .registry import CODER, MAJOR, MINOR, endpoint, param

AGENT = "coder"


def _agent():
    return agents.get(AGENT)


def _project():
    a = _agent()
    if not a.project:
        raise RuntimeError("There's no coding task running.")
    return a.project


def _session(name, create=True):
    from agents.coder import on_claude

    s = claude_code.session(_project(), name, create=create)
    s.on_activity = on_claude
    return s


def _stopping():
    return _agent()._stop.is_set()


def _turn_report(s, turn):
    """A finished turn as the coding agent reads it."""
    reply = turn["reply"]
    if len(reply) > config.CLAUDE_REPLY_CHARS:
        # The recap is at the end, and it's the part that matters most.
        keep = config.CLAUDE_REPLY_CHARS
        reply = reply[:keep // 3] + "\n… (middle cut) …\n" + reply[-(keep - keep // 3):]
    out = {"session": s.name, "status": "error" if turn["is_error"] else "replied",
           "reply": reply, "files_changed": turn["files"],
           "seconds": turn["seconds"], "cost_usd": turn["cost_usd"]}
    if turn["stopped"]:
        out["status"] = "stopped before it finished"
    if turn["blocked"]:
        out["refused_permission"] = turn["blocked"]
        out["about_refusals"] = ("Claude wasn't allowed to do these. Tell it to find another "
                                 "way, run the command yourself with sandbox_run, or ask the "
                                 "user to allow it with claude_allow.")
    if turn.get("hint"):
        out["hint"] = turn["hint"]
    if not any(turn["files"].values()) and not turn["is_error"]:
        out["note"] = "No files in the project changed during this reply."
    return out


def _still_working(s):
    return {"session": s.name, "status": "still working",
            "working_for_seconds": round(time.time() - s.current["started"], 1),
            "recent_activity": list(s.activity)[-8:],
            "next": "Call claude_wait to keep waiting (or do something else first), "
                    "or claude_stop to interrupt it."}


def _await(s, seconds):
    try:
        turn = s.wait(seconds, should_stop=_stopping)
    except InterruptedError:
        raise RuntimeError("The task was stopped.")
    return _still_working(s) if turn is None else _turn_report(s, turn)


_SESSION = param("string", "Which Claude session. 'main' unless you deliberately want "
                           "another one (e.g. 'reviewer').", default="main")


# =============================================================================
# Claude Code
# =============================================================================

@endpoint(
    tier=MAJOR, scope=CODER, result_chars=config.CLAUDE_REPLY_CHARS + 2500,
    description=(
        "Sends a message to Claude Code, the expert programmer working in this project's "
        "folder, and waits for its reply. Claude reads, writes and runs code itself and "
        "remembers everything earlier in the same session. The first message to a "
        "session must be a complete brief; later ones can be short. Claude ends each "
        "reply with a RECAP (Done / Files / Run it / Verified / Not done / Questions). "
        "Waits up to about ten minutes; if Claude is still going, you get its recent "
        "activity and should claude_wait."
    ),
    params={
        "message": param("string", "What you want Claude to do or know — see your "
                                   "prompt for how to write it.", required=True),
        "session": _SESSION,
        "fresh": param("boolean", "true to wipe this session's memory and start it over. "
                                  "Rarely needed.", default=False),
        "wait": param("boolean", "false to return at once and let Claude work while you "
                                 "do something else; collect the reply with claude_wait.",
                      default=True),
    },
    returns=("Claude's reply, the files that actually changed on disk, anything Claude "
             "was refused permission to do, time and cost — or 'still working'."),
)
def claude_send(message, session="main", fresh=False, wait=True):
    s = _session(session)
    s.send(message, fresh=bool(fresh))
    if not wait:
        return {"session": s.name, "status": "working",
                "next": "Claude is working. Call claude_wait for its reply."}
    return _await(s, config.CLAUDE_WAIT_SECONDS)


@endpoint(
    tier=MINOR, scope=CODER, result_chars=config.CLAUDE_REPLY_CHARS + 2500,
    description=(
        "Waits for a Claude session that is still working and returns its reply once it "
        "has one. If it has already replied, returns that reply straight away."
    ),
    params={"session": _SESSION,
            "seconds": param("integer", "How long to wait at most, up to 900.", default=600)},
    returns="Claude's reply and what changed — or 'still working' with its recent activity.",
)
def claude_wait(session="main", seconds=600):
    s = _session(session, create=False)
    if not s.busy():
        if s.last is None:
            return {"session": s.name, "status": "idle",
                    "note": "It hasn't been sent anything yet. Use claude_send."}
        return _turn_report(s, s.last)
    return _await(s, max(10, min(int(seconds or 600), 900)))


@endpoint(
    tier=MINOR, scope=CODER,
    description="Lists this project's Claude sessions: working or idle, how many messages, "
                "cost so far, and the start of each one's last reply.",
    returns="One entry per session.",
)
def claude_sessions():
    found = claude_code.sessions(_project())
    return [s.summary() for s in found] or "No Claude sessions yet. Start one with claude_send."


@endpoint(
    tier=MINOR, scope=CODER, result_chars=12000,
    description=(
        "Re-reads earlier messages with a Claude session: what you sent and what it "
        "replied. Use it when you need something from an earlier reply that was "
        "shortened out of your conversation."
    ),
    params={"session": _SESSION,
            "count": param("integer", "How many of the most recent exchanges, 1 to 5.",
                           default=2)},
    returns="The exchanges, oldest first.",
)
def claude_history(session="main", count=2):
    s = _session(session, create=False)
    count = max(1, min(int(count or 2), 5))
    budget = 11000 // count
    out = []
    for t in s.turns[-count:]:
        out.append({"you_sent": t["sent"][:budget // 3], "claude_replied": t["reply"][-budget:],
                    "files_changed": t.get("files")})
    return out or "Nothing has been sent to that session yet."


@endpoint(
    tier=MAJOR, scope=CODER,
    description="Interrupts a Claude session part way through a reply. What it already "
                "changed on disk stays changed; the session keeps its memory.",
    params={"session": _SESSION},
)
def claude_stop(session="main"):
    s = _session(session, create=False)
    if not s.busy():
        return f"Session '{s.name}' wasn't working on anything."
    s.stop()
    s.wait(15)
    return f"Stopped session '{s.name}'."


@endpoint(
    tier=MAJOR, scope=CODER,
    description=(
        "Asks the user to let Claude use a tool or command it was refused, for the rest "
        "of this project — e.g. Bash(cargo *) or WebFetch. The user has to say yes; "
        "only ask when the work genuinely needs it."
    ),
    params={
        "rule": param("string", "A Claude Code permission rule: a tool name like WebFetch, "
                                "or Bash(<command prefix> *).", required=True),
        "why": param("string", "One sentence the user will read: why Claude needs it.",
                     required=True),
    },
)
def claude_allow(rule, why):
    rule = str(rule).strip()
    if not rule:
        raise ValueError("Give the rule, e.g. Bash(cargo *).")
    if not _agent().approve("permission", f"Let Claude use {rule} in {_project()}?", str(why)):
        return (f"The user did not allow {rule}. Have Claude do it another way, or leave "
                f"it out and say so in your report.")
    claude_code.allow(_project(), rule)
    return f"Allowed {rule} for this project, from Claude's next message on."


# =============================================================================
# The sandbox
# =============================================================================

@endpoint(
    tier=MINOR, scope=CODER,
    description="Lists the files in the project folder (or a folder inside it), with "
                "sizes. Dependency and cache folders are left out.",
    params={"path": param("string", "A folder inside the project; '.' for all of it.",
                          default="."),
            "depth": param("integer", "How many levels of folders to go down.", default=3)},
    returns="An indented listing.",
)
def sandbox_list(path=".", depth=3):
    return workspace.tree(_project(), path, depth=max(1, min(int(depth or 3), 8)))


@endpoint(
    tier=MINOR, scope=CODER, result_chars=14000,
    description="Reads a file in the project, with line numbers.",
    params={"path": param("string", "The file, relative to the project folder.",
                          required=True),
            "start": param("integer", "First line to show.", default=1),
            "lines": param("integer", "How many lines, up to 400.", default=250)},
    returns="The lines, numbered, and how long the file is.",
)
def sandbox_read(path, start=1, lines=250):
    return workspace.read(_project(), path, start, max(1, min(int(lines or 250), 400)))


@endpoint(
    tier=MINOR, scope=CODER,
    description="Searches the project's files for a regular expression (or plain text).",
    params={"pattern": param("string", "What to look for.", required=True),
            "path": param("string", "Folder or file to search in.", default="."),
            "glob": param("string", "Only files whose name matches, e.g. *.py.")},
    returns="Matching lines as file:line: text.",
)
def sandbox_search(pattern, path=".", glob=None):
    return workspace.search(_project(), pattern, path, glob)


@endpoint(
    tier=MAJOR, scope=CODER,
    description=(
        "Writes a whole file in the project, replacing it if it exists. For the rare "
        "small job not worth a message to Claude — a config value, a notes file. Real "
        "code changes go through claude_send."
    ),
    params={"path": param("string", "The file, relative to the project folder.",
                          required=True),
            "text": param("string", "The complete contents.", required=True)},
)
def sandbox_write(path, text):
    return workspace.write(_project(), path, text)


@endpoint(
    tier=MAJOR, scope=CODER,
    description=(
        "Replaces one exact passage in a project file — for a one-line fix, a typo, a "
        "setting. Read the file first and copy the text exactly; it must match once. "
        "Anything bigger goes through claude_send."
    ),
    params={"path": param("string", "The file, relative to the project folder.",
                          required=True),
            "find": param("string", "The exact text to replace.", required=True),
            "replace": param("string", "What to put instead; empty deletes it.",
                             required=True)},
)
def sandbox_edit(path, find, replace):
    return workspace.edit(_project(), path, find, replace)


@endpoint(
    tier=MAJOR, scope=CODER, result_chars=config.SANDBOX_OUTPUT_CHARS + 500,
    description=(
        "Runs one PowerShell command in the project folder and returns its output and "
        "exit code — to check Claude's work yourself: run the tests, run the program, "
        "confirm a file does what the recap says. Anything that reaches outside the "
        "project, deletes folders or changes the system asks the user first. Programs "
        "that don't finish on their own (servers, games) are killed at the timeout."
    ),
    params={"command": param("string", "The PowerShell command.", required=True),
            "timeout": param("integer", "Seconds before it's killed, up to 600.",
                             default=120)},
    returns="exit_code, output (start and end kept if long), seconds.",
)
def sandbox_run(command, timeout=120):
    project = _project()
    why = workspace.risky(project, command)
    if why:
        if not _agent().approve("command", f"Run this in {project}? It {why}.", str(command)):
            return {"refused": f"The user didn't approve it ({why}). Do it another way."}
    return workspace.run(project, command, timeout)


@endpoint(
    tier=MINOR, scope=CODER,
    description="Lists every project in the sandbox, newest first — useful when the task "
                "refers to earlier work.",
    returns="Name, file count and last change for each.",
)
def sandbox_projects():
    return workspace.projects() or "The sandbox is empty."


# =============================================================================
# The job
# =============================================================================

@endpoint(
    tier=MINOR, scope=CODER,
    description=(
        "Writes down your plan: the goal, how you'll know it's done, and a checklist of "
        "steps marked [x] when done. Replaces the previous plan. It's shown on the "
        "user's dashboard and given back to you if your conversation is shortened, so "
        "update it as you go."
    ),
    params={"plan": param("string", "The whole plan, as short plain lines.", required=True)},
)
def coding_plan(plan):
    a = _agent()
    a.plan = str(plan).strip()[:4000]
    a.log("plan updated")
    return "Plan saved."


@endpoint(
    tier=MINOR, scope=CODER,
    description=(
        "Asks the user a question and waits for their answer. Only for decisions you "
        "can't sensibly make yourself and that would waste the work if guessed wrong. "
        "If they don't answer in time you're told to use your judgment."
    ),
    params={"question": param("string", "One short, specific question — with the options "
                                        "if there are any.", required=True)},
    returns="Their answer.",
)
def ask_user(question):
    return _agent().question(str(question).strip())


@endpoint(
    tier=MINOR, scope=CODER,
    description=(
        "Ends the job and hands your report to the user. Only once the request is met "
        "and you've checked it — or you've hit something you can't get past."
    ),
    params={
        "summary": param("string", "The report, in plain sentences for the user: what was "
                                   "built or changed, where it is, how to run it, what you "
                                   "checked, and anything left undone.", required=True),
        "outcome": param("string", "done, partly done, or blocked.", required=True,
                         enum=["done", "partly done", "blocked"]),
        "live_url": param("string", "If the app is online: its production URL, which you've "
                                    "opened and seen work. Remembered for later edits."),
    },
)
def coding_finish(summary, outcome, live_url=None):
    a = _agent()
    if live_url:
        workspace.record_deployment(a.project, live_url)
    a.final = {"summary": str(summary).strip(), "outcome": str(outcome)}
    if live_url:
        a.final["live_url"] = str(live_url).strip()
    a.log(f"finished ({outcome})")
    return "Reported. The job is over."
