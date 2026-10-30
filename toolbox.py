"""The toolbox: tools BananaGent made for itself.

The coding agent builds a script in a sandbox project — a USPTO search, a
unit converter — and then, with tool_make, declares its arguments and hooks
it up. From then on it's an endpoint like any other: in the assistant's tool
list, called with [[call name {...}]], MINOR ones handed to worker subagents.

A made tool is one small JSON file, kept outside every project:

    sandbox/.banana/tools/
        uspto_search.json    {"description": ..., "tier": "minor",
                              "project": "uspto-search", "script": "search.py",
                              "params": {"query": {"type": "string", ...}}, ...}
        removed/             what tool_remove took away, in case it's wanted back

The file is the whole declaration; the script stays in its project, so when
Claude improves the script the tool improves with it. The folder is re-read
whenever a file in it changes (registry.refresh), so a new tool, an edit by
hand, or a removal shows up on the next message with no restart.

How a script is run — the contract the coding agent briefs Claude with:

  * in its project folder, by extension: .py with the project's .venv if it
    has one, else the python on PATH (what Claude tested it with); .js, .mjs,
    .cjs with node; .ps1 with PowerShell;
  * the arguments go in as a JSON object on stdin (and in the environment as
    BANANA_TOOL_ARGS) — or, with pass_args "flags", as --name value;
  * it prints its result to stdout, JSON if it's structured, and fails by
    exiting non-zero with the reason on stderr.

Nothing goes through a shell, so an argument the model wrote can't turn into
a command. That keeps the model's words out of the command line, not the
script out of anything: a made tool runs with the user's own rights, like
everything else Claude writes in the sandbox.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import workspace
from endpoints import registry

DIR = os.path.join(workspace.META, "tools")
REMOVED = os.path.join(DIR, "removed")

NAME = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
TYPES = ("string", "integer", "number", "boolean", "array", "object")
RUNNERS = (".py", ".js", ".mjs", ".cjs", ".ps1")
PASS_ARGS = ("json", "flags")
TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_DESCRIPTION = 1200
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_lock = threading.RLock()
_seen = None  # the folder as it was when last read: ((file, mtime), ...)
_MADE = {}    # name -> manifest, for every tool currently registered from here


# -- keeping the registry in step with the folder -------------------------------
def sync(force=False):
    """Register what's in the folder, drop what's gone. Cheap when nothing
    changed — one listdir — so it runs before every read of the registry."""
    global _seen
    with _lock:
        sig = _signature()
        if sig == _seen and not force:
            return
        _seen = sig
        for name in list(_MADE):
            registry.unregister(name)
        _MADE.clear()
        for fname, _ in sig:
            name = fname[:-5]
            try:
                manifest = _read(name)
                registry.register(_endpoint(manifest))
            except Exception as e:
                # A clash with a built-in name, or a file broken by hand. The
                # rest of the toolbox still loads.
                print(f"[toolbox] skipped {fname}: {e}")
                continue
            _MADE[name] = manifest


def _signature():
    if not os.path.isdir(DIR):
        return ()
    found = []
    for f in os.listdir(DIR):
        path = os.path.join(DIR, f)
        if f.endswith(".json") and os.path.isfile(path):
            found.append((f, os.path.getmtime(path)))
    return tuple(sorted(found))


def _read(name):
    with open(os.path.join(DIR, f"{name}.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    # The file name is the tool's name; a "name" edited inside it can't
    # make two files claim the same tool.
    manifest["name"] = name
    return _checked(manifest)


def _endpoint(manifest):
    params = {n: registry.param(p["type"], p.get("description", ""),
                                required=bool(p.get("required")), enum=p.get("enum"),
                                default=p.get("default"))
              for n, p in manifest["params"].items()}

    def call(**args):
        return run(manifest, args)

    call.__name__ = manifest["name"]
    return registry.Endpoint(call, manifest["name"], manifest["tier"],
                             manifest["description"], params, manifest.get("returns"))


# -- declaring one ------------------------------------------------------------
def _checked(m):
    """A manifest with every field valid and normalised, or ValueError."""
    name = str(m.get("name", "")).strip()
    if not NAME.match(name):
        raise ValueError(f"'{name}' can't be a tool name: use 2-40 lowercase letters, digits "
                         f"and underscores, starting with a letter, like uspto_search.")
    description = " ".join(str(m.get("description") or "").split())
    if not description:
        raise ValueError("Give it a description: what it does and when to use it.")
    if len(description) > MAX_DESCRIPTION:
        raise ValueError(f"The description is {len(description)} characters; keep it under "
                         f"{MAX_DESCRIPTION}. BananaGent reads it before every message.")
    tier = str(m.get("tier") or "").strip().lower()
    if tier not in (registry.MAJOR, registry.MINOR):
        raise ValueError("tier is minor (it only looks things up) or major (it changes "
                         "something).")
    pass_args = str(m.get("pass_args") or "json").strip().lower()
    if pass_args not in PASS_ARGS:
        raise ValueError("pass_args is json or flags.")
    script = str(m.get("script") or "").strip().replace("\\", "/")
    if os.path.splitext(script)[1].lower() not in RUNNERS:
        raise ValueError(f"The script has to be one of {', '.join(RUNNERS)} — '{script}' "
                         f"isn't.")
    return {
        "name": name,
        "description": description,
        "returns": " ".join(str(m.get("returns") or "").split()),
        "tier": tier,
        "project": workspace.slug(m.get("project")),
        "script": script,
        "params": _params(m.get("params")),
        "pass_args": pass_args,
        "timeout": max(5, min(int(m.get("timeout") or TIMEOUT), MAX_TIMEOUT)),
        "created": m.get("created"),
        "updated": m.get("updated"),
    }


def _params(params):
    if params in (None, "", {}):
        return {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError as e:
            raise ValueError(f"params isn't valid JSON: {e}")
    if not isinstance(params, dict):
        raise ValueError('params is an object: {"query": {"type": "string", '
                         '"description": "...", "required": true}}.')
    out = {}
    for pname, spec in params.items():
        if not NAME.match(str(pname)) and not re.match(r"^[a-z]$", str(pname)):
            raise ValueError(f"'{pname}' can't be an argument name: lowercase letters, digits "
                             f"and underscores.")
        if not isinstance(spec, dict):
            raise ValueError(f"Argument '{pname}' needs a type and a description.")
        kind = str(spec.get("type", "string")).lower()
        if kind not in TYPES:
            raise ValueError(f"Argument '{pname}' has type '{kind}'; use one of "
                             f"{', '.join(TYPES)}.")
        about = " ".join(str(spec.get("description") or "").split())
        if not about:
            # BananaGent chooses what to pass from this alone.
            raise ValueError(f"Argument '{pname}' needs a description: what to pass in it.")
        entry = {"type": kind, "description": about, "required": bool(spec.get("required"))}
        if spec.get("enum"):
            entry["enum"] = list(spec["enum"])
        if spec.get("default") is not None:
            entry["default"] = spec["default"]
        out[str(pname)] = entry
    return out


def make(project, name, script, description, tier, params=None, returns=None,
         pass_args="json", timeout=TIMEOUT, test_args=None):
    """Declare a tool, run it once with test_args, and save it only if that
    worked. The same name again replaces it."""
    name = str(name or "").strip().lower()
    existing = registry.get(name)
    if existing is not None and name not in _MADE:
        raise ValueError(f"'{name}' is already one of BananaGent's built-in tools. Pick "
                         f"another name.")
    manifest = _checked({"name": name, "description": description, "tier": tier,
                         "project": project, "script": script, "params": params,
                         "returns": returns, "pass_args": pass_args, "timeout": timeout})
    full = workspace.resolve(manifest["project"], manifest["script"])
    if not os.path.isfile(full):
        raise ValueError(f"There's no {manifest['script']} in {manifest['project']}. Have "
                         f"Claude write it first.")
    manifest["script"] = workspace.shown(manifest["project"], full)

    if test_args is None and manifest["tier"] == registry.MINOR:
        raise ValueError("Test it: pass test_args with real arguments, e.g. "
                         '{"query": "nike"} — or {} if it takes none.')
    tested = None
    if test_args is not None:
        if isinstance(test_args, str):
            test_args = json.loads(test_args or "{}")
        # The tool drops arguments it doesn't declare, which would make a test
        # pass without trying what it was meant to.
        stray = sorted(set(test_args) - set(manifest["params"]))
        if stray:
            raise ValueError(f"test_args has {', '.join(stray)}, which isn't among the "
                             f"declared params ({', '.join(manifest['params']) or 'none'}). "
                             f"Declare it or leave it out of the test.")
        # Through the same Endpoint the model will call, so the defaults,
        # required arguments and type checks are the ones it'll really get.
        tested = _endpoint(manifest).call(test_args)

    now = time.strftime("%Y-%m-%d %H:%M")
    old = _MADE.get(name)
    manifest["created"] = (old or {}).get("created") or now
    manifest["updated"] = now
    os.makedirs(DIR, exist_ok=True)
    path = os.path.join(DIR, f"{name}.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    sync(force=True)
    print(f"[toolbox] {'updated' if old else 'made'} {name} -> "
          f"{manifest['project']}/{manifest['script']}")
    return manifest, tested, old is not None


def remove(name):
    name = str(name or "").strip().lower()
    path = os.path.join(DIR, f"{name}.json")
    if not os.path.isfile(path):
        raise ValueError(f"There's no made tool called '{name}'. Made tools: "
                         f"{', '.join(sorted(_MADE)) or 'none'}.")
    os.makedirs(REMOVED, exist_ok=True)
    os.replace(path, os.path.join(REMOVED, f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.json"))
    sync(force=True)
    print(f"[toolbox] removed {name}")


def made():
    """Every made tool's manifest, by name."""
    sync()
    with _lock:
        return dict(sorted(_MADE.items()))


