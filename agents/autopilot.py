"""Autopilot: BananaGent's model working one long job for hours, on its own.

The coding agent runs one coding job in one conversation. Autopilot is for
jobs that won't fit in one: open-ended, wide or simply long — research a
market and write it up, plan a trip down to the bookings, work through a list
of fifty things. So it doesn't hold the job in its context at all. The job
lives on disk (workbench/): the goal, a plan, a task list it adds to and ticks
off, a quick-jot notebook, the questions it asked the user and their answers,
and a journal of what each stretch of work did.

It runs in cycles:

    brief (built fresh from the saved job) → think: what's next? → work the
    task list with its tools, jotting as it goes → checkpoint (a handoff:
    what got done, what's next) → the next cycle, in a new conversation

That's the loop into itself: every cycle starts by re-reading where things
stand and deciding what to do, rather than carrying on blind from a context
that's 90% stale tool output.

The context manager is _between(), run before every request to the model:

  * past 60% of AUTOPILOT_CONTEXT_CHARS, older tool results are shortened;
  * past 85%, or after AUTOPILOT_CYCLE_CALLS calls, it's told to checkpoint;
  * past 95%, or a few calls after that, the cycle is ended for it and the
    handoff is written by a separate summarising call (_handoff).

Nothing is lost when a cycle ends early, because every task, note and answer
was saved the moment the tool that made it ran.

Questions to the user go out on Discord (question_ask) and don't stop the
work: the task that needs the answer is marked blocked and the model moves on
to another. The answer — a tap on the question's button, its form, or a
Discord reply to it — comes back here instead of to the assistant (main.py
routes it by the question's message id) and is in front of the model before
its next step. With nothing left but waiting, a checkpoint can ask to sleep
until the user answers.

It can hand pieces of the job to the rest of BananaGent — a research
subagent, the coding agent, the browsing agent. An agent it starts reports
back to it, not to the user (Agent.owner); a step one of them wants approved
still goes to the user, because those gates are for a person.

A job that was running when the app quit picks up again when it starts
(AUTOPILOT_RESUME_ON_START), so a code update doesn't have to wait hours for
a long job to end (main.py's _update_restart lets it restart).
"""

import json
import os
import re
import threading
import time

import httpx

import brain
import config
import endpoints
import memory
import workbench
from .registry import (FAILED, STOPPED, WAITING, WORKING, Agent, all_agents, announce,
                       register)

PROMPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autopilot.md")
_COMMENT = re.compile(r"<!--.*?-->", re.S)

NUDGE = ("You replied without calling a tool. In a job, every turn is a tool call: "
         "carry on with the task you're on, or pick the next one from your list. If "
         "you've done a solid stretch of work, call checkpoint. If the whole goal is "
         "met and checked, call job_finish.")
CHECKPOINT_NOW = ("[Context manager — from BananaGent, not the user: {why}. Finish the "
                  "step you're on, make sure your tasks and notebook are up to date, then "
                  "call checkpoint with what this cycle got done and what's next. The "
                  "next cycle starts fresh from your saved plan, tasks, notebook and "
                  "journal — anything only in this conversation is gone after it.]")
HANDOFF = ("You keep the handoff notes for a long job that's worked in stretches. The "
           "stretch below ran out of room before it wrote its own. From its log, write "
           "the note the next stretch starts from, in exactly this form:\n\n"
           "DONE: what got done and found — specific names, numbers, files, links.\n"
           "NEXT: what should happen next, most important first.\n\n"
           "Under 180 words. No preamble.")

# Consecutive model failures (the Spark rebooting, the network dropping)
# before the job gives up, with a wait between each.
RETRIES = 6
RETRY_SECONDS = 60


class _CycleOver(Exception):
    """Raised between rounds to end the current cycle."""


