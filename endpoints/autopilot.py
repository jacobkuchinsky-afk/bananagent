"""Autopilot's endpoints: four for the assistant, and the job's own tools.

The assistant gets start, status, message and stop, and nothing else — the
same reasoning as the coding and browsing agents (every tool in its list costs
it attention on every message; see the README).

Everything else here is scoped to AUTOPILOT, so only a job sees it: the plan,
the task list, the notebook, questions and updates to the user, files, and
the two calls that end a cycle (checkpoint) or the whole job (job_finish).
These are the productivity tools that keep a long job's state out of the
model's context and on disk (workbench/), where every cycle can read it.
"""

import os

import agents
import config
import workbench
from .registry import AUTOPILOT, MAJOR, MINOR, endpoint, param

AGENT = "autopilot"
FILE_READ_CHARS = 8000
FILE_WRITE_CHARS = 200000


def _pilot():
    return agents.get(AGENT)


def _job():
    job = _pilot().job
    if job is None:
        raise RuntimeError("There's no Autopilot job running.")
    return job


# -- for the assistant ------------------------------------------------------------
@endpoint(
    tier=MAJOR, agent=AGENT,
    description=(
        "Starts Autopilot on a long job that it works through on its own, in the "
        "background, for as long as it takes — hours if need be. Use it when the user "
        "hands you something big, open-ended or many-stepped to just get on with: "
        "researching and writing something up, planning something down to the "
        "details, working through a list, building out an idea end to end, or "
        "whenever they say to keep working on something, take it on, or work on it "
        "for a few hours. It plans, keeps a task list and notebook, uses search, "
        "subagents and the coding and browsing agents itself, asks the user questions "
        "on Discord without stopping, and reports back when it's done. A plain coding "
        "job is code_do's; one quick big task you need the answer to now is "
        "subagent_run's. Name an earlier job to pick it up again."
    ),
    params={
        "goal": param("string", "The user's whole request in their own words, with every "
                                "detail, preference and constraint they gave. Don't "
                                "shorten it. When picking up an earlier job: anything new "
                                "they want, or leave out.", required=True),
        "job": param("string", "To carry on an earlier job: its name, from "
                               "autopilot_status. Leave out for a new job."),
        "hours": param("number", "Only if the user said how long to keep at it: the "
                                 "hours to keep working and improving before reporting."),
    },
    returns="The job's name and the folder its files go in.",
)
def autopilot_start(goal, job=None, hours=None):
    pilot = _pilot()
    if pilot.busy():
        raise RuntimeError(f"Autopilot is already on the job '{pilot.job.slug}'. Pass the "
                           f"new request to it with autopilot_message, or stop it first "
                           f"with autopilot_stop.")
    found = None
    if job:
        found = workbench.find(job)
        if found is None:
            names = ", ".join(j.slug for j in workbench.all_jobs()[:12]) or "none"
            raise ValueError(f"No earlier job called '{job}'. Jobs: {names}. Leave job out "
                             f"to start a new one.")
    if found is not None:
        pilot.start(goal=goal, hours=hours, job=found)
    else:
        pilot.start(goal=goal, hours=hours)
    j = pilot.job
    return {"started": True, "job": j.slug, "resumed": found is not None,
            "folder": j.files_dir,
            "note": "It's working in the background and will report back when it's done; "
                    "its questions go to the user on Discord. Check on it with "
                    "autopilot_status."}


