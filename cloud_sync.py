"""Keeping every computer BananaGent runs on the same, through GitHub.

Two things are synced, by one background thread:

  * The code — this folder's own repo (origin, public). Changes made here are
    committed and pushed once the files have sat untouched for a while
    (SYNC_CODE_QUIET_SECONDS) and every changed .py file still compiles, so a
    half-finished edit or a syntax error never reaches the other computers.
    Updates from GitHub are pulled whenever this copy has nothing of its own
    waiting; if they change anything, the app restarts itself to run them
    (once no reply or agent is mid-job), installing requirements.txt first
    if that changed.

  * The memory — long-term MEMORY.md, the conversation, and the business
    notes (SYNC_MEMORY_FILES). They hold personal details, so they never go
    near the public code repo: they're kept in a private repo of their own
    (SYNC_MEMORY_REMOTE), through a separate git directory, .memory-sync.git,
    that tracks only those files where they already are. Every change is
    committed and pushed within SYNC_MEMORY_SECONDS, and the other computers'
    changes are pulled in just as often.

Two computers changing the memory at once is expected, not an error, so
nothing is ever thrown away. Markdown merges line by line, keeping both sides'
new lines (git's union merge: MEMORY.md is one fact per line). JSON merges by
item — the conversation as user/reply pairs in time order, the business lists
item by item — through `merge-json` below, which git calls as a merge driver.
Anything those can't settle keeps this computer's version, and the other one
stays in the memory repo's history.

The triggers (triggers.json) are deliberately left out: a reminder synced to
two computers would go off twice, and a file watch names a path on one of them.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
MEMORY_GIT = os.path.join(ROOT, ".memory-sync.git")


# =============================================================================
# The JSON merge driver: git runs `cloud_sync.py merge-json BASE OURS THEIRS`
# and takes whatever is left in OURS. Pure: no config, nothing else imported.
# =============================================================================

_MISSING = object()


def merge_json(base, ours, theirs):
    """Three-way merge of two edits of one JSON value. `base` is _MISSING when
    the two sides share no history (two computers that started separately)."""
    if ours == theirs:
        return ours
    if base is not _MISSING and base == ours:
        return theirs
    if base is not _MISSING and base == theirs:
        return ours
    if isinstance(ours, dict) and isinstance(theirs, dict):
        b = base if isinstance(base, dict) else {}
        out = {}
        for key in list(ours) + [k for k in theirs if k not in ours]:
            o, t, bk = ours.get(key, _MISSING), theirs.get(key, _MISSING), b.get(key, _MISSING)
            if o is _MISSING:
                if bk is _MISSING or t != bk:  # new on their side, or changed there
                    out[key] = t
            elif t is _MISSING:
                if bk is _MISSING or o != bk:  # new on ours, or we changed it
                    out[key] = o
            else:
                out[key] = merge_json(bk, o, t)
        return out
    if isinstance(ours, list) and isinstance(theirs, list):
        b = base if isinstance(base, list) else []
        if _is_conversation(ours) or _is_conversation(theirs):
            pairs = _merge_items(_pairs(b), _pairs(ours), _pairs(theirs))
            pairs.sort(key=lambda p: p[0].get("at") or 0)
            return [m for p in pairs for m in p]
        return _merge_items(b, ours, theirs)
    return ours  # two different values for one thing: this computer's wins


def _merge_items(base, ours, theirs):
    """Lists as sets of items: everything either side added, minus what either
    side removed. An item edited on one side is its old self removed and its
    new self added; edited differently on both, both versions are kept."""
    key = lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False)
    b, o = {key(x) for x in base}, {key(x) for x in ours}
    removed_there = b - {key(x) for x in theirs}
    out = [x for x in ours if key(x) not in removed_there]
    out += [x for x in theirs if key(x) not in o and key(x) not in b]
    return out


def _is_conversation(items):
    return bool(items) and all(isinstance(m, dict) and "role" in m for m in items)


def _pairs(messages):
    """memory/conversation.json as (message, reply) pairs, so a merge can never
    split a reply from what it answered."""
    return [tuple(messages[i:i + 2]) for i in range(0, len(messages) - 1, 2)]


def _merge_driver(base_path, ours_path, theirs_path):
    def load(path):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            return _MISSING
        return json.loads(text) if text.strip() else _MISSING
    base, ours, theirs = load(base_path), load(ours_path), load(theirs_path)
    if ours is _MISSING or theirs is _MISSING:
        return 1
    merged = merge_json(base, ours, theirs)
    with open(ours_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(merged, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "merge-json":
        try:
            sys.exit(_merge_driver(*sys.argv[2:]))
        except Exception:
            sys.exit(1)  # git then treats it as a conflict, and the sync keeps ours
    print("usage: cloud_sync.py merge-json BASE OURS THEIRS")
    sys.exit(2)


# =============================================================================
# The sync itself
# =============================================================================

import config  # noqa: E402  (after the merge driver, which mustn't need it)

HOST = socket.gethostname()
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_thread = None
_stop = threading.Event()
_wake = threading.Event()
_on_update = None
_said = {}  # topic -> the last thing logged about it, so repeats stay quiet
_status = {"code": "not started", "memory": "not started"}


def start(on_update=None):
    """Start syncing in the background. `on_update(files)` is called after an
    update to the code is pulled from GitHub, with the files it changed."""
    global _thread, _on_update
    _on_update = on_update
    if not (config.SYNC_CODE or config.SYNC_MEMORY):
        print("[sync] off (SYNC_CODE and SYNC_MEMORY are both False)")
        return
    if _git_exe() is None:
        print("[sync] git isn't installed, so nothing is synced")
        return
    if config.SYNC_MEMORY:
        # Once now, before the app reads its memory, so it opens on the latest
        # from the other computers — not on what this one had when it closed.
        _guard("memory", lambda: _memory_once(timeout=20))
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        _thread = threading.Thread(target=_run, name="cloud-sync", daemon=True)
        _thread.start()


def stop(flush=True):
    """Stop the thread; with `flush`, sync the memory one last time, so closing
    the app on one computer leaves the others with everything."""
    _stop.set()
    _wake.set()
    if _thread is not None:
        _thread.join(timeout=10)
    if flush and config.SYNC_MEMORY:
        try:
            _memory_once(timeout=20)
        except Exception as e:
            print(f"[sync] memory couldn't be pushed on the way out: {e}")


def sync_now():
    """Run both syncs at the next chance rather than waiting for the timer."""
    _wake.set()


def status():
    return dict(_status)


def _run():
    next_code = next_memory = 0.0
    while not _stop.is_set():
        now = time.time()
        woken = _wake.is_set()
        _wake.clear()
        if config.SYNC_MEMORY and (woken or now >= next_memory):
            _guard("memory", _memory_once)
            next_memory = time.time() + config.SYNC_MEMORY_SECONDS
        if config.SYNC_CODE and (woken or now >= next_code):
            _guard("code", _code_once)
            next_code = time.time() + config.SYNC_CODE_SECONDS
        _wake.wait(max(1.0, min(next_code if config.SYNC_CODE else 1e18,
                                next_memory if config.SYNC_MEMORY else 1e18) - time.time()))


def _guard(topic, step):
    try:
        step()
    except Exception as e:
        _status[topic] = f"error: {e}"
        _say(topic, f"{topic} sync failed: {type(e).__name__}: {e}")
        if not isinstance(e, GitError):
            traceback.print_exc()


def _say(topic, text):
    """Log `text` unless it's what was last said about `topic`."""
    if _said.get(topic) != text:
        _said[topic] = text
        print(f"[sync] {text}")