def system_prompt():
    with open(PROMPT, encoding="utf-8") as f:
        text = _COMMENT.sub("", f.read())
    text = text.replace("{{tools}}", endpoints.catalog_prompt(allowed_tools()))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def allowed_tools():
    """Its own tools, the assistant's that config.AUTOPILOT_TOOLS lets it
    have, and every tool the coding agent has made."""
    mine = endpoints.names(endpoints.AUTOPILOT)
    lent = set(config.AUTOPILOT_TOOLS)
    for e in endpoints.all_endpoints():
        if e.name in lent or e.module == "toolbox":
            mine.add(e.name)
    return mine


def _size(messages):
    return sum(len(m["content"]) for m in messages)


def _compact(messages, keep):
    """Shorten old tool results and long turns, never the brief. True if
    anything got shorter."""
    changed = False
    for i in range(2, max(2, len(messages) - keep)):
        content = messages[i]["content"]
        if len(content) > 700:
            messages[i] = dict(messages[i], content=content[:420] +
                               " …(shortened to save room — saved state has what matters)"
                               + content[-150:])
            changed = True
    return changed


def _args_line(args):
    if not isinstance(args, dict):
        return str(args)[:120]
    bits = []
    for k, v in args.items():
        v = str(v).replace("\n", " ")
        bits.append(f"{k}={v[:60] + '…' if len(v) > 60 else v}")
    return " ".join(bits)[:160]


def _runs():
    return {a.name: a.run for a in all_agents()}


def _complete(messages, timeout=300.0):
    """One plain, non-streamed answer from the model — for the handoff."""
    body = brain._body(messages)
    body["stream"] = False
    r = httpx.post(brain._url("/api/chat"), json=body,
                   timeout=httpx.Timeout(10.0, read=timeout))
    r.raise_for_status()
    return r.json().get("message", {}).get("content", "").strip()


