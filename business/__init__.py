"""The business: everything BananaGent keeps about the company the user is
building, apart from its general long-term memory.

Long-term memory (memory/) is facts about the user and their life, one line
each. The business needs more than that — a pipeline of people to reach, a
to-do list, a dated record of what was decided and why, numbers that change
over time — so it lives here, in two files of its own:

    business/BUSINESS.md   the brief: what the business is, its product,
                           market, competitors, pricing, name, open questions.
                           Plain markdown under headings, the title line is the
                           business's name. Edit it by hand any time.
    business/data.json     the moving parts: leads (outreach), tasks, the log
                           (decisions, milestones, learnings) and metrics.

Both are sent to the model with every message, after long-term memory (see
render() and brain.system_message), and kept forever. Every write through this
module first copies the old file into business/history/, keeping the last
HISTORY_KEEP of each. Both hold business plans and contacts and are gitignored.
"""

import datetime
import json
import os
import re
import shutil
import threading
import time

import config

HERE = os.path.dirname(os.path.abspath(__file__))
BRIEF = os.path.join(HERE, "BUSINESS.md")
DATA = os.path.join(HERE, "data.json")
HISTORY = os.path.join(HERE, "history")
HISTORY_KEEP = 200  # per file; lead and task updates are frequent

# Where a lead is in the pipeline, in order. "lost" is anyone who said no or
# went quiet for good; they're kept, but not shown with every message.
LEAD_STAGES = ("new", "contacted", "replied", "meeting", "trial", "customer", "lost")
CLOSED_STAGES = ("customer", "lost")
TASK_STATES = ("open", "done", "dropped")
PRIORITIES = ("high", "medium", "low")
LOG_KINDS = ("decision", "milestone", "learning", "setback", "note")

# What render() puts in front of the model with every message. The rest is a
# business_leads or business_history call away.
SHOWN_LEADS = 25
SHOWN_LOG = 12
SHOWN_DONE = 5

_TEMPLATE = """<!--
The business BananaGent is helping build. Everything below is sent to the model
with every message, after long-term memory, and kept forever. The title line is
the business's name. Plain markdown, one fact per line under a heading; the
model keeps it current with its business tools. Leads, tasks, the log and
metrics live in data.json. Notes inside these markers never reach the model.
-->

# My business

## Overview

## Product

## Customers

## Positioning

## Market

## Competitors

## Pricing

## Name and brand

## Open questions
"""

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_lock = threading.RLock()


# =============================================================================
# The brief: BUSINESS.md
# =============================================================================

def read():
    """BUSINESS.md exactly as written, notes and all."""
    with _lock:
        if not os.path.exists(BRIEF):
            with open(BRIEF, "w", encoding="utf-8") as f:
                f.write(_TEMPLATE)
        with open(BRIEF, encoding="utf-8") as f:
            return f.read()


def name():
    """The business's name: the brief's title line."""
    m = re.search(r"(?m)^#[ \t]+(.+?)[ \t]*$", _COMMENT.sub("", read()))
    return m.group(1) if m else "the business"


def section(heading):
    """One section of the brief, heading included, or raise if there's none."""
    text = read()
    m = _heading(heading).search(text)
    if not m:
        have = re.findall(r"(?m)^##[ \t]+(.+?)[ \t]*$", _COMMENT.sub("", text))
        raise ValueError(f"No section called '{heading}'. Sections: {', '.join(have)}")
    nxt = re.compile(r"(?m)^#{1,2}\s").search(text, m.end())
    return text[m.start():nxt.start() if nxt else len(text)].strip()