# -- git -----------------------------------------------------------------------
class GitError(RuntimeError):
    pass


def _git_exe():
    found = shutil.which("git")
    if found:
        return found
    for path in (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files (x86)\Git\cmd\git.exe"):
        if os.path.exists(path):
            return path
    return None


def _git(*args, check=True, timeout=120, git_dir=None):
    cmd = [_git_exe()]
    if git_dir:
        cmd += ["--git-dir", git_dir, "--work-tree", ROOT]
    cmd += list(args)
    # Never wait on a prompt nobody can see; Git Credential Manager may still
    # open its own sign-in window the first time on a new computer.
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       creationflags=_NO_WINDOW)
    if check and p.returncode != 0:
        msg = (p.stderr or p.stdout).strip().splitlines()
        raise GitError(f"git {args[0]}: {msg[-1] if msg else f'exit {p.returncode}'}")
    return p


def _out(*args, **kw):
    return _git(*args, **kw).stdout.strip()


def _identity():
    """Who the commits are by: this repo's git identity (or the computer's),
    else BananaGent itself — a computer with none set would fail every commit."""
    name = _git("config", "user.name", check=False).stdout.strip() or "BananaGent"
    email = _git("config", "user.email", check=False).stdout.strip() or f"bananagent@{HOST}.local"
    return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


