"""Loads the editable prompt from prompt.md.

Re-read whenever the file changes on disk, so tuning the wording is: save the
file, ask the next question. No restart. If the file is missing or a section is
empty we fall back to the built-in text in config.py, so a bad edit degrades to
the default instead of breaking the app.
"""

import os
import re
import threading

import config

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt.md")

_lock = threading.Lock()
_cache = {}
_stamp = None

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)


def _parse(text):
    """Split the file into {section name: body}, dropping notes-to-self."""
    text = _COMMENT.sub("", text)
    sections = {}
    marks = list(_HEADING.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].strip()
        if body:
            sections[m.group(1).strip().lower()] = body
    return sections


def _load():
    global _stamp, _cache
    try:
        stamp = os.path.getmtime(PATH)
    except OSError:
        _cache, _stamp = {}, None
        return _cache

    if stamp != _stamp:
        with _lock:
            try:
                with open(PATH, encoding="utf-8") as f:
                    _cache = _parse(f.read())
                _stamp = stamp
                print(f"[prompts] loaded {os.path.basename(PATH)} "
                      f"({', '.join(sorted(_cache)) or 'no sections'})")
            except Exception as e:
                print(f"[prompts] couldn't read {PATH}: {e}; using built-in prompt")
                _cache, _stamp = {}, stamp
    return _cache


def system():
    """The standing instructions."""
    return _load().get("system") or config.SYSTEM_PROMPT


def audio_turn():
    """The text sent alongside the audio in the user's turn."""
    return _load().get("audio turn") or config.AUDIO_PROMPT


if __name__ == "__main__":
    for name, body in _load().items():
        print(f"\n===== {name} =====\n{body}")