def add(fact, heading=None):
    """Add one line under `heading` (made if missing), or under Notes."""
    fact = " ".join(str(fact).split())
    if not fact:
        raise ValueError("There's nothing to save in an empty note.")
    line = fact if fact.startswith(("- ", "* ")) else f"- {fact}"
    heading = str(heading or "").strip().lstrip("#").strip() or "Notes"
    with _lock:
        text = read()
        m = _heading(heading).search(text)
        if not m:
            return write(f"{text.rstrip()}\n\n## {heading}\n{line}\n")
        nxt = re.compile(r"(?m)^#{1,6}\s").search(text, m.end())
        end = nxt.start() if nxt else len(text)
        body = text[m.end():end].strip("\n")
        block = f"{body}\n{line}" if body.strip() else line
        return write(text[:m.end()] + "\n" + block + ("\n\n" + text[end:] if nxt else "\n"))


def replace(find, new):
    """Swap one exact passage of the brief; an empty replacement deletes it."""
    find = str(find)
    with _lock:
        text = read()
        count = text.count(find) if find else 0
        if count == 0:
            raise ValueError("That text isn't in the business brief. Read it first "
                             "and copy the exact wording.")
        if count > 1:
            raise ValueError(f"That text appears {count} times; include more around it "
                             f"so it matches only once.")
        text = text.replace(find, str(new), 1)
        text = re.sub(r"(?m)^[ \t]*[-*][ \t]*\n", "", text)  # a bullet with nothing left
        return write(text)


def write(text):
    """Replace BUSINESS.md outright. The old version goes to history first."""
    text = str(text).replace("\r\n", "\n").strip() + "\n"
    if len(_COMMENT.sub("", text)) > config.BUSINESS_MAX_CHARS:
        raise ValueError(f"The business brief would be over {config.BUSINESS_MAX_CHARS} "
                         f"characters. Tighten it first: merge lines, cut what no "
                         f"longer matters, and move dated detail into the log.")
    with _lock:
        _backup(BRIEF)
        with open(BRIEF, "w", encoding="utf-8") as f:
            f.write(text)
    print(f"[business] BUSINESS.md saved ({len(text)} chars)")
    return text


def _heading(heading):
    return re.compile(rf"(?mi)^#{{1,6}}[ \t]+{re.escape(heading.strip())}[ \t]*$")


# =============================================================================
# The moving parts: data.json
# =============================================================================

def _empty():
    return {"leads": [], "tasks": [], "log": [], "metrics": {}}


def load():
    """data.json as a dict, with every list present."""
    with _lock:
        try:
            with open(DATA, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return _empty()
        except Exception as e:
            # Never silently start over on the business's records: keep the
            # damaged file for a look and refuse until it's fixed.
            raise RuntimeError(f"business/data.json can't be read ({e}); fix it by "
                               f"hand or restore it from business/history/") from e
        for key, value in _empty().items():
            data.setdefault(key, value)
        return data


def save(data):
    with _lock:
        _backup(DATA)
        tmp = DATA + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, DATA)  # never leave a half-written file behind


def today():
    return datetime.date.today().isoformat()


def as_date(value, what="date"):
    """A date the model gave, as YYYY-MM-DD. Takes ISO dates and a few common
    spellings, plus today/tomorrow; anything else is refused, since a date the
    app can't read can't be flagged as due or overdue."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("none", "null", "clear", "no"):
        return ""
    lowered = text.lower()
    base = datetime.date.today()
    if lowered == "today":
        return base.isoformat()
    if lowered == "tomorrow":
        return (base + datetime.timedelta(days=1)).isoformat()
    m = re.fullmatch(r"in (\d+) (day|week)s?", lowered)
    if m:
        days = int(m.group(1)) * (7 if m.group(2) == "week" else 1)
        return (base + datetime.timedelta(days=days)).isoformat()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%b %d %Y", "%B %d %Y",
                "%b %d, %Y", "%B %d, %Y", "%a %b %d %Y"):
        try:
            return datetime.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    for fmt in ("%b %d", "%B %d", "%m/%d"):  # no year: the next one coming up
        try:
            d = datetime.datetime.strptime(text, fmt).date().replace(year=base.year)
            if d < base - datetime.timedelta(days=60):
                d = d.replace(year=base.year + 1)
            return d.isoformat()
        except ValueError:
            pass
    raise ValueError(f"Couldn't read the {what} '{text}'. Give it as YYYY-MM-DD.")


def when(iso):
    """A stored date as the model reads it: 'Thu Oct 2', with how it stands
    against today spelled out, since that's the arithmetic models get wrong."""
    if not iso:
        return ""
    try:
        d = datetime.date.fromisoformat(iso[:10])
    except ValueError:
        return iso
    gap = (d - datetime.date.today()).days
    label = d.strftime("%a %b ") + str(d.day)
    if d.year != datetime.date.today().year:
        label += f" {d.year}"
    if gap == 0:
        return f"{label} (today)"
    if gap == 1:
        return f"{label} (tomorrow)"
    if gap < 0:
        return f"{label} ({-gap} day{'s' if gap != -1 else ''} overdue)"
    return label