@endpoint(
    tier=MINOR, agent=AGENT,
    description=(
        "Reports on Autopilot: the job it's on, its plan and task list, what it's doing "
        "right now, the questions it's waiting on the user for, what its last cycles "
        "did, and the earlier jobs it can pick up again."
    ),
    returns="Status, the job's goal, plan, tasks, open questions and recent journal.",
)
def autopilot_status():
    pilot = _pilot()
    job = pilot.job
    out = {"status": pilot.status, "detail": pilot.detail}
    if job is not None:
        out.update({
            "job": job.slug, "job_status": job.status, "goal": job.goal,
            "cycle": job.cycle, "tool_calls": job.calls,
            "plan": job.plan[:1500] or None,
            "tasks": job.task_lines(show_done=True, done_limit=6)[:40],
            "open_questions": [f"{q['id']}: {q['text']}" for q in job.open_questions()],
            "journal": [job.journal_line(j) for j in job.journal[-3:]],
            "folder": job.files_dir,
            "deadline": workbench.clock(job.deadline) if job.deadline else None,
            "final": job.final,
        })
    out["recent"] = [line["text"] for line in list(pilot.activity)[-8:]]
    out["jobs"] = [f"{j.slug} ({j.status}, {workbench.ago(j.updated_at)})"
                   for j in workbench.all_jobs()[:10]]
    return out


@endpoint(
    tier=MAJOR, agent=AGENT,
    description=(
        "Passes the user's words to Autopilot: the answer to a question it asked "
        "(give the question's id), or new instructions, changes of mind and extra "
        "things to do on its job. It reads them before its next step. Pass along "
        "exactly what the user said."
    ),
    params={
        "text": param("string", "What the user said, in their words.", required=True),
        "question": param("string", "If it answers one of Autopilot's questions: its id, "
                                    "like q2."),
    },
)
def autopilot_message(text, question=None):
    return _pilot().receive(text, question=question or None)


@endpoint(
    tier=MAJOR, agent=AGENT,
    description=(
        "Stops Autopilot. Everything it's done so far — plan, tasks, notes, files — is "
        "kept, and the job can be picked up again later with autopilot_start."
    ),
)
def autopilot_stop():
    pilot = _pilot()
    if not pilot.busy():
        return "Autopilot wasn't running."
    pilot.stop()
    return f"Stopped Autopilot. The job '{pilot.job.slug}' is saved and can be picked up again."


# -- the plan and the task list -------------------------------------------------------
@endpoint(
    tier=MINOR, scope=AUTOPILOT,
    description=(
        "Writes your plan for the job, replacing the old one: the approach, the main "
        "phases, what done looks like, and anything you've decided about how to do it. "
        "Rewrite it whenever you learn something that changes the approach. Every "
        "cycle starts by showing it to you."
    ),
    params={"plan": param("string", "The whole plan, in a few short paragraphs or a list.",
                          required=True)},
)
def plan_write(plan):
    job = _job()
    with job._lock:
        job.plan = str(plan).strip()[:workbench.PLAN_CHARS]
        job.save()
    return "Plan saved."


@endpoint(
    tier=MINOR, scope=AUTOPILOT,
    description=(
        "Adds tasks to your task list, in order. Each should be one concrete thing you "
        "can do and tick off. Give a parent to split a big task into steps under it."
    ),
    params={
        "tasks": param("array", "The tasks, as a JSON list of strings — one or several.",
                       required=True),
        "parent": param("string", "Optional: the id of the task these are steps of, like t3."),
        "notes": param("string", "Optional: a note to put on each of them."),
    },
    returns="The new tasks with their ids.",
)
def todo_add(tasks, parent=None, notes=None):
    made = _job().add_tasks(tasks, parent=parent, notes=notes)
    return {"added": [f"{t['id']} {t['title']}" for t in made]}


