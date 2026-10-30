"""The coding agent: BananaGent's own model managing Claude Code.

BananaGent's model is a good manager and a mediocre programmer; Claude Code is
the other way round. So for a coding job the model doesn't write the code. It
runs a loop, in the background, in a project folder in the sandbox:

    look at the project → plan → brief Claude → read Claude's recap →
    check the work (read files, run things) → tell Claude what's next or
    what's wrong → … → finish with a report for the user

It keeps going round until it judges the job done — the user's request met,
and checked rather than taken on Claude's word — or it needs the user.

The loop is brain.converse() with the coder-scoped tools (endpoints/coder_tools.py)
and agents/coder.md as the prompt, so the call protocol and its safeguards are
the assistant's own. Three things are added around it, in _between(), which
runs before every request to the model:

  * a stop request ends it there;
  * anything the user said to it meanwhile (code_message) is passed on;
  * once its conversation gets long, older tool results are shortened, and it's
    reminded of its plan — so a long job doesn't outgrow the model's context.

A reply with no tool call doesn't end the job; it's asked to either carry on
or call coding_finish. Only coding_finish, the step budget, or a stop ends it.
"""

import collections
import json
import os
import re
import time

import brain
import claude_code
import config
import endpoints
import workspace
from .registry import WAITING, WORKING, Agent, Approval, Denied, announce, register

PROMPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coder.md")
_COMMENT = re.compile(r"<!--.*?-->", re.S)

NUDGE = ("You replied without calling a tool, and the job only ends with coding_finish. "
         "If the work is done and checked, call coding_finish with your report. If you "
         "need the user to decide something, call ask_user. Otherwise make your next "
         "call now — most likely claude_send.")


class _Finished(Exception):
    """Raised between rounds once coding_finish has been called."""


