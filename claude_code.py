"""Claude Code, run headless, as the coding agent's hands.

Each message to Claude is one run of

    claude -p --output-format stream-json --resume <session id>

in the project's folder, with the message on stdin. The first message of a
session has no --resume and Claude hands back a new session id; every message
after resumes it, so Claude remembers everything it has read, written and been
told in that session. Session ids are saved in sandbox/.banana/<project>.json,
so a project picked up next week carries on the same conversation.

A turn runs on its own thread. stream-json reports every step as it happens —
each file Claude reads or edits, each command it runs — which becomes the live
activity the dashboard shows and the coding agent reads while it waits. When
the run ends, the final event carries Claude's reply, its cost, and anything
it was refused permission to do. The files that changed are found by diffing
the project folder before and after, so the coding agent can check a recap
against what actually happened on disk.

Every exchange is also written out in full to sandbox/.banana/logs/, for the
user to read whenever they like.

Claude is told (with --append-system-prompt) that a program is driving it, that
no one can answer questions mid-turn, and to end each turn with a RECAP in a
fixed shape — which is what the coding agent's prompt teaches it to read.
"""

import collections
import glob
import json
import os
import shutil
import subprocess
import threading
import time

import config
import workspace

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DRIVER_NOTE = """\
You are being driven by BananaGent, an automated assistant working for its user. \
BananaGent sends you tasks and reads your replies; no person is watching this \
terminal, and nobody can answer a question in the middle of your turn.

- Work inside the current directory, which is this project's folder in \
BananaGent's sandbox. Don't read or change anything outside it.
- If something is ambiguous, pick the most sensible option, say what you assumed, \
and carry on. Only stop to ask when a wrong guess would waste most of the work.
- Verify your own work whenever you can: run the code, run the tests, and report \
what actually happened rather than what should happen.
- Run one command per tool call. Commands chained with ; && or | need an approval \
nobody is here to give, so they're refused; separate calls are allowed.
- If you are refused permission for a command, don't loop on it — find another \
way, or say exactly which command you needed and why.
- Putting an app online: the `vercel` (hosting) and `neon` (Postgres) CLIs are installed and already signed in through VERCEL_TOKEN and NEON_API_KEY in your environment — never run a login, and never print, echo or write those keys into a file. Deploy only when the task asks for it. Pass --yes to vercel so it never waits on a prompt. Deleting a project or database, or buying a domain, is refused: say so in your recap instead.
- End every reply with a recap in exactly this shape:

RECAP
Done: <what you did, one line per change>
Files: <files created or changed>
Run it: <the exact command to run or try it, if there is one>
Verified: <what you ran to check it, and the result>
Not done / problems: <anything left, broken, blocked or assumed — or "nothing">
Questions: <anything you need decided — or "none">
"""


# -- finding it -----------------------------------------------------------------
def find_exe():
    """The claude CLI: configured, on PATH, the native install, or the copy
    the desktop app keeps (newest version first). It usually isn't on PATH
    here — the desktop app's copy is the one this machine has."""
    if config.CLAUDE_EXE:
        return config.CLAUDE_EXE if os.path.exists(config.CLAUDE_EXE) else None
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    home = os.path.expanduser("~")
    native = os.path.join(home, ".local", "bin", "claude.exe")
    if os.path.exists(native):
        return native
    # The desktop app is an MSIX package: %APPDATA%\Claude is redirected into
    # its container, and only exists as that path for processes inside the
    # app. From out here the files are under LocalAppData\Packages.
    bundled = glob.glob(os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude-code",
                                     "*", "claude.exe"))
    bundled += glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Packages",
                                      "Claude_*", "LocalCache", "Roaming", "Claude",
                                      "claude-code", "*", "claude.exe"))

    def version(p):
        name = os.path.basename(os.path.dirname(p))
        return tuple(int(x) if x.isdigit() else 0 for x in name.split("."))

    return max(bundled, key=version) if bundled else None


def _env():
    """Our environment, minus the markers that make Claude think it's being
    started from inside another Claude session (it refuses to nest), plus the
    Vercel and Neon keys so it can deploy."""
    env = workspace.cloud_env()
    hosted = "CLAUDE_CODE_ENTRYPOINT" in env
    for key in list(env):
        if key == "CLAUDECODE" or key.startswith(("CLAUDE_CODE_", "CLAUDE_AGENT_SDK")) \
                or key == "CLAUDE_PID":
            env.pop(key)
    if hosted:
        # Launched from inside a Claude session (while testing, say): its base
        # URL points at that host's connection, not at this login's.
        env.pop("ANTHROPIC_BASE_URL", None)
    return env


_auth = {"at": 0.0, "value": None}


