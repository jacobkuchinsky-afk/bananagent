"""The coding agent's tools for giving BananaGent new tools (see toolbox.py).

    tool_make     declare a script's arguments and hook it up as a tool
    tool_list     the tools made so far
    tool_remove   take one away

scope=CODER like the rest of the coding agent's tools: the assistant never
sees these, only the tools they make. Asking for a new tool is a code_do job,
so the script gets written, run and checked like any other code before it's
hooked up — and tool_make runs it once more itself before saving.
"""

import json

import toolbox
from .coder_tools import _project
from .registry import CODER, MAJOR, MINOR, endpoint, param


@endpoint(
    tier=MAJOR, scope=CODER,
    description=(
        "Makes a script in this project into a tool BananaGent can call from now on, the "
        "same way it calls its built-in tools: you name it, describe it, declare its "
        "arguments and point it at the script. It's run once with test_args first and "
        "only saved if that works. The same name again updates it. The script's "
        "contract (brief Claude with it): it runs in the project folder; it gets the "
        "arguments as a JSON object on stdin (pass_args json — in Python, "
        "json.load(sys.stdin)) or as --name value flags (pass_args flags, for an "
        "argparse script); it prints its result to stdout, as JSON when it's "
        "structured, kept under about 5000 characters; and on failure it exits non-zero "
        "with the reason on stderr. .py, .js and .ps1 scripts; Python runs with the "
        "project's .venv if there is one, else the python on PATH."
    ),
    params={
        "name": param("string", "What BananaGent calls it: snake_case, saying what it "
                                "does, like uspto_search.", required=True),
        "description": param("string", "What it does, what it's for and when to use it — "
                                       "BananaGent reads this when choosing a tool, so be "
                                       "specific. One to three plain sentences.",
                             required=True),
        "script": param("string", "The script in this project that does the work, e.g. "
                                  "uspto_search.py.", required=True),
        "tier": param("string", "minor if it only looks things up and reports; major if "
                                "running it changes anything — files, settings, sends or "
                                "buys something.", required=True, enum=["minor", "major"]),
        "params": param("object", 'Its arguments: {"query": {"type": "string", '
                                  '"description": "The trademark to search for.", '
                                  '"required": true}, "limit": {"type": "integer", '
                                  '"description": "How many results.", "default": 10}}. '
                                  "Types: string, integer, number, boolean, array, object; "
                                  "enum and default are optional. Leave out if it takes none."),
        "test_args": param("object", 'Real arguments to test it with, e.g. {"query": "nike"} '
                                     "— {} if it takes none. Required for a minor tool; leave "
                                     "out only for a major one whose test would do something "
                                     "for real."),
        "returns": param("string", "What it hands back, in a few words — e.g. 'matching "
                                   "trademarks with serial number, status and classes'."),
        "pass_args": param("string", "How the script gets its arguments.",
                           enum=["json", "flags"], default="json"),
        "timeout": param("integer", "Seconds before a run is stopped, up to 600.",
                         default=60),
    },
    returns="The tool as saved, how BananaGent calls it, and the test run's result.",
)
def tool_make(name, description, script, tier, params=None, test_args=None, returns=None,
              pass_args="json", timeout=60):
    manifest, tested, updated = toolbox.make(
        _project(), name, script, description, tier, params=params, returns=returns,
        pass_args=pass_args, timeout=timeout, test_args=test_args)
    shown = tested
    if isinstance(shown, (dict, list)) or (isinstance(shown, str) and len(shown) > 2000):
        text = json.dumps(shown, ensure_ascii=False, default=str)
        shown = text if len(text) <= 2000 else text[:2000] + " …(cut)"
    out = {"tool": toolbox.signature(manifest), "updated" if updated else "made": True,
           "tier": manifest["tier"],
           "runs": f"{manifest['project']}/{manifest['script']} (args as {manifest['pass_args']})",
           "note": "BananaGent can call it from its next message. Mention it in your report."}
    if test_args is not None:
        out["test_run"] = shown
    return out


@endpoint(
    tier=MINOR, scope=CODER,
    description="Lists the tools made for BananaGent so far: name, arguments, what each "
                "does, and the project and script behind it.",
    returns="One entry per tool.",
)
def tool_list():
    return [{"tool": toolbox.signature(m), "tier": m["tier"], "description": m["description"],
             "runs": f"{m['project']}/{m['script']}", "pass_args": m["pass_args"],
             "updated": m.get("updated")}
            for m in toolbox.made().values()] or "No tools made yet."


@endpoint(
    tier=MAJOR, scope=CODER,
    description="Takes a made tool away from BananaGent. Its script stays in the project, "
                "and the declaration is kept aside in case it's wanted back.",
    params={"name": param("string", "The tool's name.", required=True)},
)
def tool_remove(name):
    toolbox.remove(name)
    return f"Removed {name}. BananaGent won't see it from its next message."