# =============================================================================
# Code: this folder's own repo
# =============================================================================

def _code_once():
    branch = _out("rev-parse", "--abbrev-ref", "HEAD")
    upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
    if branch == "HEAD" or upstream.returncode != 0:
        _status["code"] = "not on a branch with a GitHub upstream"
        return _say("code", "code isn't on a branch that tracks GitHub; not syncing it")
    if os.path.exists(os.path.join(ROOT, ".git", "rebase-merge")) or \
            os.path.exists(os.path.join(ROOT, ".git", "MERGE_HEAD")):
        _status["code"] = "waiting: a merge or rebase is in progress"
        return _say("code", "a merge or rebase is in progress here; code sync waits for it")

    dirty = _dirty_files()
    if dirty:
        _commit_code(dirty)
        dirty = _dirty_files()

    _git("fetch", "--quiet", "origin")
    ahead, behind = (int(n) for n in _out("rev-list", "--left-right", "--count",
                                          "HEAD...@{u}").split())
    if behind:
        if dirty:
            _status["code"] = f"{behind} update(s) on GitHub, waiting for local edits to settle"
            return _say("code", f"{behind} update(s) on GitHub wait until the edits "
                                f"here are committed")
        before = _out("rev-parse", "HEAD")
        how = ("rebase", "@{u}") if ahead else ("merge", "--ff-only", "@{u}")
        p = _git(*_identity(), *how, check=False)
        if p.returncode != 0:
            if ahead:
                _git("rebase", "--abort", check=False)
            _status["code"] = "stuck: local commits conflict with GitHub's"
            return _say("code", "the commits here conflict with GitHub's, so code sync is "
                                "paused; run `git pull --rebase` and settle it by hand")
        changed = [f for f in _out("diff", "--name-only", before, "HEAD").splitlines() if f]
        print(f"[sync] pulled {behind} update(s) from GitHub: {', '.join(changed[:8])}"
              f"{' …' if len(changed) > 8 else ''}")
        _after_pull(changed)
        ahead = int(_out("rev-list", "--count", "@{u}..HEAD"))
    if ahead:
        _git("push", "--quiet", "origin", f"HEAD:{_out('rev-parse', '--abbrev-ref', '@{u}').split('/', 1)[1]}")
        print(f"[sync] pushed {ahead} commit(s) to GitHub")
    _status["code"] = f"in sync ({time.strftime('%H:%M:%S')})"
    _said.pop("code", None)


def _dirty_files():
    """Changed and new files git would commit (gitignored ones aside)."""
    out = _git("status", "--porcelain", "-z", "--untracked-files=all").stdout
    files, entries = [], out.split("\0")
    i = 0
    while i < len(entries):
        e = entries[i]
        if len(e) > 3:
            files.append(e[3:])
            if e[0] in "RC":
                i += 1  # a rename is followed by its old name
        i += 1
    return files


def _commit_code(files):
    newest = max((os.path.getmtime(os.path.join(ROOT, f)) for f in files
                  if os.path.exists(os.path.join(ROOT, f))), default=0)
    quiet = time.time() - newest
    if quiet < config.SYNC_CODE_QUIET_SECONDS:
        _status["code"] = "local edits waiting to settle"
        return
    broken = [f"{f}: {e}" for f in files if f.endswith(".py") for e in [_compiles(f)] if e]
    if broken:
        _status["code"] = "not pushing: a file doesn't compile"
        return _say("code", f"not pushing the changes here — this doesn't compile: {broken[0]}")
    limit = config.SYNC_MAX_FILE_MB * 1024 * 1024
    big = [f for f in files if os.path.exists(os.path.join(ROOT, f))
           and os.path.getsize(os.path.join(ROOT, f)) > limit]
    if big:
        _status["code"] = "not pushing: a file is too big"
        return _say("code", f"not pushing the changes here — {big[0]} is over "
                            f"{config.SYNC_MAX_FILE_MB} MB; gitignore it or raise SYNC_MAX_FILE_MB")
    _git("add", "-A")
    shown = ", ".join(files[:5]) + (f" and {len(files) - 5} more" if len(files) > 5 else "")
    _git(*_identity(), "commit", "--quiet", "-m", f"Update {shown}\n\nSynced automatically from {HOST}.")
    print(f"[sync] committed {len(files)} changed file(s): {shown}")