def auth_status():
    """Whether the CLI is installed and signed in: {"ok", "exe", "problem"}.
    Cached for a minute — it's asked at the start of every task."""
    if _auth["value"] and time.time() - _auth["at"] < 60 and _auth["value"]["ok"]:
        return _auth["value"]
    exe = find_exe()
    if not exe:
        value = {"ok": False, "exe": None,
                 "problem": "Claude Code isn't installed. Install it from "
                            "https://claude.com/claude-code, or set CLAUDE_EXE in config.py."}
    else:
        try:
            r = subprocess.run([exe, "auth", "status"], capture_output=True, timeout=30,
                               env=_env(), creationflags=_NO_WINDOW)
            info = json.loads(r.stdout.decode("utf-8", errors="replace") or "{}")
        except Exception as e:
            info = {"loggedIn": None, "error": str(e)}
        if info.get("loggedIn") is False:
            value = {"ok": False, "exe": exe,
                     "problem": f'Claude Code isn\'t signed in. In a terminal, run: '
                                f'"{exe}" auth login'}
        else:
            # None means the check itself failed; let a real turn say what's wrong.
            value = {"ok": True, "exe": exe, "method": info.get("authMethod")}
    _auth.update(at=time.time(), value=value)
    return value


# -- sessions -------------------------------------------------------------------
IDLE, WORKING, FAILED = "idle", "working", "failed"