def signature(manifest):
    bits = [n if p.get("required") else f"{n}?" for n, p in manifest["params"].items()]
    return f"{manifest['name']}({', '.join(bits)})"


# -- running one ----------------------------------------------------------------
def run(manifest, args):
    """Run the tool's script with these arguments and return what it printed —
    parsed, if it's JSON. Raises with the script's own error if it fails."""
    args = {n: _coerce(n, manifest["params"][n], v) for n, v in args.items()
            if n in manifest["params"]}
    project = manifest["project"]
    full = workspace.resolve(project, manifest["script"])
    if not os.path.isfile(full):
        raise RuntimeError(f"{manifest['script']} is gone from the {project} project, so "
                           f"{manifest['name']} can't run. Ask the coding agent to rebuild it.")
    command = _runner(project, full)
    payload = json.dumps(args, ensure_ascii=False)
    if manifest["pass_args"] == "flags":
        command += _flags(manifest["params"], args)
    env = dict(os.environ, BANANA_TOOL_ARGS=payload, PYTHONUTF8="1",
               PYTHONIOENCODING="utf-8")

    proc = subprocess.Popen(command, cwd=workspace.project_dir(project), env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    try:
        out, err = proc.communicate(payload.encode("utf-8"), timeout=manifest["timeout"])
    except subprocess.TimeoutExpired:
        workspace.kill_tree(proc.pid)
        proc.communicate()
        raise RuntimeError(f"{manifest['name']} took longer than {manifest['timeout']}s "
                           f"and was stopped.")
    out = out.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
    err = err.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
    if proc.returncode != 0:
        why = (err or out or "no output")[-1500:]
        raise RuntimeError(f"{manifest['name']} failed (exit code {proc.returncode}): {why}")
    if not out:
        return err[-1500:] if err else "(it ran and printed nothing)"
    try:
        return json.loads(out)
    except ValueError:
        return out


def _coerce(name, spec, value):
    """The value as the declared type. Models write "5" for 5 and "true" for
    true often enough that refusing them would just cost a retry."""
    kind = spec["type"]
    try:
        if kind == "integer" and not isinstance(value, bool):
            value = int(float(value)) if isinstance(value, str) else int(value)
        elif kind == "number" and not isinstance(value, bool):
            value = float(value)
        elif kind == "boolean" and isinstance(value, str):
            low = value.strip().lower()
            if low not in ("true", "false", "yes", "no", "1", "0"):
                raise ValueError(value)
            value = low in ("true", "yes", "1")
        elif kind in ("array", "object") and isinstance(value, str):
            value = json.loads(value)
        elif kind == "string" and not isinstance(value, str):
            value = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{name}' should be {'an' if kind[0] in 'aeiou' else 'a'} {kind}, "
                         f"not {value!r}.")
    if kind == "array" and not isinstance(value, list):
        value = [value]
    if spec.get("enum") and value not in spec["enum"]:
        raise ValueError(f"'{name}' is one of {', '.join(map(str, spec['enum']))}, "
                         f"not {value!r}.")
    return value