@endpoint(
    tier=MINOR, scope=AUTOPILOT,
    description=(
        "Updates a task: mark it doing when you start it (only one at a time), done "
        "when it's finished, blocked when it's waiting on something, dropped if it "
        "turned out not to be needed, or todo to reopen it. Notes are added to the "
        "task — what you found, where the result is, why it's blocked."
    ),
    params={
        "id": param("string", "The task's id, like t4.", required=True),
        "status": param("string", "Its new status.", enum=list(workbench.STATUSES)),
        "notes": param("string", "Optional: a short note to add to it."),
        "title": param("string", "Optional: a better wording for the task."),
    },
)
def todo_update(id, status=None, notes=None, title=None):
    if not (status or notes or title):
        raise ValueError("Give a status, notes or a title to change.")
    job = _job()
    t = job.update_task(id, status=status, notes=notes, title=title)
    out = {"task": job.task_line(t)}
    left = [x for x in job.tasks if x["status"] in ("todo", "doing")]
    if t["status"] == "done":
        out["next_up"] = [job.task_line(x, notes=False) for x in left[:3]] or \
            "Nothing left to do on the list. Look at the goal again: what's missing?"
    return out


@endpoint(
    tier=MINOR, scope=AUTOPILOT,
    description="Shows your whole task list with every task's status and notes.",
    params={"done": param("boolean", "Include finished and dropped tasks.", default=True)},
)
def todo_list(done=True):
    job = _job()
    lines = []
    for t in job.tasks:
        if not done and t["status"] in ("done", "dropped"):
            continue
        lines.append(job.task_line(t, notes=False) +
                     (" — " + " | ".join(t["notes"]) if t["notes"] else ""))
    return {"tasks": lines or ["(no tasks yet)"], "counts": job.counts()}


# -- the notebook -------------------------------------------------------------------
@endpoint(
    tier=MINOR, scope=AUTOPILOT,
    description=(
        "Jots a note in your notebook: a finding, a number, a link, a decision and its "
        "reason, an idea for later, an assumption you made. It's how anything you learn "
        "outlives this conversation — once a cycle ends, only what's in your notebook, "
        "tasks, plan and files is left. Jot as you go, one thing per note."
    ),
    params={
        "note": param("string", "The note — specific enough to be useful cold.",
                      required=True),
        "tag": param("string", "Optional: one word to file it under, like source, "
                               "decision, idea, assumption."),
        "replaces": param("string", "Optional: the id of a note this one corrects, like n7."),
    },
)
def jot(note, tag=None, replaces=None):
    n = _job().jot(note, tag=tag, replaces=replaces)
    return f"Noted as {n['id']}."


@endpoint(
    tier=MINOR, scope=AUTOPILOT,
    description=(
        "Reads your notebook and journal — everything, or just what matches some "
        "words. Use it to look up a note that's too old to be in your brief."
    ),
    params={"search": param("string", "Optional: words to look for.")},
)
def notebook_read(search=None):
    notes, journal = _job().search(search)
    out = {"notes": notes[-80:] or ["(nothing matches)"]}
    if journal:
        out["journal"] = journal[-15:]
    return out


# -- the user -------------------------------------------------------------------------
@endpoint(
    tier=MINOR, scope=AUTOPILOT,
    description=(
        "Asks the user a question on Discord without stopping: you keep working while "
        "they answer, and the answer comes back to you whenever it arrives. Ask when "
        "only they can say — a preference, a decision, a detail you can't find — and "
        "it matters to the result. Give the task that needs it, and that task is "
        "marked blocked until they answer; then carry on with something else. Ask one "
        "thing per question, and don't ask what you can find out or reasonably decide."
    ),
    params={
        "question": param("string", "The question, short and specific, with enough "
                                    "context to answer it cold.", required=True),
        "kind": param("string", "choice (they tap one of your choices), short (a word or "
                                "a line) or long (a few sentences).",
                      enum=["choice", "short", "long"], default="short"),
        "choices": param("array", "For kind choice: 2 to 6 options, as a JSON list of "
                                  "strings."),
        "task": param("string", "Optional: the id of the task that waits on the answer."),
    },
    returns="The question's id, and where it was asked.",
)
def question_ask(question, kind="short", choices=None, task=None):
    from .questions import clean

    question, kind, choices = clean(question, kind, choices)
    job = _job()
    dup = next((q for q in job.open_questions() if q["text"].lower() == question.lower()),
               None)
    if dup:
        return f"You already asked that as {dup['id']}; it's still waiting for an answer."
    if len(job.open_questions()) >= 5:
        raise RuntimeError("You have 5 questions waiting already. Carry on without, "
                           "making a sensible assumption (jot it down), until some are "
                           "answered.")
    q = job.ask(question, kind, choices, task=task)
    where = _pilot().deliver(q)
    return (f"Asked {where} as {q['id']}" + (f"; {q['task']} is blocked until they answer"
                                            if q["task"] else "") +
            ". Don't wait for it: carry on with something that doesn't depend on the "
            "answer. It'll be in front of you when it comes.")


