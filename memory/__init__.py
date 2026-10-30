"""BananaGent's memory: a short-term window and a long-term file.

Short-term is the recent conversation — every user message and reply, oldest
first, cut to the last config.SHORT_TERM_TOKENS. It's what makes "and what
about tomorrow?" mean something. Each finished turn is appended and the oldest
fall out, and the whole window is kept in memory/conversation.json so a restart
picks up where things left off. Each message keeps the time it was sent
("at"), and the model sees it as a stamp at the start of the message.

Long-term is memory/MEMORY.md: plain markdown, one fact per line under a few
headings, kept forever and sent with every message. The model writes to it
through the memory tools; you can edit it by hand at any time and the next
message sees the change. Every write through this module first copies the old
version to memory/history/.

Every config.MEMORY_REVIEW_EVERY messages, yours and its replies alike, the
model is told to go back over them and save what it learned (see brain.py);
memory/review.json keeps count across restarts.

Both files hold personal details and are gitignored.
"""

import json
import os
import re
import shutil
import threading
import time

import config

HERE = os.path.dirname(os.path.abspath(__file__))
LONG_TERM = os.path.join(HERE, "MEMORY.md")
SHORT_TERM = os.path.join(HERE, "conversation.json")
HISTORY = os.path.join(HERE, "history")
REVIEW = os.path.join(HERE, "review.json")

_TEMPLATE = """<!--
BananaGent's long-term memory. Everything below is sent to the model with every
message and kept forever. Edit it freely: plain markdown, one fact per line
under a heading. The model adds to it with its memory tools. Notes inside
these markers are for you and never reach the model.
-->

## About the user

## Preferences

## People

## Projects
"""

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_lock = threading.RLock()
_turns = None  # the short-term window, loaded on first use
_since = None  # messages since the last memory review, loaded on first use


# =============================================================================
# Short-term: the sliding window
# =============================================================================

def window():
    """The conversation the model is shown, as user/assistant messages.

    A reply that used tools is replayed as it happened — each call, then its
    result, then the answer — not as the answer alone. Shown only the answers,
    the model sees itself saying the time without ever calling get_time, and
    starts doing just that.

    Every message opens with a stamp of when it was sent (see stamp()), so the
    model can tell "earlier today" from "last week".
    """
    with _lock:
        t = _load_turns()
        return [m for i, entry in enumerate(t)
                for m in _as_messages(entry, t[i - 1].get("at") if i else None)]


def last_at():
    """When the most recent message in the window was sent, or None."""
    with _lock:
        t = _load_turns()
        return t[-1].get("at") if t else None


_GAP = 5 * 60  # a gap shorter than this isn't worth pointing out


def stamp(at, previous=None):
    """How a message's send time is shown to the model:
    "[Wed Sep 30 2026, 4:28:22 PM]", or with "— 3 hours 5 minutes after the
    previous message" inside the brackets when the gap is long enough to matter.

    The gap is spelled out because working it out from two stamps is exactly
    the arithmetic a model gets wrong. It's fixed per message, so the window
    reads the same on every request and the model server can keep reusing it.
    """
    if not at:
        return ""
    lt = time.localtime(at)
    text = (time.strftime("%a %b ", lt) + str(lt.tm_mday) + time.strftime(" %Y, ", lt)
            + time.strftime("%I:%M:%S %p", lt).lstrip("0"))
    if previous and at - previous >= _GAP:
        text += f" — {_duration(at - previous)} after the previous message"
    return f"[{text}] "


def _duration(seconds):
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    units = [(days, "day"), (hours, "hour"), (minutes, "minute")]
    shown = [f"{n} {name}{'s' if n != 1 else ''}" for n, name in units if n][:2]
    return " ".join(shown) or "under a minute"


