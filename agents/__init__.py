"""Agent package.

Importing this imports every module beside it, which is what registers their
agents — the same trick the endpoints package uses. To add an agent, drop a
module in this folder that subclasses Agent and calls register(); it appears on
the dashboard's Agents page with no wiring anywhere else.

Agents are imported lazily by design: browser.py pulls in Stagehand, which is a
heavy import, so nothing here runs until something actually asks for an agent.
"""

import importlib
import pkgutil

from .registry import (  # noqa: F401  (re-exported for agent modules to use)
    DONE,
    FAILED,
    IDLE,
    LIVE,
    STOPPED,
    WAITING,
    WORKING,
    Agent,
    Approval,
    Denied,
    Stopped,
    all_agents,
    get,
    register,
    snapshot,
    stop_all,
)

_NOT_AGENTS = {"registry", "__main__"}


def _discover():
    for info in pkgutil.iter_modules(__path__):
        if info.name not in _NOT_AGENTS:
            importlib.import_module(f"{__name__}.{info.name}")


_discover()