@endpoint(
    tier=MAJOR, scope=AUTOPILOT,
    description=(
        "Texts the user a short progress update on Discord, with a file from this "
        "computer if you like. Only for milestones worth interrupting them for — a "
        "phase done, a first draft ready, something they'd want to know now. Not for "
        "questions (question_ask) or the final report (job_finish)."
    ),
    params={
        "text": param("string", "The update, a sentence or three.", required=True),
        "file": param("string", "Optional: a file to attach — a path in your files "
                                "folder, or a full path."),
    },
)
def update_user(text, file=None):
    path = _path(file) if file else None
    return _pilot().update(str(text).strip()[:1800], path)


# -- files ---------------------------------------------------------------------------
def _path(path, writing=False):
    job = _job()
    root = os.path.realpath(job.files_dir)
    path = str(path or "").strip().strip('"')
    if not path:
        raise ValueError("Give a file path.")
    full = os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
    if writing and os.path.commonpath([full, root]) != root:
        raise ValueError(f"You can only write inside your files folder, {root}. Give a "
                         f"path relative to it.")
    return full


@endpoint(
    tier=MINOR, scope=AUTOPILOT,
    description=(
        "Writes a file in your job's files folder — a report, a draft, a table, notes "
        "too long for the notebook. Use paths relative to the folder (subfolders are "
        "fine). Writes the whole file, or adds to the end of it."
    ),
    params={
        "path": param("string", "Like report.md or research/sources.csv.", required=True),
        "text": param("string", "What to write.", required=True),
        "append": param("boolean", "Add to the end instead of replacing the file.",
                        default=False),
    },
)
def file_write(path, text, append=False):
    full = _path(path, writing=True)
    text = str(text)
    if len(text) > FILE_WRITE_CHARS:
        raise ValueError(f"That's {len(text)} characters; write it in parts with append.")
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "a" if append else "w", encoding="utf-8") as f:
        f.write(text)
    return f"{'Added to' if append else 'Wrote'} {full} ({os.path.getsize(full)} bytes)."


@endpoint(
    tier=MINOR, scope=AUTOPILOT,
    description=(
        "Reads a text file: one in your files folder (a relative path), or anywhere on "
        "this computer (a full path). Long files come a page at a time."
    ),
    params={
        "path": param("string", "Relative to your files folder, or a full path.",
                      required=True),
        "start": param("integer", "Character to start from, for the next page.", default=0),
    },
)
def file_read(path, start=0):
    full = _path(path)
    if not os.path.isfile(full):
        raise FileNotFoundError(f"There's no file at {full}.")
    with open(full, encoding="utf-8", errors="replace") as f:
        text = f.read()
    start = max(0, int(start or 0))
    page = text[start:start + FILE_READ_CHARS]
    out = {"path": full, "text": page, "length": len(text)}
    if start + FILE_READ_CHARS < len(text):
        out["next_start"] = start + FILE_READ_CHARS
    return out


@endpoint(
    tier=MINOR, scope=AUTOPILOT,
    description="Lists the files in your job's files folder, with their sizes.",
)
def files_list():
    root = _job().files_dir
    found = []
    for base, _, names in os.walk(root):
        for n in names:
            full = os.path.join(base, n)
            found.append(f"{os.path.relpath(full, root)} ({os.path.getsize(full)} bytes)")
    return {"folder": root, "files": found or ["(empty)"]}