class AutopilotAgent(Agent):
    name = "autopilot"
    title = "Autopilot"
    description = ("Works one long or open-ended job for hours: plans it, keeps a task "
                   "list and a notebook, asks the user questions on Discord without "
                   "stopping, and keeps going cycle after cycle until it's done.")
    answer_tool = "autopilot_message (with the question's id)"
    # Safe to restart the app under: the job is on disk and picks up again.
    restartable = True

    def __init__(self):
        super().__init__()
        self.job = None
        self._wake = threading.Event()
        self._checkpoint = None
        self._forced = False
        self._cycle_calls = 0
        self._warned = 0
        self._shutting = False
        self._restarted = False
        self._last_update = 0.0
        self._told_time = 0
        self._asking = None

    # -- lifecycle ------------------------------------------------------------
    def start(self, goal=None, name=None, hours=None, job=None, restarted=False):
        if self.busy():
            return super().start(goal)  # raises, saying what it's on
        if job is None:
            if not str(goal or "").strip():
                raise ValueError("Give the job's goal.")
            job = workbench.create(goal, name)
        elif goal and str(goal).strip() and str(goal).strip() != job.goal:
            job.tell(f"Message from the user as they picked the job up again: {goal}")
        if hours:
            hours = min(float(hours), config.AUTOPILOT_MAX_HOURS)
            job.deadline = time.time() + hours * 3600
        job.status = "running"
        job.final = None
        job.finish_attempts = 0
        job.save()
        self.job = job
        self._restarted = restarted
        self._shutting = False
        self._told_time = 0
        self._wake.clear()
        return super().start(job.goal)

    def stop(self, why="stopped by request"):
        self._wake.set()
        return super().stop(why)

    def close(self):
        """App quitting: stop, but leave the job marked running so it resumes."""
        self._shutting = True
        self.stop("shutting down")

    def _cleanup(self):
        job = self.job
        if job is None:
            return
        if self.status == STOPPED:
            job.status = "running" if self._shutting else "stopped"
        elif self.status == FAILED:
            job.status = "failed"
        job.save()

    def resume_on_start(self):
        """Pick up the job that was running when the app last quit, if any."""
        if self.busy():
            return
        for job in workbench.all_jobs():
            if job.status == "running":
                print(f"[autopilot] picking up '{job.slug}' where it left off")
                try:
                    self.start(job=job, restarted=True)
                except Exception as e:
                    print(f"[autopilot] couldn't resume {job.slug}: {e}")
                return

    # -- the job ----------------------------------------------------------------
    def _run(self, task):
        job = self.job
        began = time.time()
        hard_end = began + config.AUTOPILOT_MAX_HOURS * 3600
        failures = 0
        self.step(f"job '{job.slug}' — {job.counts()['done']} tasks done so far")
        while job.final is None:
            self.check_stop()
            now = time.time()
            if job.calls >= config.AUTOPILOT_MAX_CALLS:
                self._give_up("out of steps", f"It used all {config.AUTOPILOT_MAX_CALLS} "
                                              f"tool calls a job gets.")
                break
            if now > hard_end:
                self._give_up("out of time", f"It ran for the most a job may, "
                                             f"{config.AUTOPILOT_MAX_HOURS} hours.")
                break
            if job.deadline and now > job.deadline + 3600:
                self._give_up("time's up", "Its deadline passed an hour ago and it "
                                           "hadn't finished.")
                break
            try:
                wait = self._cycle()
                failures = 0
            except (httpx.HTTPError, RuntimeError) as e:
                if isinstance(e, RuntimeError) and "outgrown" not in str(e) and \
                        "model server" not in str(e) and "ollama" not in str(e):
                    raise
                failures += 1
                self.log(f"the model didn't answer ({type(e).__name__}: {e}); "
                         f"retry {failures}/{RETRIES} in {RETRY_SECONDS}s")
                if failures >= RETRIES:
                    raise
                self._sleep(RETRY_SECONDS, wake_on_news=False)
                continue
            if wait and job.final is None:
                self._wait_for_user(wait)
        job.status = "finished"
        job.save()
        return self._result()

    def _cycle(self):
        """One stretch of work in a fresh conversation. Returns the minutes
        its checkpoint asked to wait for the user, or 0."""
        job = self.job
        job.cycle += 1
        job.save()
        c = job.counts()
        self.step(f"cycle {job.cycle} — {c['done']} done, "
                  f"{c['todo'] + c['doing'] + c['blocked']} open")
        news = self._take_news()
        messages = [{"role": "system", "content": system_prompt()},
                    {"role": "user", "content": memory.stamp(time.time()) +
                     workbench.brief(job, news, restarted=self._restarted)}]
        self._restarted = False
        self._checkpoint, self._forced = None, False
        self._cycle_calls, self._warned = 0, 0
        allowed = allowed_tools()
        nudges, text = 0, ""
        while True:
            said, before = [], {}
            try:
                for piece in brain.converse(messages, allowed=allowed, max_rounds=10 ** 6,
                                            before_round=self._between):
                    if isinstance(piece, brain.ToolUse):
                        said = []
                        if piece.status == "running":
                            self._cycle_calls += 1
                            job.calls += 1
                            before[piece.id] = _runs()
                            self.step(f"{piece.name} {_args_line(piece.args)}")
                        else:
                            self._adopt(before.pop(piece.id, None))
                            if piece.status == "failed":
                                self.log(f"{piece.name} failed: {piece.result[:160]}")
                    elif isinstance(piece, str):
                        said.append(piece)
            except _CycleOver:
                break
            if self._checkpoint is not None or job.final is not None:
                break
            text = "".join(said).strip()
            nudges += 1
            if nudges > 2:
                self._forced = True
                break
            self.log("replied without a tool call; nudging it on")
            messages.append({"role": "assistant", "content": text or "(nothing)"})
            messages.append({"role": "user", "content": NUDGE})

        if job.final is not None:
            return 0
        if self._checkpoint is None:
            self._handoff(messages, text)
            return 0
        return self._checkpoint.get("wait", 0)

    def _between(self, messages):
        """The context manager. Runs before every request to the model."""
        self.check_stop()
        job = self.job
        if self._checkpoint is not None or job.final is not None:
            raise _CycleOver()
        notes = []
        news = self._take_news()
        if news:
            notes.append("[New while you were working — read it before your next step:\n"
                         + "\n".join(f"- {n}" for n in news) + "]")
        cap = config.AUTOPILOT_CONTEXT_CHARS
        size = _size(messages)
        if size > cap * 0.6 and _compact(messages, config.AUTOPILOT_KEEP_RECENT):
            size = _size(messages)
            self.log(f"context at {size * 100 // cap}%: shortened older results")
        limit = config.AUTOPILOT_CYCLE_CALLS
        if size > cap * 0.95 or self._cycle_calls >= limit + config.AUTOPILOT_CYCLE_GRACE:
            self._forced = True
            self.log("cycle ended for it — context full or call limit passed")
            raise _CycleOver()
        why = None
        if size > cap * 0.85:
            why = f"your context is {size * 100 // cap}% full"
        elif self._cycle_calls >= limit:
            why = (f"you've made {self._cycle_calls} tool calls this cycle — time to step "
                   f"back and re-plan")
        if why:
            self._warned += 1
            notes.append(CHECKPOINT_NOW.format(why=why))
        if job.deadline and time.time() > job.deadline and not self._told_time:
            self._told_time = job.cycle
            notes.append("[The time the user gave you is up. Wrap up: make sure the "
                         "deliverables are saved, then call job_finish with your report.]")
        if notes:
            extra = "\n\n" + "\n\n".join(notes)
            if messages[-1]["role"] == "user":
                messages[-1] = dict(messages[-1], content=messages[-1]["content"] + extra)
            else:
                messages.append({"role": "user", "content": extra.strip()})

    def _handoff(self, messages, last_text=""):
        """Write the journal entry for a cycle that ended without a checkpoint."""
        job = self.job
        log = []
        for m in messages[2:]:
            content = m["content"]
            if len(content) > 900:
                content = content[:600] + " … " + content[-200:]
            log.append(f"{m['role'].upper()}: {content}")
        transcript = "\n\n".join(log)[-30000:]
        done = nxt = ""
        try:
            text = _complete([{"role": "system", "content": HANDOFF},
                              {"role": "user", "content": f"The job's goal: {job.goal}\n\n"
                                                         f"The stretch's log:\n\n{transcript}"}])
            m = re.search(r"DONE:\s*(.*?)(?:\n\s*NEXT:\s*(.*))?$", text, re.S | re.I)
            if m:
                done, nxt = m.group(1).strip(), (m.group(2) or "").strip()
            else:
                done = text
        except Exception as e:
            self.log(f"couldn't write the handoff with the model ({e}); writing it plainly")
        if not done:
            tools = [re.match(r"Tool result for (\w+)", m["content"]) for m in messages[2:]
                     if m["role"] == "user"]
            used = [t.group(1) for t in tools if t]
            done = (f"Made {len(used)} calls ({', '.join(sorted(set(used))) or 'none'}). "
                    + (f"Last said: {last_text[:300]}" if last_text else ""))
            nxt = "Check the task list and carry on with the task marked doing."
        job.log_cycle(done[:1500], nxt[:800], self._cycle_calls, forced=True)
        self.log("handoff written for it")

    def _give_up(self, outcome, why):
        job = self.job
        last = job.journal[-1] if job.journal else None
        c = job.counts()
        summary = (f"{why} By then: {c['done']} tasks done, "
                   f"{c['todo'] + c['doing'] + c['blocked']} still open. "
                   + (f"Its last handoff: {last['done']} Next would have been: {last['next']}"
                      if last else ""))
        job.final = {"report": summary, "outcome": outcome}
        job.save()
        self.log(f"stopped: {outcome}")

    def _result(self):
        job = self.job
        files = []
        for root, _, names in os.walk(job.files_dir):
            for n in names:
                files.append(os.path.join(root, n))
        c = job.counts()
        return {"answer": job.final.get("report"), "outcome": job.final.get("outcome"),
                "job": job.slug, "folder": job.files_dir, "files": files[:30],
                "tasks_done": c["done"], "tasks_open": c["todo"] + c["doing"] + c["blocked"],
                "cycles": job.cycle, "tool_calls": job.calls,
                "unanswered_questions": job.final.get("unanswered") or
                [q["text"] for q in job.open_questions()]}

    # -- waiting ------------------------------------------------------------------
    def _sleep(self, seconds, wake_on_news=True):
        end = time.time() + seconds
        while time.time() < end:
            self.check_stop()
            if wake_on_news and self.job.messages:
                return True
            self._wake.wait(min(2.0, max(0.0, end - time.time())))
            self._wake.clear()
        return False

    def _wait_for_user(self, minutes):
        minutes = max(1, min(int(minutes), config.AUTOPILOT_WAIT_MAX_MINUTES))
        if self.job.messages:
            return
        self.status = WAITING
        self.detail = f"waiting up to {minutes} min for the user to answer"
        self.log(self.detail)
        try:
            heard = self._sleep(minutes * 60)
        finally:
            if not self._stop.is_set():
                self.status = WORKING
        self.log("heard from the user" if heard else "done waiting; carrying on")

    # -- news in -------------------------------------------------------------------
    def _take_news(self):
        return [m["text"] for m in self.job.take_messages()] if self.job else []

    def _adopt(self, before):
        """An agent this job just started reports back here, not to the user."""
        if before is None:
            return
        for agent in all_agents():
            if agent is not self and agent.run and agent.run != before.get(agent.name):
                agent.owner = self._agent_finished
                self.log(f"started the {agent.title.lower()}; it'll report back here")

    def _agent_finished(self, agent, event, label, message):
        job = self.job
        if job is None:
            return
        if agent.status == FAILED:
            text = f"{agent.title} failed on what you gave it ({agent.task[:200]}): {agent.error}"
        else:
            report = json.dumps(agent.result, ensure_ascii=False, default=str)
            if len(report) > 5000:
                report = report[:5000] + " …(cut)"
            text = f"{agent.title} finished what you gave it ({agent.task[:200]}). Its report: {report}"
        job.tell(text)
        self._wake.set()

    def owns(self, message_id):
        """Whether a Discord message is one of this job's questions."""
        job = self.job
        return bool(job and message_id and job.question(message_id=message_id))

    def receive(self, text, question=None, message_id=None):
        """The user's words for the job: an answer to one of its questions, or
        a message. Saved at once, so they reach it even across a restart."""
        job = self.job
        if job is None:
            raise RuntimeError("Autopilot hasn't been given a job. Start one with "
                               "autopilot_start.")
        text = str(text).strip()
        if not text:
            raise ValueError("Give the user's words.")
        q = job.question(qid=question, message_id=message_id) if (question or message_id) \
            else None
        if q is None and question:
            asked = ", ".join(f"{x['id']} \"{x['text']}\"" for x in job.open_questions())
            raise ValueError(f"Autopilot has no question {question}. Open ones: "
                             f"{asked or 'none'}. Leave question out to pass it on as a "
                             f"message.")
        if q is not None:
            freed = job.answer(q, text)
            job.tell(f"The user answered {q['id']} (\"{q['text']}\"): {text}"
                     + (f" — {', '.join(freed)} can go ahead now." if freed else ""))
            self.log(f"the user answered {q['id']}: {text[:80]}")
            said = f"Passed on as the answer to {q['id']}."
        else:
            job.tell(f"Message from the user: {text}")
            self.log(f"message from the user: {text[:80]}")
            said = "Passed on; it'll read it before its next step."
        self._wake.set()
        if not self.busy():
            said += (f" It isn't running right now (the job is {job.status}), so it'll "
                     f"see it when it's picked up again with autopilot_start.")
        return said

    # -- out to the user ---------------------------------------------------------------
    def deliver(self, q):
        """Put a question in front of the user without stopping work: on
        Discord if it's connected, otherwise through the assistant."""
        from integrations import discord_bot

        job = self.job
        text = f"🍌 Autopilot ({job.slug}) asks: {q['text']}"
        if discord_bot.running():
            try:
                channel = discord_bot.channel_for(config.AUTOPILOT_DISCORD_TO)
                q["message_id"] = discord_bot.ask(channel, text, q["kind"], q["choices"])
                q["where"] = "discord"
                job.save()
                return "on Discord"
            except Exception as e:
                self.log(f"couldn't ask on Discord ({e}); asking through the chat")
        q["where"] = "chat"
        job.save()
        self._asking = q
        announce(self, "question")
        return "in BananaGent's chat"

    def update(self, text, path=None):
        from integrations import discord_bot

        gap = config.AUTOPILOT_UPDATE_GAP_SECONDS
        since = time.time() - self._last_update
        if since < gap:
            raise RuntimeError(f"You sent an update {int(since // 60)} min ago. Save "
                               f"updates for milestones — at most one every "
                               f"{gap // 60} min.")
        if not discord_bot.running():
            raise RuntimeError("Discord isn't connected, so there's nowhere to send it. "
                               "Put it in your final report instead.")
        channel = discord_bot.channel_for(config.AUTOPILOT_DISCORD_TO)
        said = discord_bot.send(str(channel), f"🍌 Autopilot ({self.job.slug}): {text}",
                                path)
        self._last_update = time.time()
        return said

    def report(self, event):
        job = self.job
        if event == "question":
            q = self._asking or {}
            return ("Autopilot is asking you something",
                    f"Autopilot, working on the job \"{job.slug}\", has a question for the "
                    f"user. It's carrying on with other work meanwhile. It asks "
                    f"({q.get('id')}): {q.get('text')}"
                    + (f"\nOptions: {', '.join(q['choices'])}" if q.get("choices") else "")
                    + f"\n\nAsk the user this in a sentence or two. When they answer, pass "
                      f"it on with autopilot_message with question \"{q.get('id')}\".")
        if self.status == FAILED:
            return super().report(event)
        r = self.result or {}
        files = "\n".join(r.get("files") or []) or "(none)"
        return ("Autopilot finished",
                f"Autopilot has finished the job \"{r.get('job')}\" ({r.get('outcome')}) "
                f"after {r.get('cycles')} cycles and {r.get('tool_calls')} tool calls. The "
                f"goal was: {job.goal}\n\nIts report:\n{r.get('answer')}\n\nFiles it made, "
                f"in {r.get('folder')}:\n{files}\n\n"
                + (f"Questions the user never answered: {r['unanswered_questions']}\n\n"
                   if r.get("unanswered_questions") else "")
                + "Tell the user now, in your usual short plain style: that it's done, "
                  "what it did or found, and where the results are. That report is "
                  "everything; don't call a tool to check on it.")

    # -- what the dashboard and the model read -------------------------------
    def snapshot(self):
        state = super().snapshot()
        job = self.job
        if job is not None:
            c = job.counts()
            state.update({
                "job": job.slug,
                "job_status": job.status,
                "cycle": job.cycle,
                "tool_calls": job.calls,
                "tasks": f"{c['done']} done, {c['todo'] + c['doing']} to do, "
                         f"{c['blocked']} waiting on the user",
                "doing": next((t["title"] for t in job.tasks if t["status"] == "doing"), None),
                "open_questions": [f"{q['id']}: {q['text']}" for q in job.open_questions()],
                "folder": job.files_dir,
                "deadline": workbench.clock(job.deadline) if job.deadline else None,
            })
        return state

    def context_note(self):
        """A line for the assistant's model with every message, while there's a
        job on: what it's doing, and the questions it's waiting on, so a plain
        text that answers one can be passed on (see brain._context_notes)."""
        job = self.job
        if job is None or (not self.busy() and not job.open_questions()):
            return None
        c = job.counts()
        bits = [f"Autopilot is {'working on' if self.busy() else 'paused on'} the job "
                f"\"{job.slug}\" (cycle {job.cycle}, {c['done']} tasks done, "
                f"{c['todo'] + c['doing'] + c['blocked']} open)."]
        qs = job.open_questions()
        if qs:
            bits.append("It's waiting on the user to answer: " +
                        "; ".join(f"{q['id']} \"{q['text']}\"" for q in qs[-4:]) +
                        ". If this message answers one, pass it on with autopilot_message "
                        "and that question's id.")
        return " ".join(bits)


_AGENT = register(AutopilotAgent())
