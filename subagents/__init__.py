"""Subagents: a fresh model run for one big job, then gone.

Some jobs would swamp the assistant's own context — writing a long document,
working through a lot of material, many steps of lookups. For those, the
assistant calls subagent_run with the task, and a subagent does the work in a
conversation of its own:

  * its own system prompt (subagents/<kind>.md) and its own set of tools;
  * its own memory: every message and tool result of the run, kept whole —
    no sliding window, and none of the assistant's short- or long-term memory
    (the task has to carry whatever context it needs);
  * when it finishes, its final reply goes back to the assistant as the tool
    result, and the conversation is thrown away.

A kind is a name, a description the assistant reads when choosing one, a
prompt file, and a rule for which tools it gets. Adding one is a Kind(...) and
register() below plus its .md file. Subagents can't start subagents.

The model and server are the assistant's own; a run goes through the same
brain.converse() tool loop, so the call protocol and its safeguards are shared.
"""

import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable

import config
import endpoints
import memory

HERE = os.path.dirname(os.path.abspath(__file__))
# Tools no subagent gets, whatever its kind says: no nesting, and no touching
# the assistant's own memory or prompts from inside a job.
NEVER = {"subagent_run", "memory_add", "memory_edit", "memory_write",
         "memory_open_file", "conversation_clear",
         "prompt_write", "prompt_append", "prompt_edit", "prompt_reset", "app_quit",
         "app_restart"}

_COMMENT = re.compile(r"<!--.*?-->", re.S)


@dataclass
class Kind:
    name: str
    description: str                         # what the assistant reads when choosing
    tools: Callable = field(repr=False)      # endpoint -> bool: may this kind call it?
    max_steps: float = math.inf              # tool calls; unlimited unless a kind says otherwise

    def allowed(self):
        return {e.name for e in endpoints.all_endpoints()
                if e.name not in NEVER and self.tools(e)}

    def system_prompt(self):
        with open(os.path.join(HERE, f"{self.name}.md"), encoding="utf-8") as f:
            text = _COMMENT.sub("", f.read())
        catalog = endpoints.catalog_prompt(self.allowed()) or "(none)"
        return re.sub(r"\n{3,}", "\n\n", text.replace("{{tools}}", catalog)).strip()


KINDS = {}


def register(kind):
    KINDS[kind.name] = kind
    return kind


register(Kind(
    name="worker",
    description=("General-purpose: long writing and drafting, summarising or reworking "
                 "a lot of text, and multi-step look-ups using read-only tools."),
    tools=lambda e: e.tier == endpoints.MINOR,
))

register(Kind(
    name="research",
    description=("Research projects: searches the web over and over, from every angle, "
                 "until it has the whole landscape, then reports back a sourced summary. "
                 "Brief it with the question, why the user wants to know, what they need "
                 "back, and any constraints."),
    # Nothing but search and the clock: a short tool list keeps a small model
    # calling the one tool it needs instead of wandering off.
    tools=lambda e: e.name in ("web_search", "get_time", "keep_going"),
))


def run(task, kind="worker"):
    """Do one job in a subagent and return what it produced."""
    import brain

    if kind not in KINDS:
        raise ValueError(f"No subagent kind '{kind}'. Kinds: {', '.join(KINDS)}")
    k = KINDS[kind]
    task = str(task).strip()
    if not task:
        raise ValueError("A subagent needs a task.")

    # Stamped like the assistant's own messages, so it knows what "current"
    # means — unstamped, a research run searches for last year's results.
    messages = [{"role": "system", "content": k.system_prompt()},
                {"role": "user", "content": memory.stamp(time.time()) + task}]
    started = time.time()
    print(f"[subagent:{k.name}] started: {task[:120]}")
    # The result is what it wrote after its last tool call. Asked not to, the
    # model still narrates between calls ("Let me try another search…"), and
    # twenty of those lines ahead of a research report bury it. Only if there's
    # nothing after the last call is the whole of it kept.
    everything, final = [], []
    for piece in brain.converse(messages, allowed=k.allowed(), max_rounds=k.max_steps):
        if isinstance(piece, str):
            everything.append(piece)
            final.append(piece)
        else:
            final = []
    result = ("".join(final).strip() or "".join(everything).strip())
    calls = sum(1 for m in messages if m["role"] == "user" and
                m["content"].startswith("Tool result for "))
    seconds = round(time.time() - started, 1)
    print(f"[subagent:{k.name}] done in {seconds}s, {calls} tool calls, {len(result)} chars")
    if len(result) > config.SUBAGENT_RESULT_CHARS:
        result = result[:config.SUBAGENT_RESULT_CHARS] + " …(cut)"
    return {"kind": k.name, "result": result, "tool_calls": calls, "seconds": seconds}