def system_prompt():
    """agents/coder.md, notes stripped and {{tools}} filled — re-read every
    task, so an edit from the Prompts tab applies to the next one."""
    with open(PROMPT, encoding="utf-8") as f:
        text = _COMMENT.sub("", f.read())
    text = text.replace("{{tools}}", endpoints.catalog_prompt(endpoints.names(endpoints.CODER)))
    text = text.replace("{{sandbox}}", workspace.ROOT)
    text = text.replace("{{neon_org}}", config.NEON_ORG_ID)
    text = text.replace("{{neon_region}}", config.NEON_REGION)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class CoderAgent(Agent):
    name = "coder"
    title = "Coding Agent"
    description = ("Builds and changes code in the sandbox by working with Claude Code: "
                   "briefs it, reads its recaps, checks the work, and keeps going until "
                   "the job is done.")
    answer_tool = ("code_message (with decision approve or deny if it asked for "
                   "approval)")

    def __init__(self):
        super().__init__()
        self.project = None
        self.plan = ""
        self.final = None
        self.calls = 0
        self.inbox = collections.deque()
        self._compactions = 0

    # -- lifecycle ------------------------------------------------------------
    def start(self, task, project=None):
        if self.busy():
            return super().start(task)  # raises, saying what it's busy with
        self.project = workspace.slug(project or _name_from(task))
        self.plan = ""
        self.final = None
        self.calls = 0
        self._compactions = 0
        self.inbox.clear()
        return super().start(task)

    def stop(self, why="stopped by request"):
        super().stop(why)
        if self.project:
            claude_code.stop_all(self.project)
        return True

    def close(self):
        self.stop("shutting down")

    def _cleanup(self):
        # Nothing Claude is part way through should outlive the task.
        if self.project:
            claude_code.stop_all(self.project)

    def tell(self, text):
        """A message from the user, handed over at the agent's next step (or
        as the answer, if it's waiting on a question)."""
        if not self.busy():
            raise RuntimeError("The coding agent isn't working on anything. Start it with "
                               "code_do.")
        self.inbox.append(str(text).strip())
        self.log(f"message from you: {text}")

    # -- the loop -------------------------------------------------------------
    def _run(self, task):
        status = claude_code.auth_status()
        if not status["ok"]:
            raise RuntimeError(status["problem"])
        folder = workspace.project_dir(self.project)
        self.step(f"project '{self.project}' at {folder}")

        messages = [{"role": "system", "content": system_prompt()},
                    {"role": "user", "content": self._brief(task)}]
        allowed = endpoints.names(endpoints.CODER)
        nudges = 0
        while True:
            said = []  # what it wrote since the last tool call
            try:
                for piece in brain.converse(messages, allowed=allowed,
                                            max_rounds=max(1, config.CODER_MAX_STEPS - self.calls),
                                            before_round=self._between):
                    if isinstance(piece, brain.ToolUse):
                        said = []
                        if piece.status == "running":
                            self.calls += 1
                            self.step(f"{piece.name} {_args_line(piece.args)}")
                        elif piece.status == "failed":
                            self.log(f"{piece.name} failed: {piece.result[:200]}")
                    else:
                        said.append(piece)
            except _Finished:
                break
            text = "".join(said).strip()
            if self.final is not None:
                break
            if self.calls >= config.CODER_MAX_STEPS:
                self.final = {"summary": text or "I ran out of steps before finishing.",
                              "outcome": "out of steps"}
                break
            nudges += 1
            if nudges > 2:
                # Three plain replies running: take the last as its report.
                self.final = {"summary": text or "Stopped without a report.",
                              "outcome": "unclear"}
                break
            self.log("replied without a tool call; nudging it to carry on or finish")
            messages.append({"role": "assistant", "content": text or "(nothing)"})
            messages.append({"role": "user", "content": NUDGE})

        found = claude_code.sessions(self.project)
        cost = sum(s.cost_usd for s in found)
        report = {"answer": self.final["summary"], "outcome": self.final.get("outcome"),
                  "project": self.project, "folder": folder, "open": _link(folder),
                  "claude_cost_usd": round(cost, 4), "steps": self.calls}
        live = workspace.deployments().get(self.project)
        if live:
            report["live_url"] = live["url"]
        # Claude's own account of the last thing it did — what it changed, how
        # to run it, what it verified — so the assistant can relay specifics.
        main = next((s for s in found if s.name == "main"), found[0] if found else None)
        if main is not None and main.last and not main.last["is_error"]:
            report["claude_recap"] = _recap(main.last["reply"])
        return report

    def _brief(self, task):
        """The job, and everything the model needs to know before its first step."""
        folder = workspace.project_dir(self.project)
        listing = workspace.tree(self.project, ".", depth=2, limit=60)
        known = claude_code.sessions(self.project)
        lines = [f"The user's request, passed on by BananaGent:\n\n{task}\n",
                 f"Project: {self.project}",
                 f"Folder: {folder}",
                 "What's in it now:",
                 listing if listing != "(empty)" else "(empty — a brand new project)"]
        live = workspace.deployments().get(self.project)
        if live:
            lines.append(f"\nIt's online at {live['url']} (deployed {live['at']}). Changes "
                         f"only reach that link once you redeploy it.")
        if known:
            lines.append("\nClaude sessions from earlier work on this project (each still "
                         "remembers its whole conversation):")
            for s in known:
                lines.append("- " + json.dumps(s.summary(), ensure_ascii=False))
        others = [p["name"] for p in workspace.projects() if p["name"] != self.project][:12]
        if others:
            lines.append(f"\nOther projects in the sandbox: {', '.join(others)}")
        lines.append("\nStart by making a plan with coding_plan, then brief Claude.")
        return "\n".join(lines)

    def _between(self, messages):
        """Runs before every request to the model: stop, pass on messages, compact."""
        self.check_stop()
        if self.final is not None:
            raise _Finished()
        notes = []
        while self.inbox:
            notes.append("Message from the user, passed on by BananaGent — it takes "
                         f"priority over your plan:\n{self.inbox.popleft()}")
        if sum(len(m["content"]) for m in messages) > config.CODER_CONTEXT_CHARS:
            self._compact(messages)
            notes.append("Your conversation was getting long, so older tool results were "
                         "shortened. Use claude_history or sandbox_read to look anything up "
                         f"again. Your plan, as you last wrote it:\n{self.plan or '(none)'}")
        if notes:
            extra = "\n\n" + "\n\n".join(notes)
            if messages[-1]["role"] == "user":
                messages[-1] = dict(messages[-1], content=messages[-1]["content"] + extra)
            else:
                messages.append({"role": "user", "content": extra.strip()})

    def _compact(self, messages):
        keep = config.CODER_KEEP_RECENT
        for i in range(2, max(2, len(messages) - keep)):
            content = messages[i]["content"]
            if len(content) > 700:
                messages[i] = dict(messages[i], content=content[:450] +
                                   " …(shortened to save room)" + content[-150:])
        self._compactions += 1
        self.log(f"shortened older history (#{self._compactions})")

    # -- asking the user --------------------------------------------------------
    def approve(self, kind, question, detail=""):
        """Ask for a yes before a step; False (not an exception) on a no.

        Agent.ask ends the whole task on a refusal, which is right for the
        browsing agent's irreversible steps. Here a no only means "not that
        one": the model is told and finds another way.
        """
        try:
            return self.ask(kind, question, detail, timeout=config.CODER_APPROVAL_SECONDS)
        except Denied:
            self.check_stop()
            self.pending = None
            self.status = WORKING
            return False

    def question(self, question, timeout=None):
        """Ask the user something and wait for their answer.

        Answered with code_message (any text), or the dashboard's buttons
        (Approve reads as yes, Deny as no). Unanswered, it comes back empty and
        the model is told to use its judgment.
        """
        self.check_stop()
        timeout = config.CODER_QUESTION_SECONDS if timeout is None else timeout
        approval = Approval("question", question)
        self.pending = approval
        self.status = WAITING
        self.detail = question
        self.log(f"asking you: {question}")
        announce(self, "waiting")
        deadline = time.time() + timeout
        answer = None
        try:
            while answer is None:
                self.check_stop()
                if self.inbox:
                    answer = self.inbox.popleft()
                elif approval.granted is not None:
                    answer = "yes" if approval.granted else "no"
                elif time.time() > deadline:
                    break
                else:
                    time.sleep(0.25)
        finally:
            self.pending = None
            if not self._stop.is_set():
                self.status = WORKING
        if answer is None:
            self.log("no answer; carrying on")
            return (f"The user didn't answer within {int(timeout // 60)} minutes. Use your "
                    f"best judgment, and say what you assumed in your final report.")
        self.log(f"you answered: {answer}")
        return f"The user answered: {answer}"

    # -- what the dashboard and the model read -------------------------------
    def snapshot(self):
        state = super().snapshot()
        live = []
        if self.project:
            for s in claude_code.sessions(self.project):
                if s.busy():
                    live.append(s.summary())
        state.update({
            "project": self.project,
            "folder": workspace.project_dir(self.project, create=False) if self.project else None,
            "plan": self.plan or None,
            "claude_working": live or None,
            "tool_calls": self.calls,
        })
        return state


