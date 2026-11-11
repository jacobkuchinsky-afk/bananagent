"""The workbench: everything an Autopilot job keeps, on disk.

A long job outlives any one conversation with the model. Its context fills up,
the app restarts to pick up new code, the user takes three hours to answer a
question. So nothing the job needs to carry on lives only in the model's
context: it lives here, and every cycle of the job starts from a fresh
conversation built out of it (brief()).

    workbench/jobs/<slug>/
        job.json      the goal, plan, task list, notebook, questions to the
                      user and their answers, and the journal of every cycle
        files/        what the job makes: reports, drafts, data

What's in a job:

  * plan       the approach, in the model's words — rewritten as it learns
  * tasks      t1, t2… each todo | doing | done | blocked | dropped, with notes
               and an optional parent, so a big task can be split under itself
  * notes      n1, n2… the quick-jot notebook: findings, decisions, numbers,
               links, anything it'll want later
  * questions  q1, q2… asked on Discord without stopping work; a task can wait
               on one (blocked), and gets unblocked when it's answered
  * journal    one entry per cycle: what got done, what's next — the handoff
               from one context window to the next
  * messages   what the user said to it that the model hasn't seen yet

Every change is saved at once (atomically), from whichever thread made it:
tool calls come from the job's thread, answers from the app's.
"""

import json
import os
import re
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "jobs")

STATUSES = ("todo", "doing", "done", "blocked", "dropped")
OPEN = ("todo", "doing", "blocked")
MARK = {"todo": "[ ]", "doing": "[>]", "done": "[x]", "blocked": "[?]", "dropped": "[-]"}

PLAN_CHARS = 5000
TASK_CHARS = 300
NOTE_CHARS = 2000
TASK_NOTE_CHARS = 600

# How much of each part the brief shows. The rest is a tool call away.
BRIEF_NOTES_CHARS = 3500
BRIEF_JOURNAL = 5
BRIEF_DONE = 8


def slug(text):
    words = re.findall(r"[a-z0-9]+", str(text).lower())
    stop = {"a", "an", "the", "me", "my", "for", "to", "of", "and", "with", "that",
            "please", "can", "you", "could", "i", "want", "need", "some", "on", "in",
            "work", "keep", "working", "go", "do", "make", "it", "this", "all"}
    picked = [w for w in words if w not in stop][:4] or words[:4] or ["job"]
    return "-".join(picked)[:48].strip("-") or "job"


def ago(at, now=None):
    if not at:
        return "never"
    s = int((now or time.time()) - at)
    if s < 60:
        return "just now"
    m = s // 60
    if m < 60:
        return f"{m} min ago"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h}h {m}m ago" if m else f"{h}h ago"
    return f"{h // 24} days ago"


def clock(at):
    lt = time.localtime(at)
    return time.strftime("%a %b ", lt) + str(lt.tm_mday) + ", " + \
        time.strftime("%I:%M %p", lt).lstrip("0")