def _match(items, key, wanted, what, loose=True):
    """The one item whose `key` is `wanted`: exact first, then case-insensitive,
    then (if `loose`) a unique partial match. Raises when it's ambiguous."""
    wanted = " ".join(str(wanted).split())
    low = wanted.lower()
    tests = [lambda v: v == wanted, lambda v: v.lower() == low]
    if loose:
        tests.append(lambda v: low in v.lower())
    for test in tests:
        found = [i for i in items if test(str(i.get(key, "")))]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            names = "; ".join(str(i[key]) for i in found[:6])
            raise ValueError(f"More than one {what} matches '{wanted}': {names}. "
                             f"Be more specific.")
    return None


# -- leads --------------------------------------------------------------------
def lead_save(name, **fields):
    """Add a lead, or update the one already called `name`. Only the fields
    given change; empty strings clear them."""
    name = " ".join(str(name).split())
    if not name:
        raise ValueError("A lead needs a name.")
    with _lock:
        data = load()
        # Exact names only: "Sam" mustn't quietly overwrite "Samantha Lee".
        lead = _match(data["leads"], "name", name, "lead", loose=False)
        created = lead is None
        if created:
            lead = {"name": name, "stage": "new", "added": today(), "touches": []}
            data["leads"].append(lead)
        stage = fields.pop("stage", None)
        if stage:
            lead["stage"] = _stage(stage)
        follow_up = fields.pop("follow_up", None)
        if follow_up is not None:
            lead["follow_up"] = as_date(follow_up, "follow-up date")
        for key, value in fields.items():
            if value is not None:
                lead[key] = " ".join(str(value).split())
        lead["updated"] = today()
        save(data)
        return lead, created


def lead_touch(name, what, stage=None, follow_up=None):
    """Record one contact with a lead — sent, replied, met — and optionally
    move it along the pipeline and set when to follow up."""
    what = " ".join(str(what).split())
    if not what:
        raise ValueError("Say what happened.")
    with _lock:
        data = load()
        lead = _match(data["leads"], "name", name, "lead")
        if lead is None:
            raise ValueError(f"No lead called '{name}'. Add them with business_lead first.")
        lead.setdefault("touches", []).append({"on": today(), "what": what})
        if stage:
            lead["stage"] = _stage(stage)
        elif lead.get("stage") == "new":
            lead["stage"] = "contacted"
        if follow_up is not None:
            lead["follow_up"] = as_date(follow_up, "follow-up date")
        lead["last_touch"] = today()
        lead["updated"] = today()
        save(data)
        return lead


def _stage(stage):
    s = str(stage).strip().lower()
    if s not in LEAD_STAGES:
        raise ValueError(f"Unknown stage '{stage}'. Stages: {', '.join(LEAD_STAGES)}")
    return s


def leads(stage=None, search=None):
    data = load()
    out = data["leads"]
    if stage:
        out = [l for l in out if l.get("stage") == _stage(stage)]
    if search:
        q = str(search).lower()
        out = [l for l in out if q in json.dumps(l, ensure_ascii=False).lower()]
    return out