def _as_messages(entry, previous=None):
    """One stored message as the model sees it. `previous` is when the message
    before it was sent; only a user message says how long it's been."""
    head = stamp(entry.get("at"), previous if entry["role"] == "user" else None)
    parts = entry.get("parts")
    if entry["role"] != "assistant" or not parts:
        return [{"role": entry["role"], "content": head + entry["content"]}]
    out, said = [], head
    for part in parts:
        if part["kind"] == "text":
            said += part["text"]
            continue
        args = json.dumps(part.get("args") or {}, ensure_ascii=False)
        out += [{"role": "assistant", "content": f"{said}[[call {part['name']} {args}]]"},
                {"role": "user", "content": f"Tool result for {part['name']}: "
                                            f"{part.get('result') or '(not kept)'}"}]
        said = ""
    if said.strip() and said != head:
        out.append({"role": "assistant", "content": said})
    return out


def turns():
    """The window as (message, reply, parts) for the chat to show on startup.
    `parts` is the reply's text and tool calls in order, or None if it wasn't kept."""
    with _lock:
        t = _load_turns()
        return [(t[i]["content"], t[i + 1]["content"], t[i + 1].get("parts"))
                for i in range(0, len(t) - 1, 2)]


def remember(message, reply, parts=None, sent_at=None):
    """Add one finished exchange, then slide the window.

    `parts` is only for showing the reply again — its tool calls among its
    text. The model is sent the reply text alone, and only that counts
    toward the window's size.

    `sent_at` is when the user sent the message; the reply is stamped now,
    as it finishes.
    """
    now = time.time()
    with _lock:
        t = _load_turns()
        answer = {"role": "assistant", "content": reply, "at": now}
        if parts:
            answer["parts"] = parts
        t += [{"role": "user", "content": message, "at": sent_at or now}, answer]
        _slide(t)
        _save_turns()
        _set_since(_load_since() + 2)


def clear_conversation():
    """Forget the recent conversation. Long-term memory is untouched."""
    global _turns
    with _lock:
        _turns = []
        _save_turns()
        _set_since(0)


def short_term_tokens():
    with _lock:
        return _tokens(_load_turns())


def _slide(t):
    """Once over the limit, drop the oldest exchanges down to SHORT_TERM_SLIDE_TO.

    Whole exchanges only, so it never starts on a reply to a question it no
    longer has. The step down is deliberate: see SHORT_TERM_SLIDE_TO.
    """
    if _tokens(t) <= config.SHORT_TERM_TOKENS:
        return
    target = min(config.SHORT_TERM_SLIDE_TO, config.SHORT_TERM_TOKENS)
    while len(t) > 2 and _tokens(t) > target:
        del t[:2]


def _tokens(entries):
    """What the entries cost the model, tool calls and results included."""
    return sum(len(m["content"]) for e in entries for m in _as_messages(e)) \
        // config.CHARS_PER_TOKEN


def _load_turns():
    global _turns
    if _turns is None:
        try:
            with open(SHORT_TERM, encoding="utf-8") as f:
                _turns = [m for m in json.load(f) if m.get("role") in ("user", "assistant")]
        except FileNotFoundError:
            _turns = []
        except Exception as e:
            # A damaged window shouldn't stop the app; keep the file for a look.
            print(f"[memory] couldn't read {SHORT_TERM}: {e}; starting a fresh window")
            if os.path.exists(SHORT_TERM):
                shutil.copyfile(SHORT_TERM, SHORT_TERM + ".broken")
            _turns = []
        if len(_turns) % 2:
            _turns = _turns[1:]  # keep it in pairs
    return _turns


def _save_turns():
    tmp = SHORT_TERM + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_turns, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SHORT_TERM)  # never leave a half-written file behind


# =============================================================================
# Memory review: every so many messages, save what was learned
# =============================================================================

def review_due():
    """Whether the message about to be sent makes config.MEMORY_REVIEW_EVERY
    messages since the last review. 0 turns reviews off."""
    every = config.MEMORY_REVIEW_EVERY
    with _lock:
        return bool(every) and _load_since() + 1 >= every


def reviewed():
    """A review ran to the end. Whatever went past the mark counts toward the
    next one, so over time it's exactly one review per MEMORY_REVIEW_EVERY."""
    with _lock:
        _set_since(_load_since() - config.MEMORY_REVIEW_EVERY)


