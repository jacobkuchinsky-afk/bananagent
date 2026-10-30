"""The prompting system: four markdown files that become one system prompt.

    prompts/main.md          what BananaGent is and how it does its job
    prompts/personality.md   how it sounds
    prompts/texting.md       how it texts, when a message comes from Discord
    prompts/tools.md         how it uses its tools

system_prompt() joins them in that order, each under its own heading. They're
re-read whenever a file changes on disk, so editing one — by hand or through
the prompt endpoints — takes effect on the very next message, no restart.

Notes inside <!-- --> are for whoever's editing and never reach the model.
{{tools}} expands to the live endpoint catalog — tools.md uses it, since the
model calls tools by writing them out (see brain.py) and this is the only
place it learns what exists. {{places}} expands to this computer's usual
folders, so the model can link to files by their real paths.

prompts/defaults/ holds the stock text. A part whose file is missing is
restored from there, and reset() puts one back. Every write through this
module first copies the old version into prompts/history/, so an edit that
goes wrong can always be dug back out.
"""

import os
import re
import shutil
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = os.path.join(HERE, "defaults")
HISTORY = os.path.join(HERE, "history")

# Assembly order, and the heading each part sits under in the system prompt.
# main.md goes in bare: it opens the prompt and says what everything else is.
PARTS = {
    "main": None,
    "personality": "Personality",
    # Always in the prompt, not just for Discord messages: a system message
    # that changed with where you wrote from would stop the model server
    # reusing its cache, and cost a full re-read every time you switched.
    "texting": "Texting on Discord",
    "tools": "Tools",
}
MAX_CHARS = 20000  # per part, so a runaway edit can't bury everything else

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_lock = threading.RLock()
_cache = {}  # part -> (mtime, raw text)


def path(part):
    _check(part)
    return os.path.join(HERE, f"{part}.md")


def _check(part):
    if part not in PARTS:
        raise ValueError(f"No prompt part called '{part}'. Parts: {', '.join(PARTS)}")


# -- reading -----------------------------------------------------------------
def read(part):
    """A part's file exactly as written, notes and all."""
    p = path(part)
    with _lock:
        if not os.path.exists(p):
            _restore(part)
        stamp = os.path.getmtime(p)
        cached = _cache.get(part)
        if cached and cached[0] == stamp:
            return cached[1]
        with open(p, encoding="utf-8") as f:
            text = f.read()
        _cache[part] = (stamp, text)
        return text


def render(part):
    """A part as the model sees it: notes stripped, placeholders filled."""
    text = _COMMENT.sub("", read(part))
    if "{{tools}}" in text:
        import endpoints

        text = text.replace("{{tools}}", endpoints.catalog_prompt())
    if "{{places}}" in text:
        text = text.replace("{{places}}", places())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def places():
    """This computer's usual folders, so links in replies can use real paths
    (main.md's {{places}})."""
    home = os.path.expanduser("~")
    found = [f"home {home}"]
    for name in ("Desktop", "Documents", "Downloads"):
        for where in (os.path.join(home, "OneDrive", name), os.path.join(home, name)):
            if os.path.isdir(where):
                found.append(f"{name} {where}")
                break
    import workspace

    found.append(f"your coding projects {workspace.ROOT}")
    return "; ".join(found) + "."


def system_prompt():
    """Everything, assembled — what goes in the model's system message."""
    blocks = []
    for part, heading in PARTS.items():
        body = render(part)
        if body:
            blocks.append(f"# {heading}\n\n{body}" if heading else body)
    return "\n\n".join(blocks)


# -- writing -----------------------------------------------------------------
def write(part, text):
    """Replace a part outright. The old version goes to history first."""
    text = str(text).replace("\r\n", "\n").strip() + "\n"
    if len(text) > MAX_CHARS:
        raise ValueError(f"That's {len(text)} characters; a prompt part is capped at {MAX_CHARS}.")
    with _lock:
        _save_history(part)
        with open(path(part), "w", encoding="utf-8") as f:
            f.write(text)
        _cache.pop(part, None)
    print(f"[prompts] {part}.md rewritten ({len(text)} chars)")
    return text


def append(part, text):
    """Add a paragraph to the end of a part."""
    with _lock:
        return write(part, read(part).rstrip() + "\n\n" + str(text).strip())


def replace(part, find, new):
    """Swap one exact passage for another. It has to appear exactly once."""
    find = str(find)
    with _lock:
        current = read(part)
        count = current.count(find) if find else 0
        if count == 0:
            raise ValueError(f"That text isn't in {part}.md. Read it first and copy the exact wording.")
        if count > 1:
            raise ValueError(f"That text appears {count} times in {part}.md; include more "
                             f"around it so it matches only once.")
        return write(part, current.replace(find, str(new), 1))


def reset(part):
    """Put a part back to its default."""
    with _lock:
        _save_history(part)
        _restore(part)
    print(f"[prompts] {part}.md reset to default")
    return read(part)


def _restore(part):
    shutil.copyfile(os.path.join(DEFAULTS, f"{part}.md"), path(part))
    _cache.pop(part, None)


def _save_history(part):
    p = path(part)
    if not os.path.exists(p):
        return
    os.makedirs(HISTORY, exist_ok=True)
    # Milliseconds too: a model making several edits in a row would otherwise
    # land them in the same second and keep only the last one's backup.
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"
    shutil.copyfile(p, os.path.join(HISTORY, f"{part}-{stamp}.md"))
