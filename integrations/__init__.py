"""Outside platforms: services BananaGent signs in to on the user's behalf.

A platform is a name, a title, and three things it can do — connect (sign in,
usually by opening a page in the browser), disconnect (sign out and forget the
login), and status (connected or not, and as whom). Logins are kept in
INTEGRATION_TOKENS_DIR, one file per platform, so each survives restarts and
each can be a different account.

Adding a platform is a module in this folder that calls register(Platform(...)),
plus one endpoint in endpoints/ for what it can do once connected. Every
platform shares the same two endpoints for signing in and checking
(endpoints/integrations.py), so a new platform costs the model's tool list one
entry, not three (see "Why there are only four of them" in the README).
"""

import importlib
import pkgutil
import traceback
from dataclasses import dataclass, field
from typing import Callable


class NotConnected(RuntimeError):
    """The platform has no login yet, or its login stopped working."""


@dataclass
class Platform:
    name: str                                # what integration_connect takes
    title: str                               # what the user calls it
    description: str                         # one line: what connecting gives you
    connect: Callable = field(repr=False)    # () -> str, blocks until signed in
    disconnect: Callable = field(repr=False)  # () -> str
    status: Callable = field(repr=False)     # () -> dict, never raises


PLATFORMS = {}


def register(platform):
    PLATFORMS[platform.name] = platform
    return platform


def get(name):
    platform = PLATFORMS.get(str(name or "").strip().lower())
    if platform is None:
        raise ValueError(f"No platform called '{name}'. "
                         f"Available: {', '.join(sorted(PLATFORMS)) or 'none'}")
    return platform


def overview():
    """Every platform and whether it's connected — what integrations() returns."""
    out = []
    for p in PLATFORMS.values():
        try:
            state = p.status()
        except Exception as e:
            state = {"connected": False, "note": f"{type(e).__name__}: {e}"}
        out.append({"platform": p.name, "title": p.title,
                    "gives": p.description, **state})
    return out


_NOT_PLATFORMS = {"google"}  # shared helpers, not platforms of their own


def _discover():
    # One platform that won't load (a missing package, a half-written module)
    # is left out rather than taking the whole app down with it.
    for info in pkgutil.iter_modules(__path__):
        if info.name not in _NOT_PLATFORMS:
            try:
                importlib.import_module(f"{__name__}.{info.name}")
            except Exception:
                traceback.print_exc()


_discover()
