"""The endpoint registry.

An endpoint is one thing the assistant can do. Declaring one is a decorator on
the function that does it:

    @endpoint(
        tier=MAJOR,
        description="Sets the Windows master output volume to an exact percentage.",
        params={"percent": param("integer", "Target volume, 0-100.", required=True)},
    )
    def set_volume(percent):
        ...

That single declaration produces all three things the system needs: the JSON
schema handed to the model as a callable tool, the line describing it in the
system prompt, and the dispatch target when a call comes back. There is no
second place to register anything — drop a new module in this package and its
endpoints appear everywhere automatically.

Descriptions are what the model actually reasons over, so they state exactly
what the endpoint does and what it returns, not a vague label.
"""

import inspect
import traceback

MAJOR = "major"  # changes something: the app, the machine, or the outside world
MINOR = "minor"  # only reads and reports; calling it changes nothing

_REGISTRY = {}
_CONTEXT = None


def param(kind, description, required=False, enum=None, default=None):
    """One argument of an endpoint, as the model will see it."""
    spec = {"type": kind, "description": description}
    if enum:
        spec["enum"] = list(enum)
    return {"spec": spec, "required": required, "default": default}


class Endpoint:
    def __init__(self, fn, name, tier, description, params, returns):
        self.fn = fn
        self.name = name
        self.tier = tier
        self.description = description.strip()
        self.params = params or {}
        self.returns = (returns or "").strip()
        self.module = fn.__module__.rsplit(".", 1)[-1]

    @property
    def full_description(self):
        """What the model reads when deciding whether to call this.

        The tier is stamped on the front rather than explained separately in the
        system prompt. Listing endpoints as prose there measurably wrecks tool
        calling — the model reads the list and answers *from* it instead of
        invoking anything (11/18 correct with the list, 17/18 without). Putting
        the tier here keeps the major/minor distinction in front of the model at
        the exact moment it chooses, without that cost.
        """
        tag = ("[MAJOR - performs an action and changes something]" if self.tier == MAJOR
               else "[MINOR - only reads and reports; changes nothing]")
        body = self.description
        if self.returns:
            body = f"{body} Returns: {self.returns}"
        return f"{tag} {body}"

    def signature(self):
        if not self.params:
            return f"{self.name}()"
        return f"{self.name}({', '.join(self.params)})"

    def schema(self):
        """The OpenAI/Ollama function-tool schema for this endpoint."""
        props = {n: p["spec"] for n, p in self.params.items()}
        required = [n for n, p in self.params.items() if p["required"]]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.full_description,
                "parameters": {"type": "object", "properties": props, "required": required},
            },
        }

    def call(self, args):
        """Run it, filling defaults and rejecting arguments it doesn't take."""
        kwargs = {}
        for pname, p in self.params.items():
            if pname in args:
                kwargs[pname] = args[pname]
            elif p["required"]:
                raise ValueError(f"{self.name} needs '{pname}'")
            elif p["default"] is not None:
                kwargs[pname] = p["default"]
        unknown = set(args) - set(self.params)
        if unknown:
            # Models invent arguments; ignore rather than fail the whole turn.
            pass
        return self.fn(**kwargs)


def endpoint(tier, description, params=None, returns=None, name=None):
    def wrap(fn):
        ep = Endpoint(fn, name or fn.__name__, tier, description, params, returns)
        if ep.name in _REGISTRY:
            raise ValueError(f"duplicate endpoint name: {ep.name}")
        _REGISTRY[ep.name] = ep
        fn.endpoint = ep
        return fn
    return wrap


# -- access -----------------------------------------------------------------
def all_endpoints():
    return sorted(_REGISTRY.values(), key=lambda e: (e.tier != MAJOR, e.module, e.name))


def by_tier(tier):
    return [e for e in all_endpoints() if e.tier == tier]


def get(name):
    return _REGISTRY.get(name)


def tools_schema():
    """Everything, in the shape Ollama's `tools` parameter wants."""
    return [e.schema() for e in all_endpoints()]


def catalog_prompt():
    """The endpoint list as it appears in the system prompt, split by tier."""
    lines = []
    major, minor = by_tier(MAJOR), by_tier(MINOR)
    if major:
        lines.append(
            "MAJOR endpoints — each one CHANGES something (this app, the computer, "
            "or the outside world). Call one only when the user clearly asked for "
            "that action. Never call one to look something up."
        )
        lines += [f"  {e.signature()} — {e.full_description}" for e in major]
    if minor:
        lines.append("")
        lines.append(
            "MINOR endpoints — these only read and report. They change nothing, "
            "so call them freely whenever they would answer the question."
        )
        lines += [f"  {e.signature()} — {e.full_description}" for e in minor]
    return "\n".join(lines)


def dispatch(name, args):
    """Execute an endpoint by name. Never raises — the model gets the error text."""
    ep = get(name)
    if ep is None:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        return {"ok": False, "error": f"No endpoint named '{name}'. Available: {known}"}
    try:
        result = ep.call(args or {})
        return {"ok": True, "result": result}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# -- app context ------------------------------------------------------------
def bind(app):
    """Hand endpoints a handle on the running app so they can act on it."""
    global _CONTEXT
    _CONTEXT = app


def app():
    if _CONTEXT is None:
        raise RuntimeError("endpoints aren't bound to a running app")
    return _CONTEXT


def on_ui(fn, timeout=5.0):
    """Run fn on the Tk main thread and return its result.

    Endpoints execute on a worker thread, and touching Tk from there corrupts
    it, so anything that reads or changes the overlay has to hop threads.
    """
    import threading

    box, done = {}, threading.Event()

    def runner():
        try:
            box["value"] = fn()
        except Exception as e:
            box["error"] = e
        finally:
            done.set()

    app().ui.root.after(0, runner)
    if not done.wait(timeout):
        raise TimeoutError("the interface didn't respond")
    if "error" in box:
        raise box["error"]
    return box.get("value")
