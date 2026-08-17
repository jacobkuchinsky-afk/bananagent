"""Endpoint package.

Importing this imports every module beside it, which is what registers their
endpoints. To add capabilities, drop a new module in this folder — nothing here
needs editing, and it will show up in the model's tool list and in the system
prompt on the next run.
"""

import importlib
import pkgutil

from .registry import (  # noqa: F401  (re-exported for endpoint modules to use)
    MAJOR,
    MINOR,
    Endpoint,
    all_endpoints,
    app,
    bind,
    by_agent,
    by_tier,
    catalog_prompt,
    dispatch,
    endpoint,
    get,
    on_ui,
    param,
    tools_schema,
)


_NOT_ENDPOINTS = {"registry", "__main__"}


def _discover():
    for info in pkgutil.iter_modules(__path__):
        if info.name not in _NOT_ENDPOINTS:
            importlib.import_module(f"{__name__}.{info.name}")


_discover()
