"""Triggers: messages BananaGent sends itself, later or when something happens.

A trigger is a note to its future self plus the thing that sets it off:

    time   — a moment ("at 9am tomorrow", "in 20 minutes"), once or repeating
    file   — a file or folder on this computer changing (written, created,
             deleted, or for a folder anything inside it)

When one goes off, the note comes back to the model as an automatic notice —
the same way a finished agent reports in (agents.announce) — so it gets a turn
of its own to tell the user, or to do whatever the note says. Nobody has to be
talking to it at the time.

Everything is kept in triggers/triggers.json, so they survive restarts. A
one-off reminder that came due while BananaGent wasn't running goes off as
soon as it starts, saying when it was due; a repeating one goes off once for
the misses and moves on to its next time. A folder's contents are kept with
its watch, so a change made while the app was off is noticed too.

One thread checks everything every POLL_SECONDS. A file being written usually
changes several times in a row, so a change only counts once two checks in a
row agree on it — the note goes off a few seconds after the writing stops,
once, rather than once per write.
"""

import datetime
import json
import os
import threading
import time
import traceback
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "triggers.json")

POLL_SECONDS = 3
# A folder watch keeps a fingerprint of every file inside; past this many it
# stops looking deeper rather than walk a whole drive every few seconds.
MAX_FILES = 5000
# How many changed names a file notice lists before it says "and N more".
SHOW_CHANGES = 15

REPEATS = ("hourly", "daily", "weekdays", "weekly")

_lock = threading.RLock()
_listeners = []
_thread = None
_stop = threading.Event()


# -- storage -------------------------------------------------------------------
def load():
    with _lock:
        try:
            with open(DATA, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return []
        except Exception as e:
            # Don't start over on someone's reminders: refuse until it's fixed.
            raise RuntimeError(f"triggers/triggers.json can't be read ({e}); "
                               f"fix or delete it") from e


def _save(items):
    with _lock:
        tmp = DATA + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        os.replace(tmp, DATA)  # never leave a half-written file behind


def _add(item):
    with _lock:
        items = load()
        items.append(item)
        _save(items)
    return item


def _new(kind, note):
    note = str(note or "").strip()
    if not note:
        raise ValueError("a trigger needs a note: what to tell yourself when it goes off")
    return {"id": uuid.uuid4().hex[:6], "kind": kind, "note": note,
            "created": time.time(), "fired": 0, "last_fired": None}


# -- time ----------------------------------------------------------------------
_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
            "%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M%p", "%Y-%m-%d %I %p", "%Y-%m-%d")
_TIMES = ("%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p")


def parse_when(text, now=None):
    """A local date and time the model wrote, as a datetime. Full dates in the
    usual ISO spellings, or a bare clock time ("3:30 PM", "15:30"), meaning the
    next time the clock reads that. A date with no time is 9 in the morning."""
    now = now or datetime.datetime.now()
    given, text = text, " ".join(str(text).strip().upper().replace(".", "").split())
    for fmt in _FORMATS:
        try:
            when = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return when.replace(hour=9) if fmt == "%Y-%m-%d" else when
    for fmt in _TIMES:
        try:
            t = datetime.datetime.strptime(text, fmt).time()
        except ValueError:
            continue
        when = datetime.datetime.combine(now.date(), t)
        return when if when > now else when + datetime.timedelta(days=1)
    raise ValueError(f"can't read the time '{given}'; write it like 2026-10-31 15:30, "
                     f"or as a clock time like 3:30 PM")


def _next(due, repeat, every_minutes):
    """The repeat after `due`."""
    d = datetime.datetime.fromtimestamp(due)
    if every_minutes:
        return due + every_minutes * 60
    if repeat == "hourly":
        return (d + datetime.timedelta(hours=1)).timestamp()
    if repeat == "daily":
        return (d + datetime.timedelta(days=1)).timestamp()
    if repeat == "weekly":
        return (d + datetime.timedelta(days=7)).timestamp()
    if repeat == "weekdays":
        d += datetime.timedelta(days=1)
        while d.weekday() >= 5:
            d += datetime.timedelta(days=1)
        return d.timestamp()
    return None


def add_time(note, at=None, in_minutes=None, repeat=None, every_minutes=None):
    if repeat in (None, "", "none"):
        repeat = None
    elif repeat not in REPEATS:
        raise ValueError(f"repeat must be one of {', '.join(REPEATS)}")
    every_minutes = float(every_minutes) if every_minutes else None
    if every_minutes is not None and every_minutes < 1:
        raise ValueError("every_minutes must be at least 1")
    if in_minutes not in (None, ""):
        due = time.time() + float(in_minutes) * 60
    elif at:
        due = parse_when(at).timestamp()
    elif every_minutes:
        due = time.time() + every_minutes * 60
    else:
        raise ValueError("say when: at (a date and time) or in_minutes")
    if due < time.time() - 60:
        raise ValueError(f"{_stamp(due)} has already passed")
    if repeat == "weekdays":
        # By the wall clock, not in seconds, so 9am stays 9am across a DST change.
        d = datetime.datetime.fromtimestamp(due)
        while d.weekday() >= 5:
            d += datetime.timedelta(days=1)
        due = d.timestamp()
    item = _new("time", note)
    item.update(due=due, repeat=repeat, every_minutes=every_minutes)
    return _add(item)