class Job:
    def __init__(self, slug, goal):
        self.slug = slug
        self.goal = goal
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.status = "new"          # new | running | finished | stopped | failed
        self.deadline = None         # a time it's been asked to keep going until
        self.cycle = 0
        self.calls = 0
        self.plan = ""
        self.tasks = []
        self.notes = []
        self.questions = []
        self.journal = []
        self.messages = []           # from the user, not yet shown to the model
        self.final = None
        self.finish_attempts = 0
        self.counters = {"t": 0, "n": 0, "q": 0}
        self._lock = threading.RLock()

    # -- disk ------------------------------------------------------------------
    @property
    def dir(self):
        return os.path.join(ROOT, self.slug)

    @property
    def files_dir(self):
        path = os.path.join(self.dir, "files")
        os.makedirs(path, exist_ok=True)
        return path

    _FIELDS = ("slug", "goal", "created_at", "updated_at", "status", "deadline", "cycle",
               "calls", "plan", "tasks", "notes", "questions", "journal", "messages",
               "final", "finish_attempts", "counters")

    def save(self):
        with self._lock:
            self.updated_at = time.time()
            os.makedirs(self.dir, exist_ok=True)
            data = {k: getattr(self, k) for k in self._FIELDS}
            path = os.path.join(self.dir, "job.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)

    @classmethod
    def load(cls, slug):
        path = os.path.join(ROOT, slug, "job.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        job = cls(data["slug"], data["goal"])
        for k in cls._FIELDS:
            if k in data:
                setattr(job, k, data[k])
        return job

    def _next(self, kind):
        self.counters[kind] = self.counters.get(kind, 0) + 1
        return f"{kind}{self.counters[kind]}"

    # -- tasks -------------------------------------------------------------------
    def task(self, tid):
        tid = str(tid).strip().lower()
        if tid.isdigit():
            tid = "t" + tid
        for t in self.tasks:
            if t["id"] == tid:
                return t
        raise ValueError(f"No task {tid}. Tasks: " +
                         (", ".join(t["id"] for t in self.tasks) or "none yet"))

    def add_tasks(self, titles, parent=None, notes=None):
        if isinstance(titles, str):
            titles = [l for l in re.split(r"\n+", titles)]
        titles = [re.sub(r"^\s*(?:[-*]|\d+[.)]|\[ \])\s*", "", str(t)).strip()
                  for t in titles or []]
        titles = [t[:TASK_CHARS] for t in titles if t]
        if not titles:
            raise ValueError("Give at least one task.")
        with self._lock:
            if parent:
                parent = self.task(parent)["id"]
            made = []
            for title in titles:
                t = {"id": self._next("t"), "title": title, "status": "todo",
                     "parent": parent, "notes": [], "created": time.time(),
                     "updated": time.time(), "blocked_on": None}
                if notes:
                    t["notes"].append(str(notes)[:TASK_NOTE_CHARS])
                self.tasks.append(t)
                made.append(t)
            self.save()
        return made

    def update_task(self, tid, status=None, notes=None, title=None):
        with self._lock:
            t = self.task(tid)
            if status:
                status = str(status).strip().lower()
                status = {"in progress": "doing", "in_progress": "doing", "started": "doing",
                          "complete": "done", "completed": "done", "finished": "done",
                          "skip": "dropped", "skipped": "dropped", "cancelled": "dropped",
                          "open": "todo", "waiting": "blocked"}.get(status, status)
                if status not in STATUSES:
                    raise ValueError(f"status is one of {', '.join(STATUSES)}.")
                if status == "doing":
                    # One thing at a time: whatever else was "doing" goes back.
                    for other in self.tasks:
                        if other is not t and other["status"] == "doing":
                            other["status"] = "todo"
                if status != "blocked":
                    t["blocked_on"] = None
                t["status"] = status
                if status == "done":
                    t["done_at"] = time.time()
            if title:
                t["title"] = str(title).strip()[:TASK_CHARS]
            if notes:
                t["notes"].append(str(notes).strip()[:TASK_NOTE_CHARS])
                t["notes"] = t["notes"][-6:]
            t["updated"] = time.time()
            self.save()
            return t

    def open_tasks(self):
        return [t for t in self.tasks if t["status"] in OPEN]

    def task_line(self, t, notes=True):
        depth = 0
        p = t.get("parent")
        while p and depth < 4:
            depth += 1
            p = next((x.get("parent") for x in self.tasks if x["id"] == p), None)
        line = f"{'  ' * depth}{MARK[t['status']]} {t['id']} {t['title']}"
        if t["status"] == "blocked" and t.get("blocked_on"):
            line += f" (waiting on {t['blocked_on']})"
        if notes and t["notes"]:
            line += f" — {t['notes'][-1]}"
        return line

    def task_lines(self, show_done=True, done_limit=None):
        done = [t for t in self.tasks if t["status"] == "done"]
        hidden = 0
        if done_limit is not None and len(done) > done_limit:
            hidden = len(done) - done_limit
            keep = {t["id"] for t in done[-done_limit:]}
        lines = []
        for t in self.tasks:
            if t["status"] == "done":
                if not show_done or (hidden and t["id"] not in keep):
                    continue
            if t["status"] == "dropped" and not show_done:
                continue
            lines.append(self.task_line(t, notes=t["status"] in OPEN))
        if hidden:
            lines.insert(0, f"({hidden} earlier finished tasks not shown — todo_list "
                            f"shows everything)")
        return lines

    def counts(self):
        c = {s: 0 for s in STATUSES}
        for t in self.tasks:
            c[t["status"]] += 1
        return c

    # -- notebook ----------------------------------------------------------------
    def jot(self, text, tag=None, replaces=None):
        text = str(text or "").strip()
        if not text:
            raise ValueError("Write the note.")
        text = text[:NOTE_CHARS]
        with self._lock:
            if replaces:
                rid = str(replaces).strip().lower()
                rid = "n" + rid if rid.isdigit() else rid
                for n in self.notes:
                    if n["id"] == rid:
                        n.update(text=text, at=time.time(), tag=tag or n.get("tag"))
                        self.save()
                        return n
                raise ValueError(f"No note {rid}.")
            n = {"id": self._next("n"), "at": time.time(), "text": text,
                 "tag": str(tag).strip()[:30] if tag else None}
            self.notes.append(n)
            self.save()
            return n

    def note_line(self, n):
        tag = f"[{n['tag']}] " if n.get("tag") else ""
        return f"{n['id']} {tag}{n['text']}"

    def search(self, words=None):
        """Notes and journal entries matching every word (all of them if none)."""
        terms = [w for w in re.findall(r"\w+", str(words or "").lower())]
        hit = lambda text: all(w in text.lower() for w in terms)  # noqa: E731
        notes = [self.note_line(n) for n in self.notes if hit(self.note_line(n))]
        journal = [self.journal_line(j) for j in self.journal if hit(self.journal_line(j))]
        return notes, journal

    # -- questions ---------------------------------------------------------------
    def ask(self, text, kind="short", choices=None, task=None):
        with self._lock:
            q = {"id": self._next("q"), "text": text, "kind": kind,
                 "choices": list(choices or []), "asked_at": time.time(),
                 "status": "open", "answer": None, "answered_at": None,
                 "message_id": None, "where": None, "task": None}
            if task:
                t = self.task(task)
                t["status"], t["blocked_on"] = "blocked", q["id"]
                q["task"] = t["id"]
            self.questions.append(q)
            self.save()
            return q

    def question(self, qid=None, message_id=None):
        for q in self.questions:
            if (qid and q["id"] == str(qid).strip().lower()) or \
                    (message_id and q.get("message_id") == message_id):
                return q
        return None

    def answer(self, q, text):
        """Record the user's answer; any task waiting on it is free again."""
        with self._lock:
            q.update(status="answered", answer=str(text).strip(), answered_at=time.time())
            freed = []
            for t in self.tasks:
                if t["status"] == "blocked" and t.get("blocked_on") == q["id"]:
                    t["status"], t["blocked_on"] = "todo", None
                    t["notes"].append(f"{q['id']} answered: {q['answer'][:200]}")
                    freed.append(t["id"])
            self.save()
            return freed

    def open_questions(self):
        return [q for q in self.questions if q["status"] == "open"]

    # -- journal and messages ------------------------------------------------------
    def log_cycle(self, done, next_step, calls, forced=False):
        with self._lock:
            entry = {"cycle": self.cycle, "at": time.time(), "done": str(done).strip(),
                     "next": str(next_step or "").strip(), "calls": calls, "forced": forced}
            self.journal.append(entry)
            self.save()
            return entry

    def journal_line(self, j):
        tag = " (handoff written for it — context ran out)" if j.get("forced") else ""
        line = f"Cycle {j['cycle']}, {clock(j['at'])}{tag}: {j['done']}"
        if j.get("next"):
            line += f" | Next: {j['next']}"
        return line

    def tell(self, text):
        with self._lock:
            self.messages.append({"at": time.time(), "text": str(text).strip()})
            self.save()

    def take_messages(self):
        with self._lock:
            got, self.messages = self.messages, []
            if got:
                self.save()
            return got


# -- jobs on disk -------------------------------------------------------------------
def exists(name):
    return os.path.isfile(os.path.join(ROOT, name, "job.json"))


def create(goal, name=None):
    base = slug(name or goal)
    name, n = base, 2
    while exists(name):
        name, n = f"{base}-{n}", n + 1
    job = Job(name, str(goal).strip())
    job.save()
    return job


def all_jobs():
    if not os.path.isdir(ROOT):
        return []
    found = []
    for name in os.listdir(ROOT):
        if exists(name):
            try:
                found.append(Job.load(name))
            except Exception as e:
                print(f"[workbench] couldn't read job {name}: {e}")
    return sorted(found, key=lambda j: j.updated_at, reverse=True)


def find(name):
    """A job by its exact slug, or the one whose slug or goal best fits."""
    name = str(name or "").strip()
    if not name:
        return None
    if exists(name):
        return Job.load(name)
    s = slug(name)
    if exists(s):
        return Job.load(s)
    words = set(s.split("-"))
    best, score = None, 0
    for job in all_jobs():
        have = set(job.slug.split("-")) | set(slug(job.goal).split("-"))
        hit = len(words & have)
        if hit > score:
            best, score = job, hit
    return best if score >= max(1, len(words) // 2) else None


# -- the brief: what each cycle starts from ----------------------------------------
def brief(job, news=(), restarted=False, now=None):
    """Everything the model needs to pick the job up cold, as one message."""
    now = now or time.time()
    c = job.counts()
    total_open = c["todo"] + c["doing"] + c["blocked"]
    out = [f"# Your job: {job.slug}", "", "## The goal, in the user's words", job.goal, ""]

    head = [f"Started {ago(job.created_at, now)}; this is cycle {job.cycle}; "
            f"{job.calls} tool calls so far."]
    if job.deadline:
        left = job.deadline - now
        head.append(f"The user asked you to keep working on this until {clock(job.deadline)} "
                    + (f"— {_left(left)} left. Until then, keep finding ways to make "
                       f"the result better rather than finishing early."
                       if left > 0 else "— that time has passed: wrap up this cycle and "
                                        "finish with your report."))
    out += ["## Where things stand", " ".join(head)]
    if restarted:
        out.append("BananaGent restarted since your last cycle, so whatever you were in "
                   "the middle of may not have been written down. Check the tasks "
                   "marked [>] before carrying on.")
    out.append("")

    out += ["## Plan", job.plan or "(none yet — write one with plan_write)", ""]

    out.append(f"## Tasks ({c['done']} done, {total_open} open"
               + (f", {c['blocked']} waiting on the user" if c["blocked"] else "") + ")")
    lines = job.task_lines(show_done=True, done_limit=BRIEF_DONE)
    lines = [l for l in lines if not l.lstrip().startswith(MARK["dropped"])]
    out += lines or ["(none yet — break the plan down with todo_add)"]
    out.append("")

    qs = job.questions
    if qs:
        out.append("## Questions you asked the user")
        for q in qs[-10:]:
            if q["status"] == "open":
                out.append(f"- {q['id']} (still open, asked {ago(q['asked_at'], now)}): "
                           f"{q['text']}")
            elif q["status"] == "answered":
                out.append(f"- {q['id']} answered: \"{q['text']}\" → {q['answer']}")
        out.append("")

    if job.notes:
        out.append("## Your notebook (newest last)")
        shown, size = [], 0
        for n in reversed(job.notes):
            line = job.note_line(n)
            if size + len(line) > BRIEF_NOTES_CHARS and shown:
                break
            shown.append(line)
            size += len(line)
        if len(shown) < len(job.notes):
            out.append(f"({len(job.notes) - len(shown)} older notes not shown — "
                       f"notebook_read finds them)")
        out += [f"- {l}" for l in reversed(shown)]
        out.append("")

    if job.journal:
        out.append("## Journal: what earlier cycles did")
        if len(job.journal) > BRIEF_JOURNAL:
            out.append(f"({len(job.journal) - BRIEF_JOURNAL} earlier cycles not shown)")
        out += [f"- {job.journal_line(j)}" for j in job.journal[-BRIEF_JOURNAL:]]
        out.append("")

    if news:
        out.append("## New since your last cycle — read this first")
        out += [f"- {n}" for n in news]
        out.append("")

    out.append(f"Your files folder: {job.files_dir}")
    out.append("")
    out.append("## This cycle")
    if job.cycle <= 1 and not job.tasks:
        out.append(
            "This is the start of the job. Work out what the goal really needs, write "
            "your approach with plan_write, and break it into concrete tasks with "
            "todo_add. If there's something only the user can tell you, ask it with "
            "question_ask now — then carry straight on with everything that doesn't "
            "depend on the answer. Then mark the first task doing and start on it.")
    else:
        out.append(
            "Think before you act: read what's new, then look at the plan and the "
            "tasks. Is the plan still right? Add, split or drop tasks if it isn't. "
            "Then pick the most valuable task you can move forward right now — not "
            "one waiting on the user — mark it doing, and work on it. When it's done, "
            "mark it done and take the next. Jot down anything you'll want later. "
            "When you've done a solid stretch of work, or you're told your context "
            "is filling up, call checkpoint.")
    return "\n".join(out)


def _left(seconds):
    m = int(seconds // 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m} min"
