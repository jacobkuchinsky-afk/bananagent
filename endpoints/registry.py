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
endpoints appear everywhere automatically. The one exception is tools the coding
agent makes for BananaGent, which are declared in files rather than code: they
come in through register() and stay current through refresh() (see toolbox.py).

Descriptions are what the model actually reasons over, so they state exactly
what the endpoint does and what it returns, not a vague label.
"""

import inspect
import traceback

MAJOR = "major"  # changes something: the app, the machine, or the outside world
MINOR = "minor"  # only reads and reports; calling it changes nothing

# Who an endpoint is for. The assistant's tool list is what it reads before
# every reply, and the longer it gets the worse its tool choice (see the
# README), so tools only another model needs live in a scope of their own —
# the coding agent's sandbox and Claude tools are "coder". A scoped endpoint is
# left out of the assistant's catalog and refused if it calls one anyway.
ASSISTANT = "assistant"
CODER = "coder"
# Part of the call protocol rather than a job — keep_going — so every scope
# gets it, and every catalog lists it.
EVERYONE = "everyone"

_REGISTRY = {}
_CONTEXT = None


def param(kind, description, required=False, enum=None, default=None):
    """One argument of an endpoint, as the model will see it."""
    spec = {"type": kind, "description": description}
    if enum:
        spec["enum"] = list(enum)
    return {"spec": spec, "required": required, "default": default}


class Endpoint:
    def __init__(self, fn, name, tier, description, params, returns, agent=None,
                 scope=ASSISTANT, result_chars=None):
        self.fn = fn
        self.name = name
        self.tier = tier
        self.description = description.strip()
        self.params = params or {}
        self.returns = (returns or "").strip()
        self.module = fn.__module__.rsplit(".", 1)[-1]
        # Which agent this drives, if any — the dashboard groups by it.
        self.agent = agent
        self.scope = scope
        # How much of a result the model is shown; None is TOOL_RESULT_CHARS.
        # Tools that hand back a big piece of work get more.
        self.result_chars = result_chars

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


def endpoint(tier, description, params=None, returns=None, name=None, agent=None,
             scope=ASSISTANT, result_chars=None):
    def wrap(fn):
        ep = Endpoint(fn, name or fn.__name__, tier, description, params, returns, agent,
                      scope, result_chars)
        register(ep)
        fn.endpoint = ep
        return fn
    return wrap


def register(ep):
    """Add an endpoint made at runtime rather than by the decorator — the
    tools the coding agent makes (toolbox.py)."""
    if ep.name in _REGISTRY:
        raise ValueError(f"duplicate endpoint name: {ep.name}")
    _REGISTRY[ep.name] = ep
    return ep


def unregister(name):
    return _REGISTRY.pop(name, None)


# Called before the registry is read, so endpoints kept on disk (toolbox.py)
# can be brought up to date — added, changed or removed — without a restart.
_REFRESHERS = []


def on_refresh(fn):
    _REFRESHERS.append(fn)
    return fn


def refresh():
    for fn in _REFRESHERS:
        try:
            fn()
        except Exception:
            traceback.print_exc()


# -- access -----------------------------------------------------------------
def all_endpoints(scope=ASSISTANT):
    """Every endpoint in one scope (EVERYONE's included), or in all of them
    with scope=None."""
    refresh()
    return sorted((e for e in list(_REGISTRY.values())
                   if scope is None or e.scope in (scope, EVERYONE)),
                  key=lambda e: (e.tier != MAJOR, e.module, e.name))


def names(scope=ASSISTANT):
    return {e.name for e in all_endpoints(scope)}


def by_tier(tier):
    return [e for e in all_endpoints() if e.tier == tier]


def by_agent(name):
    """Everything that drives one agent — what its dashboard row lists."""
    return [e for e in all_endpoints(None) if e.agent == name]


def get(name):
    refresh()
    return _REGISTRY.get(name)


def tools_schema():
    """The assistant's tools, in the shape Ollama's `tools` parameter wants."""
    return [e.schema() for e in all_endpoints()]


def catalog_prompt(names=None):
    """Every endpoint as the model reads it in the system prompt — or just
    those in `names`, which is how a subagent sees only its own tools.

    The model calls tools by writing [[call name {json}]] (see brain.py), so
    this is the only place it learns what exists and what arguments each
    takes. One entry per tool: the name, its tier, what it does, then each
    argument with its type, whether it's required, and its allowed values.
    """
    def entry(e):
        head = f"- {e.name} ({e.tier.upper()}): {e.description}"
        if e.returns:
            head += f" Returns: {e.returns}"
        lines = [head]
        for pname, p in e.params.items():
            spec = p["spec"]
            bits = [spec["type"], "required" if p["required"] else "optional"]
            if spec.get("enum"):
                bits.append("one of " + ", ".join(map(str, spec["enum"])))
            lines.append(f"    {pname} ({'; '.join(bits)}): {spec['description']}")
        return "\n".join(lines)

    chosen = all_endpoints() if names is None else \
        [e for e in all_endpoints(None) if e.name in names]
    return "\n".join(entry(e) for e in chosen)


def dispatch(name, args):
    """Execute an endpoint by name. Never raises — the model gets the error text."""
    ep = get(name)
    if ep is None:
        known = ", ".join(sorted(list(_REGISTRY))) or "none"
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