def _flags(params, args):
    out = []
    for n in params:  # declared order, so a script can rely on it
        if n not in args:
            continue
        v = args[n]
        if isinstance(v, bool):
            if v:
                out.append(f"--{n}")
        else:
            out += [f"--{n}", json.dumps(v) if isinstance(v, (dict, list)) else str(v)]
    return out


def _runner(project, full):
    ext = os.path.splitext(full)[1].lower()
    if ext == ".py":
        venv = os.path.join(workspace.project_dir(project), ".venv", "Scripts", "python.exe")
        return [venv if os.path.isfile(venv) else _python(), full]
    if ext in (".js", ".mjs", ".cjs"):
        node = shutil.which("node")
        if not node:
            raise RuntimeError("Node.js isn't installed, so a .js tool can't run.")
        return [node, full]
    if ext == ".ps1":
        return ["powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", full]
    raise RuntimeError(f"Don't know how to run a {ext} file.")


def _python():
    """The python Claude tested with — the one on PATH — rather than
    BananaGent's own, which is often pythonw and has only its packages."""
    for name in ("python", "py"):
        found = shutil.which(name)
        # The Microsoft Store placeholder opens the Store instead of running.
        if found and "WindowsApps" not in found:
            return found
    exe = sys.executable
    if os.path.basename(exe).lower() == "pythonw.exe":
        exe = os.path.join(os.path.dirname(exe), "python.exe")
    return exe