def _compiles(path):
    """None if the file compiles, else why not. Nothing is written to disk."""
    try:
        with open(os.path.join(ROOT, path), "rb") as f:
            compile(f.read(), path, "exec")
    except FileNotFoundError:
        return None  # deleted
    except SyntaxError as e:
        return f"line {e.lineno}: {e.msg}"
    except Exception as e:
        return str(e)
    return None


def _after_pull(changed):
    if "requirements.txt" in changed:
        py = os.path.join(os.path.dirname(sys.executable), "python.exe")
        if not os.path.exists(py):
            py = sys.executable
        print("[sync] requirements.txt changed; installing")
        p = subprocess.run([py, "-m", "pip", "install", "-q", "-r",
                            os.path.join(ROOT, "requirements.txt")],
                           cwd=ROOT, capture_output=True, text=True, timeout=900,
                           creationflags=_NO_WINDOW)
        print("[sync] requirements installed" if p.returncode == 0 else
              f"[sync] pip install failed: {(p.stderr or p.stdout).strip()[-500:]}")
    if changed and config.SYNC_RESTART_ON_UPDATE and _on_update is not None:
        _on_update(changed)


# =============================================================================
# Memory: a private repo of its own, tracking only SYNC_MEMORY_FILES in place
# =============================================================================

def _mgit(*args, **kw):
    return _git(*args, git_dir=MEMORY_GIT, **kw)


def _memory_setup():
    if not os.path.isdir(MEMORY_GIT):
        _git("init", "--quiet", "--initial-branch=main", "--bare", MEMORY_GIT)
        _mgit("config", "core.bare", "false")
    # Rewritten every start: the driver's path to python is this computer's.
    py = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(py):
        py = sys.executable
    driver = f'"{py.replace(os.sep, "/")}" "{os.path.join(ROOT, "cloud_sync.py").replace(os.sep, "/")}" merge-json %O %A %B'
    remote = _git("config", "--get", "remote.origin.url", git_dir=MEMORY_GIT, check=False).stdout.strip()
    if remote != config.SYNC_MEMORY_REMOTE:
        if remote:
            _mgit("remote", "set-url", "origin", config.SYNC_MEMORY_REMOTE)
        else:
            _mgit("remote", "add", "origin", config.SYNC_MEMORY_REMOTE)
    for key, value in (("merge.bananajson.name", "BananaGent JSON merge"),
                       ("merge.bananajson.driver", driver),
                       ("core.autocrlf", "false"),
                       ("status.showUntrackedFiles", "no")):
        _mgit("config", key, value)
    info = os.path.join(MEMORY_GIT, "info")
    os.makedirs(info, exist_ok=True)
    with open(os.path.join(info, "attributes"), "w", encoding="utf-8") as f:
        f.write("*.md merge=union\n*.json merge=bananajson\n")


_ready = False


