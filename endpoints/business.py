"""Endpoints for the business the user is building.

It's kept apart from long-term memory (see business/): a brief of what the
business is, plus the moving parts — leads being reached out to, tasks, a
dated log of decisions and milestones, and metrics. All of it is shown to the
model with every message, so these mostly write; the reads are for the parts
too long to show every time and for copying exact wording before an edit.
"""

import os

import business
from .registry import MAJOR, MINOR, endpoint, param


# =============================================================================
# MINOR — read only
# =============================================================================

@endpoint(
    tier=MINOR,
    description=(
        "Reads the business brief as written, or one section, to copy exact "
        "wording before business_edit."
    ),
    params={"section": param("string", "One heading, e.g. 'Pricing'; omit for all.")},
    returns="The brief's markdown.",
)
def business_read(section=None):
    return business.section(section) if section else business.read()


@endpoint(
    tier=MINOR,
    description=(
        "Lists business leads in full with every recorded contact, including "
        "customers and lost leads you aren't shown each message."
    ),
    params={
        "stage": param("string", "Only leads at this stage.", enum=business.LEAD_STAGES),
        "search": param("string", "Only leads mentioning this word."),
    },
    returns="One entry per lead, with its history underneath.",
)
def business_leads(stage=None, search=None):
    found = business.leads(stage, search)
    if not found:
        return "No leads match." if (stage or search) else "No leads yet."
    return "\n".join(business.lead_line(l, full=True) for l in found)


@endpoint(
    tier=MINOR,
    description=(
        "Reads the business's full history: the whole log, finished and dropped "
        "tasks, and every past value of each metric. You're only shown the latest."
    ),
    params={
        "search": param("string", "Only entries mentioning this word."),
        "kind": param("string", "Only log entries of this kind.", enum=business.LOG_KINDS),
    },
    returns="The log, the closed tasks and the metric histories.",
)
def business_history(search=None, kind=None):
    data = business.load()
    q = str(search or "").lower()

    def hit(text):
        return not q or q in text.lower()

    log = [business.log_line(e) for e in data["log"]
           if (not kind or e["kind"] == kind) and hit(business.log_line(e))]
    out = ["## Log\n" + ("\n".join(log) or "Nothing.")]
    if not kind:
        closed = [business.task_line(t) for t in data["tasks"]
                  if t.get("state") != "open" and hit(t["task"] + t.get("notes", ""))]
        out.append("## Finished and dropped tasks\n" + ("\n".join(closed) or "None."))
        metrics = [f"- {name}: " + "; ".join(f"{p['value']} on {p['on']}"
                                             + (f" ({p['note']})" if p.get("note") else "")
                                             for p in points)
                   for name, points in data["metrics"].items() if hit(name)]
        out.append("## Metrics over time\n" + ("\n".join(metrics) or "None."))
    return "\n\n".join(out)


# =============================================================================
# MAJOR — these change the business record
# =============================================================================

@endpoint(
    tier=MAJOR,
    description=(
        "Saves one lasting fact about the business to its brief: product, "
        "customers, positioning, market, competitors, price, name, an open "
        "question. Use this, not memory_add, for anything about the business. "
        "People go in business_lead, numbers in business_metric."
    ),
    params={
        "fact": param("string", "One short standalone sentence.", required=True),
        "section": param("string", "Overview, Product, Customers, Positioning, Market, "
                                   "Competitors, Pricing, Name and brand, Open questions."),
    },
)
def business_note(fact, section=None):
    business.add(fact, section)
    return "Saved to the business brief."


@endpoint(
    tier=MAJOR,
    description=(
        "Replaces one exact passage of the business brief, for a fact that changed "
        "or a question now answered. Empty replace deletes it."
    ),
    params={
        "find": param("string", "The exact existing text to change.", required=True),
        "replace": param("string", "What to put in its place; empty to delete.", required=True),
    },
)
def business_edit(find, replace):
    business.replace(find, replace)
    return "Business brief updated." if replace else "Removed from the business brief."


@endpoint(
    tier=MAJOR,
    description=(
        "Adds a dated entry to the business's history: a decision and its reason, "
        "a milestone, a finding, a setback. Not for contacting people: that's "
        "business_lead_touch."
    ),
    params={
        "entry": param("string", "What happened, in a sentence or two.", required=True),
        "kind": param("string", "Sort of entry.", enum=business.LOG_KINDS, default="note"),
        "date": param("string", "YYYY-MM-DD; omit for today."),
    },
)
def business_log(entry, kind="note", date=None):
    item = business.log(entry, kind, date)
    return f"Logged under {item['on']}."


