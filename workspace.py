"""The sandbox: a folder of coding projects the coding agent owns.

    sandbox/
        snake-game/          one folder per project — where Claude Code works
        budget-cli/
        .banana/             bookkeeping, outside every project so Claude
            snake-game.json  never trips over it: Claude sessions, permissions
            logs/            every exchange with Claude, written out in full

Everything here takes a project name and a path inside it. Paths are resolved
against the project folder and refused if they land outside the sandbox, so a
model that writes "..\\..\\Windows" gets an error instead of a file. That's a
guard against mistakes, not a security boundary: sandbox_run executes real
commands, which can reach anywhere the user can. Commands that look like they
reach outside, delete a lot, change the system or publish something are caught
by risky() and go to the user for a yes first.
"""

import fnmatch
import json
import os
import re
import subprocess
import time

import config

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(APP, config.SANDBOX_DIR))
META = os.path.join(ROOT, ".banana")
LOGS = os.path.join(META, "logs")

# Never listed, searched or diffed: dependencies and caches drown out the
# files that matter, and .banana is ours.
SKIP = {".git", ".banana", "node_modules", ".venv", "venv", "env", "__pycache__",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next", "dist", "build",
        ".idea", ".vscode"}
MAX_WRITE_CHARS = 400_000
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# -- projects -----------------------------------------------------------------
def slug(name):
    """A folder-safe project name: "My Snake Game!" -> "my-snake-game"."""
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    s = "-".join(s.split("-")[:6])[:40].strip("-")
    return s or "project"


def project_dir(project, create=True):
    path = os.path.join(ROOT, slug(project))
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def projects():
    """Every project, most recently touched first."""
    if not os.path.isdir(ROOT):
        return []
    out = []
    for name in os.listdir(ROOT):
        path = os.path.join(ROOT, name)
        if name.startswith(".") or not os.path.isdir(path):
            continue
        files = list(_walk(path))
        newest = max((os.path.getmtime(f) for f in files), default=os.path.getmtime(path))
        entry = {"name": name, "files": len(files),
                 "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(newest)),
                 "_mtime": newest}
        live = deployments().get(name)
        if live:
            entry["url"] = live["url"]
        out.append(entry)
    out.sort(key=lambda p: -p["_mtime"])
    for p in out:
        del p["_mtime"]
    return out


# -- deploying ------------------------------------------------------------------
DEPLOYS = os.path.join(META, "deployments.json")


def deployments():
    """{project: {"url", "at"}} for every project that's been put online."""
    try:
        with open(DEPLOYS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def record_deployment(project, url):
    """Remember where a project is live, so later edits redeploy to the same
    place and the assistant can hand the link back."""
    url = str(url).strip()
    if not re.match(r"https?://", url):
        raise ValueError("The live URL should start with https://.")
    data = deployments()
    data[slug(project)] = {"url": url, "at": time.strftime("%Y-%m-%d %H:%M")}
    os.makedirs(META, exist_ok=True)
    tmp = DEPLOYS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, DEPLOYS)


def _fence_off_git():
    """Make the sandbox a git repository of its own, with no remote.

    `vercel link` walks up to the nearest .git and connects the new Vercel
    project to that repo's GitHub remote — and the nearest one, for a project
    without its own, is BananaGent's. Measured: it wired a test app to
    pushes on the bananagent repo. An empty repo here stops the walk first."""
    if os.path.isdir(os.path.join(ROOT, ".git")):
        return
    os.makedirs(ROOT, exist_ok=True)
    subprocess.run(["git", "init", "-q", ROOT], capture_output=True, creationflags=_NO_WINDOW)