class Session:
    """One ongoing conversation with Claude Code in one project."""

    def __init__(self, project, name, session_id=None, created=None, cost_usd=0.0,
                 turns=None):
        self.project = project
        self.name = name
        self.session_id = session_id
        self.created = created or time.time()
        self.cost_usd = cost_usd
        self.turns = turns or []           # finished exchanges, oldest first
        self.status = IDLE
        self.activity = collections.deque(maxlen=40)  # what Claude is doing right now
        self.current = None                # the turn in flight
        self.last = None                   # the most recent finished turn
        self.on_activity = None            # callback(session, text), set by the agent
        self._proc = None
        self._thread = None
        self._done = threading.Event()
        self._done.set()
        self._stopped = False

    # -- what the models and dashboard read --------------------------------
    def summary(self):
        out = {"session": self.name, "status": self.status,
               "messages_so_far": len(self.turns),
               "cost_usd": round(self.cost_usd, 4),
               "started": bool(self.session_id)}
        if self.status == WORKING and self.current:
            out["working_for_seconds"] = round(time.time() - self.current["started"], 1)
            out["recent_activity"] = list(self.activity)[-6:]
        elif self.last:
            out["last_reply_start"] = _clip(self.last["reply"], 300)
        return out

    def to_json(self):
        return {"session_id": self.session_id, "created": self.created,
                "cost_usd": self.cost_usd,
                # Replies are in the logs in full; this only has to jog a memory.
                "turns": [dict(t, reply=_clip(t["reply"], 4000), sent=_clip(t["sent"], 4000))
                          for t in self.turns[-30:]]}

    # -- one exchange ---------------------------------------------------------
    def busy(self):
        return self.status == WORKING

    def send(self, message, fresh=False):
        """Start a turn. Returns at once; wait() for the reply."""
        if self.busy():
            raise RuntimeError(f"Claude session '{self.name}' is still working on your last "
                               f"message. claude_wait for it, or claude_stop it first.")
        status = auth_status()
        if not status["ok"]:
            raise RuntimeError(status["problem"])
        if fresh:
            self.session_id = None
            self.turns = []
        self.status = WORKING
        self.activity.clear()
        self._stopped = False
        self._done.clear()
        self.current = {"sent": str(message), "started": time.time()}
        self._note("message sent" if self.session_id else "new session")
        self._thread = threading.Thread(target=self._run, args=(status["exe"], str(message)),
                                        daemon=True)
        self._thread.start()

    def wait(self, timeout, should_stop=None):
        """Block until the turn in flight finishes, up to `timeout` seconds.
        Returns the finished turn, or None if it's still going."""
        deadline = time.time() + timeout
        while not self._done.wait(0.5):
            if should_stop is not None and should_stop():
                self.stop()
                raise InterruptedError("stopped")
            if time.time() > deadline:
                return None
        return self.last

    def stop(self):
        if self._proc is not None and self._proc.poll() is None:
            self._stopped = True
            workspace.kill_tree(self._proc.pid)
            self._note("stopped")
        return True

    def _run(self, exe, message):
        cwd = workspace.project_dir(self.project)
        before = workspace.snapshot(self.project)
        cmd = [exe, "-p", "--output-format", "stream-json", "--verbose",
               "--permission-mode", config.CLAUDE_PERMISSION_MODE,
               "--append-system-prompt", DRIVER_NOTE]
        if config.CLAUDE_MODEL:
            cmd += ["--model", config.CLAUDE_MODEL]
        if config.CLAUDE_EFFORT:
            cmd += ["--effort", config.CLAUDE_EFFORT]
        if config.CLAUDE_MAX_BUDGET_USD:
            cmd += ["--max-budget-usd", str(config.CLAUDE_MAX_BUDGET_USD)]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        allowed = (list(config.CLAUDE_ALLOWED_TOOLS)
                   + [f"{tool}({c})" for c in config.CLAUDE_ALLOWED_COMMANDS
                      for tool in ("Bash", "PowerShell")]
                   + allowed_rules(self.project))
        if allowed:
            cmd += ["--allowedTools", *allowed]
        denied = [f"{tool}({c})" for c in config.CLAUDE_DENIED_COMMANDS
                  for tool in ("Bash", "PowerShell")]
        if denied:
            cmd += ["--disallowedTools", *denied]

        turn = self.current
        result, stderr_tail = None, collections.deque(maxlen=30)
        texts = []  # everything Claude said, in case the result event never comes
        try:
            self._proc = subprocess.Popen(
                cmd, cwd=cwd, env=_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
            self._proc.stdin.write(message.encode("utf-8"))
            self._proc.stdin.close()
            threading.Thread(target=_drain, args=(self._proc.stderr, stderr_tail),
                             daemon=True).start()
            killer = threading.Timer(config.CLAUDE_TURN_SECONDS, self._time_out)
            killer.daemon = True
            killer.start()
            try:
                for raw in self._proc.stdout:
                    line = raw.decode("utf-8", errors="replace").strip().lstrip("﻿")
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("type") == "result":
                        result = event
                    else:
                        self._event(event, texts)
            finally:
                killer.cancel()
            self._proc.wait()
        except Exception as e:
            result = {"is_error": True, "result": f"Couldn't run Claude Code: {e}"}

        after = workspace.snapshot(self.project)
        seconds = round(time.time() - turn["started"], 1)
        if result is None:
            why = ("stopped before it finished" if self._stopped else
                   " ".join(stderr_tail).strip() or "Claude Code exited without replying")
            result = {"is_error": True, "result": "\n\n".join(texts) + f"\n\n({why})"}
        reply = str(result.get("result") or "\n\n".join(texts) or "(no reply)").strip()
        if result.get("session_id"):
            self.session_id = result["session_id"]
        cost = float(result.get("total_cost_usd") or 0.0)
        self.cost_usd += cost
        finished = {
            "sent": turn["sent"], "reply": reply, "at": time.time(), "seconds": seconds,
            "is_error": bool(result.get("is_error")) or result.get("subtype", "success") != "success",
            "stopped": self._stopped,
            "cost_usd": round(cost, 4), "claude_turns": result.get("num_turns"),
            "files": workspace.changes(before, after),
            "blocked": [_denial(d) for d in result.get("permission_denials") or []],
        }
        if "No conversation found" in reply:
            finished["hint"] = ("This session's history is gone. Send again with fresh=true "
                                "and re-explain the task.")
        self.turns.append(finished)
        self.last = finished
        self.current = None
        self._proc = None
        self.status = FAILED if finished["is_error"] and not finished["stopped"] else IDLE
        self._note(f"replied in {seconds}s" + (" (error)" if finished["is_error"] else ""))
        _log_exchange(self, finished)
        save(self.project)
        self._done.set()

    def _time_out(self):
        self._note(f"no reply after {config.CLAUDE_TURN_SECONDS}s — stopping it")
        self.stop()

    def _event(self, event, texts):
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init" and event.get("session_id"):
            self.session_id = self.session_id or event["session_id"]
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    texts.append(block["text"].strip())
                    self._note("says: " + _clip(block["text"].strip().splitlines()[0], 140))
                elif block.get("type") == "tool_use":
                    self._note(_describe(block.get("name"), block.get("input") or {}))
        elif kind == "user":
            content = (event.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result" and block.get("is_error"):
                    self._note("a step failed: " + _clip(_text_of(block.get("content")), 140))

    def _note(self, text):
        line = f"{time.strftime('%H:%M:%S')} {text}"
        self.activity.append(line)
        if self.on_activity:
            try:
                self.on_activity(self, text)
            except Exception:
                pass


def _drain(stream, tail):
    for raw in stream:
        tail.append(raw.decode("utf-8", errors="replace").strip())


def _describe(tool, args):
    """One line for one of Claude's steps: "edits src/app.py"."""
    path = args.get("file_path") or args.get("path") or args.get("notebook_path")
    if path:
        path = os.path.basename(str(path)) if len(str(path)) > 60 else str(path)
    verbs = {"Read": "reads", "Edit": "edits", "MultiEdit": "edits", "Write": "writes",
             "NotebookEdit": "edits"}
    if tool in verbs and path:
        return f"{verbs[tool]} {path}"
    if tool in ("Bash", "PowerShell"):
        return "runs: " + _clip(str(args.get("command", "")).splitlines()[0] if args.get("command") else "", 120)
    if tool in ("Grep", "Glob"):
        return f"searches for {_clip(str(args.get('pattern', '')), 80)}"
    if tool in ("Task", "Agent"):
        return "starts a helper: " + _clip(str(args.get("description", "")), 80)
    if tool == "TodoWrite":
        return "updates its to-do list"
    if tool in ("WebFetch", "WebSearch"):
        return f"looks up {_clip(str(args.get('url') or args.get('query') or ''), 80)}"
    return f"uses {tool}"


def _denial(d):
    args = d.get("tool_input") or {}
    what = args.get("command") or args.get("file_path") or json.dumps(args)[:120]
    return f"{d.get('tool_name')}: {_clip(str(what), 160)}"


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content)


def _clip(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[:n - 1] + "…"


# -- the store --------------------------------------------------------------------
_lock = threading.RLock()
_sessions = {}   # project -> {name: Session}
_extra = {}      # project -> extra permission rules the user approved


def _meta_path(project):
    return os.path.join(workspace.META, f"{workspace.slug(project)}.json")


def _load(project):
    project = workspace.slug(project)
    with _lock:
        if project in _sessions:
            return _sessions[project]
        found = {}
        try:
            with open(_meta_path(project), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        for name, s in (data.get("sessions") or {}).items():
            found[name] = Session(project, name, s.get("session_id"), s.get("created"),
                                  s.get("cost_usd", 0.0), s.get("turns"))
            found[name].last = found[name].turns[-1] if found[name].turns else None
        _sessions[project] = found
        _extra[project] = list(data.get("allowed") or [])
        return found


def save(project):
    project = workspace.slug(project)
    with _lock:
        data = {"sessions": {n: s.to_json() for n, s in _load(project).items()},
                "allowed": _extra.get(project, [])}
        os.makedirs(workspace.META, exist_ok=True)
        tmp = _meta_path(project) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, _meta_path(project))


def session(project, name="main", create=True):
    name = workspace.slug(name or "main")
    with _lock:
        found = _load(project)
        if name not in found:
            if not create:
                raise ValueError(f"No Claude session called '{name}' in this project. "
                                 f"Sessions: {', '.join(found) or 'none yet'}.")
            found[name] = Session(workspace.slug(project), name)
        return found[name]


def sessions(project):
    return list(_load(project).values())


def allowed_rules(project):
    _load(project)
    return list(_extra.get(workspace.slug(project), []))


def allow(project, rule):
    """Let Claude use `rule` (e.g. "Bash(cargo *)") in this project from now on."""
    _load(project)
    with _lock:
        rules = _extra.setdefault(workspace.slug(project), [])
        if rule not in rules:
            rules.append(rule)
        save(project)
    return rules


def stop_all(project=None):
    with _lock:
        pools = [_sessions.get(workspace.slug(project), {})] if project else list(_sessions.values())
        for pool in pools:
            for s in pool.values():
                s.stop()


def _log_exchange(s, turn):
    """The whole exchange, appended to a readable markdown log."""
    folder = os.path.join(workspace.LOGS, s.project)
    os.makedirs(folder, exist_ok=True)
    files = turn["files"]
    changed = ", ".join(files["created"] + files["modified"]) or "none"
    with open(os.path.join(folder, f"{s.name}.md"), "a", encoding="utf-8") as f:
        f.write(f"\n\n---\n\n## {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(turn['at']))}"
                f" · {turn['seconds']}s · ${turn['cost_usd']}\n\n"
                f"### BananaGent\n\n{turn['sent']}\n\n### Claude\n\n{turn['reply']}\n\n"
                f"_Files changed: {changed}_\n")
        if turn["blocked"]:
            f.write(f"_Refused permission: {'; '.join(turn['blocked'])}_\n")


if __name__ == "__main__":
    # .venv\Scripts\python.exe claude_code.py          which claude.exe, and signed in?
    # .venv\Scripts\python.exe claude_code.py login    sign it in (opens the browser)
    import sys

    exe = find_exe()
    print(f"claude.exe: {exe or 'not found'}")
    if exe and sys.argv[1:] == ["login"]:
        sys.exit(subprocess.call([exe, "auth", "login"], env=_env()))
    if exe:
        print(auth_status().get("problem") or "signed in — the coding agent can use it")