# -- ending a cycle, ending the job ------------------------------------------------------
@endpoint(
    tier=MINOR, scope=AUTOPILOT, ends_reply=True,
    description=(
        "Ends this cycle of work with a handoff note. The next cycle starts in a fresh "
        "conversation from your saved plan, tasks, notebook and journal — so update "
        "those first. Call it after a solid stretch of work, when you're told your "
        "context is filling up, or when everything left is waiting on the user (then "
        "give wait_minutes, and you'll sleep until they answer or the time's up)."
    ),
    params={
        "done": param("string", "What this cycle got done and found — specific.",
                      required=True),
        "next": param("string", "What the next cycle should do first, and why.",
                      required=True),
        "wait_minutes": param("integer", "Only when there's nothing useful to do until "
                                         "the user answers: how long to wait for them, "
                                         f"up to {config.AUTOPILOT_WAIT_MAX_MINUTES}.",
                              default=0),
    },
)
def checkpoint(done, next, wait_minutes=0):
    pilot = _pilot()
    job = _job()
    wait = max(0, int(wait_minutes or 0))
    if wait and not job.open_questions():
        wait = 0
    job.log_cycle(done, next, pilot._cycle_calls)
    pilot._checkpoint = {"wait": wait}
    pilot.log(f"checkpoint: {str(done)[:120]}")
    return "Checkpoint saved. This cycle is over; the next starts from your saved state."


@endpoint(
    tier=MINOR, scope=AUTOPILOT, ends_reply=True,
    description=(
        "Ends the whole job and sends the user your report. Only when the goal is fully "
        "met and you've checked the result against it — or you're truly blocked. Every "
        "task must be done or dropped first."
    ),
    params={
        "report": param("string", "For the user, in plain sentences: what you did, what you "
                                  "found or made, where the files are, what you assumed, "
                                  "and anything left undone.", required=True),
        "outcome": param("string", "done, partly done, or blocked.", required=True,
                         enum=["done", "partly done", "blocked"]),
        "checked": param("string", "How you checked the result meets the goal, point by "
                                   "point.", required=True),
    },
)
def job_finish(report, outcome, checked):
    import time

    pilot = _pilot()
    job = _job()
    outcome = str(outcome).strip().lower()
    if outcome not in ("done", "partly done", "blocked"):
        raise ValueError("outcome is done, partly done, or blocked.")
    if outcome != "blocked":
        unfinished = [t for t in job.tasks if t["status"] in ("todo", "doing")]
        if unfinished:
            raise ValueError("You still have open tasks: " +
                             "; ".join(job.task_line(t, notes=False) for t in unfinished[:8])
                             + ". Do them, or mark them dropped with a note saying why, "
                               "before you finish.")
    job.finish_attempts += 1
    job.save()
    early = job.deadline and time.time() < job.deadline - 300
    refusals = 2 if early else 1
    if outcome == "done" and job.finish_attempts <= refusals:
        left = (f" The user asked you to keep working until {workbench.clock(job.deadline)}, "
                f"so you have time to make it better." if early else "")
        raise ValueError(
            "Not yet — one more look before you finish." + left + " Read the goal again, "
            "word by word, and compare it with what you've actually made. What's missing, "
            "thin, unchecked or could be clearly better? Is there a detail the user asked "
            "for that isn't there? Add tasks for whatever you find and keep working. If "
            "there's honestly nothing worth adding, call job_finish again.")
    with job._lock:
        job.final = {"report": str(report).strip(), "outcome": outcome,
                     "checked": str(checked).strip(),
                     "unanswered": [q["text"] for q in job.open_questions()]}
        for q in job.open_questions():
            q["status"] = "closed"
        job.save()
    pilot.log(f"finished ({outcome})")
    return "Report saved. The job is over."