def cloud_env():
    """Our environment plus the Vercel and Neon keys from secrets.env — for
    Claude's runs and sandbox_run only, so the CLIs work with no login."""
    env = dict(os.environ)
    try:
        with open(os.path.join(APP, config.SECRETS_FILE), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    # The Gmail login is send_email's alone (endpoints/mail.py),
                    # and the Google sign-in client and the Discord bot's token
                    # are integrations/' alone.
                    if key.strip().startswith(("GMAIL_", "GOOGLE_", "DISCORD_")):
                        continue
                    env[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    # The CLIs live in tools/ (npm install --prefix tools), not a global
    # install: one made from inside the Claude desktop app lands in that app's
    # private copy of AppData, which BananaGent started at login never sees.
    _fence_off_git()
    bins = os.path.join(APP, "tools", "node_modules", ".bin")
    if os.path.isdir(bins):
        env["PATH"] = bins + os.pathsep + env.get("PATH", "")
    env.setdefault("VERCEL_TELEMETRY_DISABLED", "1")
    env.setdefault("NEON_ORG_ID", config.NEON_ORG_ID)
    return env


# -- paths ------------------------------------------------------------------
def resolve(project, path="."):
    """An absolute path for `path`, relative to the project's folder.

    Absolute paths are accepted only inside the sandbox; anything that ends
    up outside it (through .., a drive letter or a link) is refused.
    """
    base = project_dir(project)
    path = str(path or ".").strip().strip('"').strip("'")
    full = os.path.realpath(os.path.join(base, os.path.expanduser(path)))
    root = os.path.realpath(ROOT)
    if os.path.commonpath([full.lower(), root.lower()]) != root.lower():
        raise ValueError(f"'{path}' is outside the sandbox. Everything you touch has to "
                         f"be inside {root}.")
    if os.path.commonpath([full.lower(), os.path.realpath(META).lower()]) == \
            os.path.realpath(META).lower():
        raise ValueError("That's BananaGent's own bookkeeping folder; leave it alone.")
    return full


def shown(project, full):
    """How a path is shown to the model: relative to the project, / separated."""
    rel = os.path.relpath(full, project_dir(project))
    return "." if rel == "." else rel.replace("\\", "/")


def _walk(top):
    for here, dirs, files in os.walk(top):
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        for f in sorted(files):
            yield os.path.join(here, f)


# -- reading ------------------------------------------------------------------
def tree(project, path=".", depth=3, limit=250):
    """The folder as an indented listing, files with their sizes."""
    top = resolve(project, path)
    if not os.path.exists(top):
        raise FileNotFoundError(f"{path} doesn't exist yet.")
    if os.path.isfile(top):
        return f"{shown(project, top)}  ({_size(os.path.getsize(top))})"
    lines, count = [], 0
    for here, dirs, files in os.walk(top):
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        level = os.path.relpath(here, top).count(os.sep) + (0 if here == top else 1)
        if level >= depth:
            dirs[:] = []
        indent = "  " * level
        if here != top:
            lines.append(f"{'  ' * (level - 1)}{os.path.basename(here)}/")
        for f in sorted(files):
            count += 1
            if count <= limit:
                lines.append(f"{indent}{f}  ({_size(os.path.getsize(os.path.join(here, f)))})")
    if count > limit:
        lines.append(f"… and {count - limit} more files")
    return "\n".join(lines) or "(empty)"


def read(project, path, start=1, lines=250):
    """Part of a text file, numbered, so edits and feedback can cite lines."""
    full = resolve(project, path)
    if not os.path.isfile(full):
        raise FileNotFoundError(f"{path} isn't a file.")
    with open(full, encoding="utf-8", errors="replace") as f:
        text = f.read()
    all_lines = text.splitlines()
    start = max(1, int(start or 1))
    end = min(len(all_lines), start + max(1, int(lines or 250)) - 1)
    body = "\n".join(f"{n:>5}  {all_lines[n - 1]}" for n in range(start, end + 1))
    return {"path": shown(project, full), "total_lines": len(all_lines),
            "showing": f"{start}-{end}", "text": body}


def search(project, pattern, path=".", glob=None, limit=80):
    """Lines matching a regex (or plain text, if it isn't one) across files."""
    top = resolve(project, path)
    try:
        rx = re.compile(pattern, re.I)
    except re.error:
        rx = re.compile(re.escape(pattern), re.I)
    hits = []
    files = [top] if os.path.isfile(top) else _walk(top)
    for f in files:
        if glob and not fnmatch.fnmatch(os.path.basename(f), glob):
            continue
        if os.path.getsize(f) > 2_000_000:
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if rx.search(line):
                        hits.append(f"{shown(project, f)}:{n}: {line.strip()[:200]}")
                        if len(hits) >= limit:
                            return {"matches": hits, "note": f"stopped at {limit}"}
        except (UnicodeDecodeError, OSError):
            continue  # binary, or locked
    return {"matches": hits or ["(no matches)"]}


# -- writing ------------------------------------------------------------------
def write(project, path, text):
    full = resolve(project, path)
    text = str(text)
    if len(text) > MAX_WRITE_CHARS:
        raise ValueError(f"That's {len(text)} characters; one write is capped at {MAX_WRITE_CHARS}.")
    existed = os.path.exists(full)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return {"path": shown(project, full), "created": not existed,
            "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1)}


def edit(project, path, find, replace):
    """Swap one exact passage. It has to appear exactly once."""
    full = resolve(project, path)
    if not os.path.isfile(full):
        raise FileNotFoundError(f"{path} isn't a file.")
    with open(full, encoding="utf-8", newline="") as f:
        text = f.read()
    find, replace = str(find), str(replace)
    # The model writes \n; a file made on Windows may use \r\n. Match either.
    if "\r\n" in text and "\r\n" not in find and find not in text:
        find, replace = find.replace("\n", "\r\n"), replace.replace("\n", "\r\n")
    count = text.count(find) if find else 0
    if count == 0:
        raise ValueError(f"That text isn't in {path}. Read the file and copy the exact "
                         f"wording, spaces and all.")
    if count > 1:
        raise ValueError(f"That text appears {count} times in {path}; include more of the "
                         f"lines around it so it matches only once.")
    with open(full, "w", encoding="utf-8", newline="") as f:
        f.write(text.replace(find, replace, 1))
    return {"path": shown(project, full), "replaced": 1}


# -- what changed ---------------------------------------------------------------
def snapshot(project):
    """Every file's modification time and size, to diff against later."""
    base = project_dir(project)
    out = {}
    for f in _walk(base):
        try:
            st = os.stat(f)
        except OSError:
            continue
        out[shown(project, f)] = (st.st_mtime, st.st_size)
    return out


def changes(before, after):
    created = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(p for p in set(before) & set(after) if before[p] != after[p])
    return {"created": created, "modified": modified, "deleted": deleted}


# -- running things -----------------------------------------------------------
# Each is a reason a command goes to the user before it runs. A keyword pass,
# deliberately blunt: a false alarm costs one click, a miss could cost a folder.
_CMD = r"(?:^|[;&|(\n{]\s*)"  # where a command starts, so "--kill-switch" isn't "kill"
_RISKS = [
    (_CMD + r"(?:remove-item|rm|ri|del|erase|rmdir|rd)\b[^|;&]*(?:-r\b|-recurse|-rf|-fr|/s\b)",
     "deletes a folder and everything in it"),
    (r"\b(?:shutdown|restart-computer|stop-computer|format|diskpart|bcdedit|reg\s+(?:add|delete)"
     r"|set-executionpolicy|schtasks|sc(?:\.exe)?\s+(?:create|delete|config)|netsh)\b",
     "changes the system"),
    (_CMD + r"(?:stop-process|taskkill|kill)\b", "stops other programs"),
    (r"\b(?:git\s+push|npm\s+publish|twine\s+upload|gh\s+\w+|docker\s+push)\b",
     "publishes something outside this computer"),
    (r"\bvercel\s+(?:(?:projects?\s+)?(?:rm|remove)|domains\s+buy)\b"
     r"|\bneon\s+(?:projects?|databases?|db|branch(?:es)?)\s+delete\b",
     "takes down an app or database that's online, or spends money"),
    (r"\b(?:invoke-expression|iex)\b|\|\s*(?:ba)?sh\b", "runs a downloaded script"),
    (r"\b(?:winget|choco|scoop)\s+install\b|\bnpm\s+(?:i|install)\s+(?:-g|--global)\b",
     "installs software on the whole computer"),
    (r"(?:^|[\s\"'=(])~[\\/]|\$home\b|(?:\$env:|%)(?:userprofile|homepath|homedrive|appdata"
     r"|localappdata|programfiles|programdata|windir|systemroot|onedrive)",
     "reaches into the user's folders"),
    (r"(?:^|[\s\"'=(])\.\.[\\/]", "reaches outside the project folder"),
]


def risky(project, command):
    """Why this command should be approved before it runs, or None."""
    text = str(command).strip()
    low = text.lower()
    for rx, why in _RISKS:
        if re.search(rx, low):
            return why
    # pip into a Python that isn't a virtual environment changes it for everyone.
    if re.search(r"\bpip3?\s+install\b", low) and not re.search(r"\.?venv|virtualenv", low):
        return "installs Python packages outside a virtual environment"
    root = os.path.realpath(ROOT).lower()
    for m in re.finditer(r"(?<![\w])([a-zA-Z]:[\\/][^\s\"'|;&]*)", text):
        if not os.path.realpath(m.group(1)).lower().startswith(root):
            return f"touches {m.group(1)}, outside the sandbox"
    return None


def run(project, command, timeout=None):
    """Run one PowerShell command in the project folder and return its output.

    Output is kept from both ends when it's long — the start says what ran,
    the end is where errors land. A command still going at the timeout is
    killed along with anything it started.
    """
    timeout = max(5, min(int(timeout or config.SANDBOX_RUN_SECONDS), config.SANDBOX_RUN_MAX_SECONDS))
    cwd = project_dir(project)
    script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
              "$ProgressPreference = 'SilentlyContinue'; " + str(command))
    started = time.time()
    proc = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=cwd, env=cloud_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, creationflags=_NO_WINDOW)
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc.pid)
        out, _ = proc.communicate()
    text = out.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
    limit = config.SANDBOX_OUTPUT_CHARS
    if len(text) > limit:
        head = limit // 4
        text = (text[:head] + f"\n… ({len(text) - limit} characters cut) …\n"
                + text[-(limit - head):])
    result = {"exit_code": proc.returncode, "seconds": round(time.time() - started, 1),
              "output": text or "(no output)"}
    if timed_out:
        result["timed_out"] = (f"Killed after {timeout}s. Long-running things like servers "
                               f"can't be left running from here — test them another way.")
    return result


def kill_tree(pid):
    """End a process and everything it started. Windows won't do the second
    half on its own, and a killed shell's children would otherwise live on."""
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                   creationflags=_NO_WINDOW)


def _size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
