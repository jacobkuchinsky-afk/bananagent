"""The agent registry.

An agent is something that goes off and does a job on its own, over many steps,
while the assistant stays responsive. That makes it a different shape from an
endpoint: an endpoint returns in milliseconds, an agent runs for minutes and has
to be watchable and interruptible while it does.

So every agent carries the same four things, which is what the dashboard's
Agents page renders and what the model reads back over voice:

  * a status — idle, working, waiting, done, failed or stopped;
  * the task it was given, in the words it was given in;
  * an activity log, appended as it goes, so you can see what it actually did;
  * an approval gate, for the steps that shouldn't happen unattended.

Subclasses implement `_run(task)` and nothing else. Threading, cancellation,
logging and approvals are handled here so each agent doesn't reinvent them.

Drop a module in this package and its agent registers itself, exactly like
endpoints — see __init__.py.
"""

import collections
import threading
import time
import traceback

IDLE = "idle"
WORKING = "working"
WAITING = "waiting"      # blocked on a human answering an approval
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"

LIVE = (WORKING, WAITING)

_AGENTS = {}


class Stopped(Exception):
    """Raised inside an agent's thread when someone asks it to stop."""


class Denied(Exception):
    """Raised when the human refuses an approval the agent asked for."""


class Approval:
    """One thing the agent wants a person to say yes to before it happens."""

    def __init__(self, kind, question, detail=""):
        self.kind = kind            # submit | account | purchase | consent | message | other
        self.question = question
        self.detail = detail
        self.asked_at = time.time()
        self.granted = None
        self._decided = threading.Event()

    def decide(self, granted):
        if self.granted is None:
            self.granted = bool(granted)
            self._decided.set()
        return self.granted

    def wait(self, timeout):
        self._decided.wait(timeout)
        return self.granted

    def snapshot(self):
        return {"kind": self.kind, "question": self.question, "detail": self.detail,
                "waiting_for_seconds": round(time.time() - self.asked_at, 1)}


class Agent:
    name = "agent"
    title = "Agent"
    description = ""
    # Steps of this kind always stop and ask, however the task was worded.
    gated = ()

    def __init__(self):
        self.status = IDLE
        self.task = None
        self.detail = ""
        self.result = None
        self.error = None
        self.started_at = None
        self.finished_at = None
        self.steps = 0
        self.pending = None            # an Approval, while status is WAITING
        self.activity = collections.deque(maxlen=60)
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # -- what the dashboard and the model both read --------------------------
    def snapshot(self):
        return {
            "name": self.name,
            "title": self.title,
            "status": self.status,
            "task": self.task,
            "detail": self.detail,
            "steps": self.steps,
            "running_for_seconds": (round(time.time() - self.started_at, 1)
                                    if self.started_at and self.status in LIVE else None),
            "result": self.result,
            "error": self.error,
            "waiting_for": self.pending.snapshot() if self.pending else None,
            "recent": list(self.activity)[-8:],
        }

    def log(self, text):
        line = {"at": time.time(), "text": str(text)[:300]}
        self.activity.append(line)
        print(f"[{self.name}] {line['text']}")
        return line

    # -- lifecycle -----------------------------------------------------------
    def busy(self):
        return self.status in LIVE

    def start(self, task):
        with self._lock:
            if self.busy():
                raise RuntimeError(
                    f"{self.title} is already working on: {self.task}. "
                    "Stop it first if you want it to do something else.")
            self.task = str(task).strip()
            self.status = WORKING
            self.detail = "starting"
            self.result = self.error = None
            self.pending = None
            self.steps = 0
            self.started_at = time.time()
            self.finished_at = None
            self.activity.clear()
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._wrap, args=(self.task,),
                                            daemon=True)
            self._thread.start()
        return self.task

    def stop(self, why="stopped by request"):
        self._stop.set()
        # A pending question would otherwise keep the thread parked forever.
        if self.pending is not None:
            self.pending.decide(False)
        if self.busy():
            self.detail = why
        return True

    def _wrap(self, task):
        try:
            self.result = self._run(task)
            self.status = DONE
            self.detail = "finished"
        except Stopped:
            self.status = STOPPED
            self.detail = "stopped"
            self.log("stopped")
        except Denied:
            self.status = STOPPED
            self.detail = "you said no"
            self.log("stopped — approval refused")
        except Exception as e:
            traceback.print_exc()
            self.status = FAILED
            self.error = f"{type(e).__name__}: {e}"
            self.detail = "failed"
            self.log(f"failed: {self.error}")
        finally:
            self.finished_at = time.time()
            self.pending = None
            self._cleanup()

    def _run(self, task):
        raise NotImplementedError

    def _cleanup(self):
        """Release anything the run held open. Always called."""

    # -- helpers for subclasses ----------------------------------------------
    def check_stop(self):
        if self._stop.is_set():
            raise Stopped()

    def step(self, detail):
        """Mark progress. Also the natural place to notice a stop request."""
        self.check_stop()
        self.steps += 1
        self.detail = detail
        self.log(detail)

    def ask(self, kind, question, detail="", timeout=300.0):
        """Stop and wait for a person to say yes.

        Used for the steps that are hard to take back — submitting a form,
        creating an account, agreeing to terms, spending money. The agent is
        perfectly capable of clicking them; the point is that it shouldn't do
        so while nobody is watching.
        """
        self.check_stop()
        approval = Approval(kind, question, detail)
        self.pending = approval
        self.status = WAITING
        self.detail = question
        self.log(f"waiting for you: {question}")

        granted = approval.wait(timeout)
        self.pending = None
        if self._stop.is_set():
            raise Stopped()
        if granted is None:
            self.status = STOPPED
            raise Denied(f"nobody answered within {int(timeout)}s")
        self.status = WORKING
        if not granted:
            raise Denied(question)
        self.log("you approved it")
        return True

    def answer(self, granted):
        """Called from outside — the human's yes or no."""
        if self.pending is None:
            raise RuntimeError(f"{self.title} isn't waiting on anything.")
        question = self.pending.question
        self.pending.decide(granted)
        return question


# -- access ------------------------------------------------------------------
def register(agent):
    if agent.name in _AGENTS:
        raise ValueError(f"duplicate agent name: {agent.name}")
    _AGENTS[agent.name] = agent
    return agent


def all_agents():
    return sorted(_AGENTS.values(), key=lambda a: a.name)


def get(name):
    agent = _AGENTS.get(name)
    if agent is None:
        known = ", ".join(sorted(_AGENTS)) or "none"
        raise ValueError(f"No agent called '{name}'. Agents: {known}")
    return agent


def snapshot():
    """Every agent's state — what the Agents page paints."""
    return [a.snapshot() for a in all_agents()]


def stop_all():
    for a in all_agents():
        try:
            a.stop("shutting down")
        except Exception:
            pass