def _name_from(task):
    """A project name from the first few meaningful words of the task."""
    stop = {"a", "an", "the", "me", "my", "for", "to", "of", "and", "with", "that", "please",
            "make", "build", "create", "write", "code", "can", "you", "could", "i", "want",
            "need", "some", "little", "simple", "quick", "new"}
    words = [w for w in re.findall(r"[a-z0-9]+", str(task).lower()) if w not in stop]
    return "-".join(words[:3]) or "project"


def _link(folder):
    """What to open to see the result: the project's web page if it made one,
    otherwise the folder."""
    for name in ("index.html", os.path.join("public", "index.html"),
                 os.path.join("dist", "index.html")):
        page = os.path.join(folder, name)
        if os.path.isfile(page):
            return "file:///" + page.replace("\\", "/")
    return folder


def _recap(reply):
    """The RECAP block from the end of a Claude reply, or its tail if it
    didn't write one."""
    i = reply.rfind("RECAP")
    text = reply[i:] if i != -1 else reply[-1200:]
    return text.strip()[:1500]


def _args_line(args):
    if not isinstance(args, dict):
        return str(args)[:120]
    bits = []
    for k, v in args.items():
        v = str(v).replace("\n", " ")
        bits.append(f"{k}={v[:70] + '…' if len(v) > 70 else v}")
    return " ".join(bits)[:160]


def on_claude(session, text):
    """Claude's live activity: the coding agent's detail line, and the steps
    that change something go in its activity log too."""
    agent = _AGENT
    if not agent.busy():
        return
    if agent.status != WAITING:
        agent.detail = f"Claude ({session.name}) {text}"
    if text.startswith(("edits ", "writes ", "runs: ", "a step failed", "replied", "stopped")):
        agent.log(f"Claude ({session.name}) {text}")


_AGENT = register(CoderAgent())