def _memory_once(timeout=60):
    global _ready
    if not config.SYNC_MEMORY_REMOTE:
        _status["memory"] = "off: no SYNC_MEMORY_REMOTE"
        return _say("memory", "memory sync is off: SYNC_MEMORY_REMOTE isn't set")
    if not _ready:
        _memory_setup()
        _ready = True

    p = _mgit("fetch", "--quiet", "origin", check=False, timeout=timeout)
    if p.returncode != 0:
        err = (p.stderr or p.stdout).strip().splitlines()
        _status["memory"] = "can't reach the memory repo"
        with _data_locks():
            _commit_memory()  # still committed here, so it's ready to go once it can
        return _say("memory", f"can't reach the memory repo {config.SYNC_MEMORY_REMOTE} "
                              f"({err[-1] if err else 'no answer'}); kept here and "
                              f"retrying. Is it created, and private?")
    remote_has = _mgit("rev-parse", "--verify", "--quiet", "origin/main", check=False).returncode == 0
    with _data_locks():
        _commit_memory(joining=remote_has)
        if remote_has:
            _merge_memory()
    has_local = _mgit("rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode == 0
    if not has_local:
        _status["memory"] = "nothing to sync yet"
        return
    ahead = int(_mgit("rev-list", "--count", "origin/main..HEAD").stdout.strip()) if remote_has else 1
    if ahead:
        p = _mgit("push", "--quiet", "origin", "HEAD:main", check=False, timeout=timeout)
        if p.returncode != 0:
            err = (p.stderr or p.stdout).strip().splitlines()
            _status["memory"] = "can't push to the memory repo"
            return _say("memory", f"couldn't push memory ({err[-1] if err else 'no answer'}); "
                                  f"kept here and retrying")
    _status["memory"] = f"in sync ({time.strftime('%H:%M:%S')})"
    _said.pop("memory", None)


def _memory_paths():
    return [p for p in config.SYNC_MEMORY_FILES if os.path.exists(os.path.join(ROOT, p))]


def _commit_memory(joining=False):
    paths = _memory_paths()
    if joining and _mgit("rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode:
        # This computer's first sync, and the others already have a memory: a
        # MEMORY.md that's still the blank template isn't worth merging in.
        import memory
        try:
            with open(memory.LONG_TERM, encoding="utf-8") as f:
                blank = f.read().strip() == memory._TEMPLATE.strip()
        except FileNotFoundError:
            blank = False
        if blank:
            os.remove(memory.LONG_TERM)
            paths = _memory_paths()
    if not paths:
        return
    _mgit("add", "--force", "--", *paths)  # the app's .gitignore lists them, for the code repo
    if _mgit("diff", "--cached", "--quiet", check=False).returncode == 0:
        return  # nothing changed
    names = ", ".join(os.path.basename(p) for p in
                      _mgit("diff", "--cached", "--name-only").stdout.split())
    _mgit(*_identity(), "commit", "--quiet", "-m", f"{names} on {HOST}")


def _merge_memory():
    behind = int(_mgit("rev-list", "--count", "HEAD..origin/main", check=False).stdout.strip() or 0) \
        if _mgit("rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode == 0 else -1
    if behind == 0:
        return
    if behind < 0:
        # A computer with no memory of its own yet: take what's there.
        _mgit("reset", "--quiet", "origin/main")
        _mgit("checkout", "--", ".")
        changed = _memory_paths()
    else:
        before = _out("rev-parse", "HEAD", git_dir=MEMORY_GIT)
        p = _mgit(*_identity(), "merge", "--no-edit", "--allow-unrelated-histories",
                  "-m", f"Merge memory from GitHub on {HOST}", "origin/main", check=False)
        if p.returncode != 0:
            # What the merge drivers couldn't settle keeps this computer's
            # version; the other one is still in the memory repo's history.
            stuck = _mgit("diff", "--name-only", "--diff-filter=U").stdout.split()
            for path in stuck:
                if _mgit("checkout", "--ours", "--", path, check=False).returncode == 0:
                    _mgit("add", "--", path)
                else:
                    _mgit("rm", "--quiet", "--cached", "--", path, check=False)
            _mgit(*_identity(), "commit", "--quiet", "--no-edit")
            print(f"[sync] memory: kept this computer's {', '.join(stuck)} where the two "
                  f"couldn't be merged (the other version is in the memory repo's history)")
        changed = _mgit("diff", "--name-only", before, "HEAD").stdout.split()
    if changed:
        print(f"[sync] memory updated from another computer: {', '.join(changed)}")
        _reload(changed)


class _data_locks:
    """Hold the memory and business locks while git touches their files, so
    nothing is written halfway through a commit or merge."""

    def __enter__(self):
        import business
        import memory
        self.locks = [memory._lock, business._lock]
        for lock in self.locks:
            lock.acquire()

    def __exit__(self, *exc):
        for lock in reversed(self.locks):
            lock.release()


def _reload(changed):
    """Make the running app see files a merge just rewrote. Only the short-term
    window is held in memory; everything else is read from disk each time."""
    if any(p.endswith("conversation.json") for p in changed):
        import memory
        memory.reload()