def lead_line(lead, full=False):
    bits = [lead["name"]]
    who = ", ".join(x for x in (lead.get("role"), lead.get("company")) if x)
    if who:
        bits[0] += f" ({who})"
    bits.append(lead.get("stage", "new"))
    if lead.get("contact"):
        bits.append(lead["contact"])
    if lead.get("last_touch"):
        bits.append(f"last contact {when(lead['last_touch'])}")
    if lead.get("follow_up"):
        bits.append(f"follow up {when(lead['follow_up'])}")
    line = "- " + " — ".join(bits)
    if lead.get("notes"):
        line += f". {lead['notes']}"
    if full:
        extra = [f"    {k}: {lead[k]}" for k in ("channel", "source", "added")
                 if lead.get(k)]
        extra += [f"    {t['on']}: {t['what']}" for t in lead.get("touches", [])]
        line = "\n".join([line, *extra])
    return line


# -- tasks --------------------------------------------------------------------
def task_add(task, due=None, priority=None, notes=None):
    task = " ".join(str(task).split())
    if not task:
        raise ValueError("A task needs saying what it is.")
    with _lock:
        data = load()
        same = [t for t in data["tasks"]
                if t["task"].lower() == task.lower() and t.get("state") == "open"]
        if same:
            raise ValueError("That task is already on the list.")
        item = {"task": task, "state": "open", "added": today(),
                "priority": _priority(priority)}
        if due:
            item["due"] = as_date(due, "due date")
        if notes:
            item["notes"] = " ".join(str(notes).split())
        data["tasks"].append(item)
        save(data)
        return item


def task_update(task, state=None, due=None, priority=None, notes=None, rename=None):
    with _lock:
        data = load()
        live = [t for t in data["tasks"] if t.get("state") == "open"]
        item = _match(live, "task", task, "open task") or \
            _match(data["tasks"], "task", task, "task")
        if item is None:
            raise ValueError(f"No task matching '{task}'. Open tasks: "
                             + ("; ".join(t["task"] for t in live) or "none"))
        if state:
            s = str(state).strip().lower()
            if s not in TASK_STATES:
                raise ValueError(f"Unknown state '{state}'. States: {', '.join(TASK_STATES)}")
            item["state"] = s
            if s == "open":
                item.pop("closed", None)
            else:
                item["closed"] = today()
        if due is not None:
            item["due"] = as_date(due, "due date")
        if priority:
            item["priority"] = _priority(priority)
        if notes is not None:
            item["notes"] = " ".join(str(notes).split())
        if rename:
            item["task"] = " ".join(str(rename).split())
        save(data)
        return item


def _priority(priority):
    p = str(priority or "medium").strip().lower()
    if p not in PRIORITIES:
        raise ValueError(f"Unknown priority '{priority}'. Priorities: {', '.join(PRIORITIES)}")
    return p


def task_line(t):
    line = f"- [{t.get('priority', 'medium')}] {t['task']}"
    if t.get("state") == "open" and t.get("due"):
        line += f" — due {when(t['due'])}"
    if t.get("state") != "open":
        line += f" — {t['state']} {when(t.get('closed'))}"
    if t.get("notes"):
        line += f". {t['notes']}"
    return line


def _open_tasks(data):
    rank = {p: i for i, p in enumerate(PRIORITIES)}
    return sorted((t for t in data["tasks"] if t.get("state") == "open"),
                  key=lambda t: (t.get("due") or "9999", rank.get(t.get("priority"), 1)))


# -- the log ------------------------------------------------------------------
def log(entry, kind="note", on=None):
    entry = " ".join(str(entry).split())
    if not entry:
        raise ValueError("There's nothing to log in an empty entry.")
    kind = str(kind or "note").strip().lower()
    if kind not in LOG_KINDS:
        raise ValueError(f"Unknown kind '{kind}'. Kinds: {', '.join(LOG_KINDS)}")
    with _lock:
        data = load()
        item = {"on": as_date(on) if on else today(), "kind": kind, "entry": entry}
        data["log"].append(item)
        data["log"].sort(key=lambda e: e["on"])  # stable: same-day entries keep order
        save(data)
        return item