# -- files ---------------------------------------------------------------------
def _fingerprint(path):
    """What a path looks like right now: {relative name: [mtime, size]}. A
    file is one entry under "."; a missing path is empty."""
    if os.path.isfile(path):
        st = os.stat(path)
        return {".": [round(st.st_mtime, 3), st.st_size]}
    if not os.path.isdir(path):
        return {}
    seen = {}
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "node_modules")]
        for name in files:
            full = os.path.join(root, name)
            try:
                st = os.stat(full)
            except OSError:
                continue  # gone between listing it and looking at it
            seen[os.path.relpath(full, path)] = [round(st.st_mtime, 3), st.st_size]
            if len(seen) >= MAX_FILES:
                return seen
    return seen


def _changes(before, after):
    """What differs, in words: created, deleted, modified names."""
    added = sorted(set(after) - set(before))
    gone = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    if list(before) in ([], ["."]) and list(after) in ([], ["."]):
        # A single file: say what happened to it, not to "."
        return ["created" if added else "deleted" if gone else "modified"]
    lines = [f"created {n}" for n in added] + [f"deleted {n}" for n in gone] \
        + [f"modified {n}" for n in changed]
    if len(lines) > SHOW_CHANGES:
        lines = lines[:SHOW_CHANGES] + [f"…and {len(lines) - SHOW_CHANGES} more"]
    return lines


def add_file(path, note, once=False):
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(str(path).strip().strip('"'))))
    if not os.path.exists(os.path.dirname(path)):
        raise ValueError(f"{os.path.dirname(path)} doesn't exist, so nothing in it can change")
    item = _new("file", note)
    item.update(path=path, once=bool(once), seen=_fingerprint(path), pending=None)
    return _add(item)


# -- managing ------------------------------------------------------------------
def cancel(trigger_id):
    with _lock:
        items = load()
        keep = [t for t in items if t["id"] != str(trigger_id).strip()]
        if len(keep) == len(items):
            raise ValueError(f"no trigger with id '{trigger_id}'; list_triggers shows them")
        _save(keep)
        return next(t for t in items if t["id"] == str(trigger_id).strip())


def _stamp(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%a %b %d %Y, %I:%M %p") \
        .replace(" 0", " ")


def describe(t):
    """One trigger in a line, for the model and the log."""
    if t["kind"] == "time":
        how = (f"every {t['every_minutes']:g} min" if t.get("every_minutes")
               else t.get("repeat") or "once")
        when = f"at {_stamp(t['due'])} ({how})"
    else:
        what = "folder" if os.path.isdir(t["path"]) else "file"
        when = f"when the {what} {t['path']} changes" + (" (once)" if t.get("once") else "")
    fired = f", gone off {t['fired']}x" if t.get("fired") else ""
    return f"[{t['id']}] {when}{fired}: {t['note']}"


# -- firing --------------------------------------------------------------------
def listen(fn):
    """fn(label, message) is called on the trigger thread when one goes off.
    It must not block."""
    _listeners.append(fn)
    return fn


def _fire(t, label, detail):
    t["fired"] = t.get("fired", 0) + 1
    t["last_fired"] = time.time()
    message = (f"This is a trigger you set yourself (id {t['id']}, set "
               f"{_stamp(t['created'])}), and it has just gone off. {detail}\n\n"
               f"Your note to yourself:\n{t['note']}\n\n"
               f"Do what the note says now. If it's a reminder for the user, tell them "
               f"in a sentence or two, in your own words.")
    print(f"[triggers] {describe(t)}")
    for fn in list(_listeners):
        try:
            fn(label, message)
        except Exception:
            traceback.print_exc()


def _check_time(t, now):
    """Fire if due. Returns False when the trigger is finished with."""
    if t["due"] > now:
        return True
    late = now - t["due"] > 120
    detail = (f"It was due at {_stamp(t['due'])}, but BananaGent wasn't running then; "
              f"it's {_stamp(now)} now." if late else f"It was set for {_stamp(t['due'])}.")
    _fire(t, "A reminder you set went off", detail)
    nxt = _next(t["due"], t.get("repeat"), t.get("every_minutes"))
    if nxt is None:
        return False
    while nxt <= now:  # skip the repeats that were missed; it fired once for them
        nxt = _next(nxt, t.get("repeat"), t.get("every_minutes"))
    t["due"] = nxt
    return True


def _check_file(t):
    now = _fingerprint(t["path"])
    if now == t["seen"]:
        t["pending"] = None
        return True
    if now != t.get("pending"):
        t["pending"] = now  # still moving; wait for it to settle
        return True
    lines = _changes(t["seen"], now)
    t["seen"], t["pending"] = now, None
    what = "folder" if os.path.isdir(t["path"]) else "file"
    detail = (f"The {what} {t['path']} changed:\n" + "\n".join(f"- {l}" for l in lines))
    _fire(t, f"A {what} you were watching changed", detail)
    return not t.get("once")


def check():
    """One pass over every trigger: fire what's due, keep what's left."""
    with _lock:
        items = load()
        if not items:
            return
        before = json.dumps(items, sort_keys=True)
        now = time.time()
        keep = []
        for t in items:
            try:
                alive = _check_time(t, now) if t["kind"] == "time" else _check_file(t)
            except Exception:
                traceback.print_exc()
                alive = True
            if alive:
                keep.append(t)
        if json.dumps(keep, sort_keys=True) != before:
            _save(keep)


def _loop():
    while not _stop.wait(POLL_SECONDS):
        try:
            check()
        except Exception:
            traceback.print_exc()


def start():
    """Start checking in the background. Safe to call twice."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="triggers", daemon=True)
    _thread.start()
    try:
        print(f"[triggers] watching {len(load())} trigger(s)")
    except Exception as e:
        print(f"[triggers] {e}")


def stop():
    _stop.set()