def _load_since():
    global _since
    if _since is None:
        try:
            with open(REVIEW, encoding="utf-8") as f:
                _since = int(json.load(f)["since"])
        except Exception:
            _since = 0  # missing or damaged: start counting from here
    return _since


def _set_since(n):
    global _since
    _since = n  # briefly -1: a review is marked done before its reply is counted
    tmp = REVIEW + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"since": _since}, f)
    os.replace(tmp, REVIEW)


# =============================================================================
# Long-term: MEMORY.md
# =============================================================================

def read():
    """MEMORY.md exactly as written, notes and all."""
    with _lock:
        if not os.path.exists(LONG_TERM):
            with open(LONG_TERM, "w", encoding="utf-8") as f:
                f.write(_TEMPLATE)
        with open(LONG_TERM, encoding="utf-8") as f:
            return f.read()


def render():
    """MEMORY.md as the model sees it: notes stripped, empty headings dropped."""
    text = _COMMENT.sub("", read())
    blocks = re.split(r"(?m)^(?=#{1,6}\s)", text)
    kept = [b.strip() for b in blocks
            if b.strip() and not re.fullmatch(r"#{1,6}\s+[^\n]*", b.strip())]
    return "\n\n".join(kept)


def add(fact, section=None):
    """Add one line to MEMORY.md under `section` (made if missing), or under
    Notes — never tacked onto whichever heading happens to be last."""
    fact = " ".join(str(fact).split())
    if not fact:
        raise ValueError("There's nothing to remember in an empty note.")
    line = fact if fact.startswith(("- ", "* ")) else f"- {fact}"
    with _lock:
        return write(_add_under(read(), str(section or "").strip() or "Notes", line))


def replace(find, new):
    """Swap one exact passage for another; an empty replacement deletes it.
    A line left empty by a deletion goes with it."""
    find = str(find)
    with _lock:
        text = read()
        count = text.count(find) if find else 0
        if count == 0:
            raise ValueError("That text isn't in the memory file. Read it first and "
                             "copy the exact wording.")
        if count > 1:
            raise ValueError(f"That text appears {count} times; include more around it "
                             f"so it matches only once.")
        text = text.replace(find, str(new), 1)
        text = re.sub(r"(?m)^[ \t]*[-*][ \t]*\n", "", text)  # a bullet with nothing left
        return write(text)


def write(text):
    """Replace MEMORY.md outright. The old version goes to history first."""
    text = str(text).replace("\r\n", "\n").strip() + "\n"
    if len(_COMMENT.sub("", text)) > config.LONG_TERM_MAX_CHARS:
        raise ValueError(f"Long-term memory would be over {config.LONG_TERM_MAX_CHARS} "
                         f"characters. Tidy it up first: merge or remove entries that "
                         f"no longer matter.")
    with _lock:
        _backup()
        with open(LONG_TERM, "w", encoding="utf-8") as f:
            f.write(text)
    print(f"[memory] MEMORY.md saved ({len(text)} chars)")
    return text


def _add_under(text, section, line):
    heading = re.compile(rf"(?mi)^#{{1,6}}[ \t]+{re.escape(section)}[ \t]*$")
    m = heading.search(text)
    if not m:
        return f"{text.rstrip()}\n\n## {section}\n{line}\n"
    nxt = re.compile(r"(?m)^#{1,6}\s").search(text, m.end())
    end = nxt.start() if nxt else len(text)
    body = text[m.end():end].strip("\n")
    block = f"{body}\n{line}" if body.strip() else line
    return text[:m.end()] + "\n" + block + ("\n\n" + text[end:] if nxt else "\n")


def _backup():
    if not os.path.exists(LONG_TERM):
        return
    os.makedirs(HISTORY, exist_ok=True)
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"
    shutil.copyfile(LONG_TERM, os.path.join(HISTORY, f"MEMORY-{stamp}.md"))