def log_line(e):
    return f"- {e['on']} {e['kind']}: {e['entry']}"


# -- metrics ------------------------------------------------------------------
def metric(name, value, note=None):
    name = " ".join(str(name).split())
    value = " ".join(str(value).split())
    if not name or not value:
        raise ValueError("A metric needs a name and a value.")
    with _lock:
        data = load()
        key = next((k for k in data["metrics"] if k.lower() == name.lower()), name)
        point = {"on": today(), "value": value}
        if note:
            point["note"] = " ".join(str(note).split())
        data["metrics"].setdefault(key, []).append(point)
        save(data)
        return key, data["metrics"][key]


def metric_line(name, points):
    last = points[-1]
    line = f"- {name}: {last['value']} (as of {last['on']}"
    if len(points) > 1:
        line += f"; was {points[-2]['value']} on {points[-2]['on']}"
    line += ")"
    if last.get("note"):
        line += f". {last['note']}"
    return line


# =============================================================================
# What the model sees
# =============================================================================

def render():
    """The brief, then the open tasks, the live pipeline, the latest metrics
    and the recent log — what's sent with every message."""
    text = _COMMENT.sub("", read())
    blocks = re.split(r"(?m)^(?=#{1,6}\s)", text)
    brief = "\n\n".join(b.strip() for b in blocks
                        if b.strip() and not re.fullmatch(r"#{2,6}\s+[^\n]*", b.strip()))
    # The title is the section heading render() sits under; drop it here.
    brief = re.sub(r"(?m)\A#[ \t]+[^\n]*\n*", "", brief).strip()
    try:
        data = load()
    except RuntimeError as e:
        return f"{brief}\n\n(The business records couldn't be read: {e})"

    out = [brief] if brief else []

    tasks = _open_tasks(data)
    out.append("## Open tasks\n" + ("\n".join(map(task_line, tasks)) or "None."))

    live = [l for l in data["leads"] if l.get("stage") not in CLOSED_STAGES]
    live.sort(key=lambda l: (l.get("follow_up") or "9999", l.get("updated", "")))
    counts = {s: sum(1 for l in data["leads"] if l.get("stage") == s) for s in LEAD_STAGES}
    summary = ", ".join(f"{n} {s}" for s, n in counts.items() if n)
    pipeline = [f"## Outreach pipeline\n{summary or 'No leads yet.'}"]
    pipeline += [lead_line(l) for l in live[:SHOWN_LEADS]]
    if len(live) > SHOWN_LEADS:
        pipeline.append(f"(and {len(live) - SHOWN_LEADS} more — business_leads lists them)")
    out.append("\n".join(pipeline))

    if data["metrics"]:
        out.append("## Metrics\n" + "\n".join(metric_line(k, v)
                                               for k, v in data["metrics"].items() if v))

    recent = data["log"][-SHOWN_LOG:]
    if recent:
        head = "## Log (most recent)" if len(data["log"]) > SHOWN_LOG else "## Log"
        out.append(head + "\n" + "\n".join(map(log_line, recent)))

    return "\n\n".join(out)


# =============================================================================
# History
# =============================================================================

def _backup(path):
    if not os.path.exists(path):
        return
    os.makedirs(HISTORY, exist_ok=True)
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"
    stem, ext = os.path.splitext(os.path.basename(path))
    shutil.copyfile(path, os.path.join(HISTORY, f"{stem}-{stamp}{ext}"))
    kept = sorted(f for f in os.listdir(HISTORY) if f.startswith(stem + "-"))
    for old in kept[:-HISTORY_KEEP]:
        try:
            os.remove(os.path.join(HISTORY, old))
        except OSError:
            pass