@endpoint(
    tier=MAJOR,
    description=(
        "Saves a person or company the business is reaching out to — prospect, "
        "partner, investor, customer — or updates the one with that name. Call it "
        "for every new person the user mentions contacting or wanting to contact, "
        "then business_lead_touch to record the contact."
    ),
    params={
        "name": param("string", "Person's name, else the company's.", required=True),
        "company": param("string", "Where they work."),
        "role": param("string", "Their job or why they matter."),
        "contact": param("string", "Email, handle or profile URL."),
        "channel": param("string", "Where to reach them: email, X, LinkedIn, Discord."),
        "source": param("string", "Where they were found."),
        "stage": param("string", "Pipeline stage.", enum=business.LEAD_STAGES),
        "follow_up": param("string", "Next follow-up, YYYY-MM-DD; empty clears it."),
        "notes": param("string", "Why they're a fit; replaces old notes."),
    },
)
def business_lead(name, company=None, role=None, contact=None, channel=None, source=None,
                  stage=None, follow_up=None, notes=None):
    lead, created = business.lead_save(name, company=company, role=role, contact=contact,
                                       channel=channel, source=source, stage=stage,
                                       follow_up=follow_up, notes=notes)
    return (f"{'Added' if created else 'Updated'} lead {lead['name']} "
            f"(stage: {lead['stage']}).")


@endpoint(
    tier=MAJOR,
    description=(
        "Records one contact with a saved lead — message sent, reply, call, demo — "
        "and sets their stage and next follow-up. Call it whenever the user reaches "
        "out to someone or hears back."
    ),
    params={
        "name": param("string", "The lead's name as saved.", required=True),
        "what": param("string", "What happened, e.g. 'DMed on X about RSI'.",
                      required=True),
        "stage": param("string", "Stage after this; 'new' becomes 'contacted' if omitted.",
                       enum=business.LEAD_STAGES),
        "follow_up": param("string", "Next follow-up, YYYY-MM-DD; empty clears it."),
    },
)
def business_lead_touch(name, what, stage=None, follow_up=None):
    lead = business.lead_touch(name, what, stage, follow_up)
    msg = f"Recorded. {lead['name']} is now at '{lead['stage']}'"
    if lead.get("follow_up"):
        msg += f", follow up {business.when(lead['follow_up'])}"
    return msg + "."


@endpoint(
    tier=MAJOR,
    description=(
        "Adds a to-do to the business's task list: a next step to build, write, "
        "research, send or decide."
    ),
    params={
        "task": param("string", "A short imperative.", required=True),
        "due": param("string", "YYYY-MM-DD."),
        "priority": param("string", "Priority.", enum=business.PRIORITIES, default="medium"),
        "notes": param("string", "Detail needed to do it."),
    },
)
def business_task_add(task, due=None, priority="medium", notes=None):
    item = business.task_add(task, due, priority, notes)
    return "Task added" + (f", due {business.when(item['due'])}." if item.get("due") else ".")


@endpoint(
    tier=MAJOR,
    description=(
        "Marks a business task done or dropped, reopens it, or changes its due "
        "date, priority, notes or wording."
    ),
    params={
        "task": param("string", "Enough of its wording to pick it out.", required=True),
        "state": param("string", "New state.", enum=business.TASK_STATES),
        "due": param("string", "YYYY-MM-DD; empty clears it."),
        "priority": param("string", "New priority.", enum=business.PRIORITIES),
        "notes": param("string", "Replaces old notes."),
        "rename": param("string", "New wording."),
    },
)
def business_task_update(task, state=None, due=None, priority=None, notes=None, rename=None):
    item = business.task_update(task, state, due, priority, notes, rename)
    return f"Task is {item['state']}: {item['task']}."


@endpoint(
    tier=MAJOR,
    description=(
        "Records today's value of a business number: MRR, users, waitlist "
        "signups, reply rate, spend. Old values are kept."
    ),
    params={
        "name": param("string", "Same name each time, e.g. 'MRR'.", required=True),
        "value": param("string", "With units, e.g. '$120' or '34'.", required=True),
        "note": param("string", "What's behind it."),
    },
)
def business_metric(name, value, note=None):
    key, points = business.metric(name, value, note)
    if len(points) > 1:
        return f"{key} is now {points[-1]['value']} (was {points[-2]['value']})."
    return f"Started tracking {key}: {points[-1]['value']}."


@endpoint(
    tier=MAJOR,
    description=(
        "Opens the business brief in the text editor for the user."
    ),
)
def business_open_file():
    business.read()  # makes sure it exists
    os.startfile(business.BRIEF)
    return "Opened the business brief."
