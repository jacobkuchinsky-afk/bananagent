"""Sends recorded audio straight to the local Gemma model and streams the reply.

No speech-to-text step: gemma4:e4b is natively multimodal, so the WAV goes in
as-is. Ollama carries audio in the message's `images` array — it's a generic
media field despite the name; a dedicated `audio` field is silently ignored.
"""

import json
import os
import subprocess
import time
from urllib.parse import urlparse

import httpx
import ollama

import config
import endpoints
import prompts

_resolved_model = None
_client = None
_server = None


def _here(name):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def client():
    global _client
    if _client is None:
        _client = ollama.Client(host=config.OLLAMA_HOST, timeout=config.REQUEST_TIMEOUT)
    return _client


def server_version():
    """Version string of the server we're pointed at, or None if it's down."""
    try:
        r = httpx.get(config.OLLAMA_HOST.rstrip("/") + "/api/version", timeout=3.0)
        return r.json().get("version") if r.status_code == 200 else None
    except Exception:
        return None


def ensure_server():
    """Start our pinned Ollama on its own port if nothing is answering there.

    Deliberately does not touch the system Ollama on 11434 — that one keeps
    running normally for everything else.
    """
    global _server
    version = server_version()
    if version:
        return version

    exe = config.OLLAMA_SERVE_EXE
    if not exe or not os.path.exists(exe):
        raise RuntimeError(
            f"Nothing is listening on {config.OLLAMA_HOST} and "
            f"OLLAMA_SERVE_EXE ({exe!r}) doesn't exist."
        )

    port = urlparse(config.OLLAMA_HOST).port or 11434
    env = dict(os.environ, OLLAMA_HOST=f"127.0.0.1:{port}")
    print(f"[brain] starting pinned ollama on port {port}…")
    # Keep the server's own output — when the audio runner wedges, its log is
    # the only place that says why.
    log = open(_here("ollama-pinned.log"), "a", buffering=1, encoding="utf-8", errors="replace")
    _server = subprocess.Popen(
        [exe, "serve"], env=env,
        stdout=log, stderr=log,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    deadline = time.time() + config.OLLAMA_START_TIMEOUT
    while time.time() < deadline:
        version = server_version()
        if version:
            return version
        if _server.poll() is not None:
            raise RuntimeError(f"Pinned ollama exited immediately (code {_server.returncode})")
        time.sleep(0.5)
    raise RuntimeError(f"Pinned ollama didn't come up within {config.OLLAMA_START_TIMEOUT}s")


def resolve_model() -> str:
    """Pick a model that actually exists locally.

    config.MODEL wins if it's pulled. Otherwise take the closest gemma, then
    anything installed — so a half-finished `ollama pull` doesn't leave the app
    dead in the water.
    """
    global _resolved_model
    if _resolved_model:
        return _resolved_model

    try:
        listed = client().list().get("models", [])
        names = [m.get("model") or m.get("name") for m in listed]
        names = [n for n in names if n]
    except Exception as e:
        raise RuntimeError(f"Ollama isn't reachable — is it running? ({e})")

    if not names:
        raise RuntimeError(f"Ollama has no models pulled. Try: ollama pull {config.MODEL}")

    want = config.MODEL
    if want in names:
        pick = want
    else:
        stem = want.split(":")[0]
        family = stem.rstrip("0123456789n") or "gemma"
        pick = (
            next((n for n in names if n.startswith(stem)), None)
            or next((n for n in names if n.startswith(family)), None)
            or names[0]
        )
        print(f"[brain] '{want}' not pulled yet — using '{pick}' instead.")

    _resolved_model = pick
    return pick


def warm():
    """Page the model into memory so the first real question isn't a 50s wait."""
    client().generate(model=resolve_model(), prompt="", keep_alive=config.KEEP_ALIVE)


def kill_pinned():
    """Stop our server and its runner — never the Ollama you have installed.

    Matched strictly on the executable path so the system install on 11434 is
    left alone.
    """
    global _server, _client
    if _server is not None and _server.poll() is None:
        try:
            _server.terminate()
            _server.wait(timeout=5)
        except Exception:
            try:
                _server.kill()
            except Exception:
                pass
    _server = None
    _client = None  # its connection pool points at a socket that's going away

    folder = os.path.dirname(os.path.abspath(config.OLLAMA_SERVE_EXE or ""))
    if not folder:
        return
    # The wedge lives in the runner subprocess, so the parent alone isn't enough.
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='ollama.exe'\" | "
        f"Where-Object {{ $_.ExecutablePath -like '{folder}\\*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def restart_server():
    print("[brain] pinned ollama stopped responding — restarting it")
    kill_pinned()
    time.sleep(1.5)
    return ensure_server()


def system_prompt():
    """The editable prompt from prompt.md, re-read whenever it changes.

    See config.INLINE_CATALOG — pasting the catalog in here reads well but
    measurably stops the model actually calling anything.
    """
    base = prompts.system()
    if config.INLINE_CATALOG:
        return f"{base}\n\n{endpoints.catalog_prompt()}"
    return base


def _round(messages, collected):
    """One exchange with the model.

    Yields reply text as it streams, and appends any tool calls the model made
    to `collected` — a list, because a generator's return value is awkward to
    get at through `yield from`.
    """
    calls = []
    text = []
    for part in client().chat(
        model=resolve_model(),
        messages=messages,
        tools=endpoints.tools_schema(),
        stream=True,
        think=config.THINK,
        options=config.OPTIONS,
        keep_alive=config.KEEP_ALIVE,
    ):
        msg = part.get("message", {}) or {}
        for call in msg.get("tool_calls") or []:
            calls.append(call)
        chunk = msg.get("content", "")
        if chunk:
            text.append(chunk)
            yield chunk
    collected.append({"calls": calls, "text": "".join(text)})


def _converse(wav_bytes):
    """Audio in, reply out, running any endpoints the model asks for.

    The model gets the clip, may call endpoints, sees their results, and then
    answers. Rounds are capped so a model that keeps calling tools can't spin
    forever.
    """
    messages = [
        {"role": "system", "content": system_prompt()},
        {
            "role": "user",
            "content": prompts.audio_turn(),
            "images": [wav_bytes],  # the client base64-encodes raw bytes for us
        },
    ]

    for _ in range(config.MAX_TOOL_ROUNDS):
        collected = []
        yield from _round(messages, collected)
        turn = collected[-1] if collected else {"calls": [], "text": ""}
        if not turn["calls"]:
            return

        messages.append({"role": "assistant", "content": turn["text"],
                         "tool_calls": turn["calls"]})
        for call in turn["calls"]:
            fn = (call.get("function") or {})
            name = fn.get("name", "")
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            outcome = endpoints.dispatch(name, args)
            print(f"[endpoint] {name}({args}) -> {str(outcome)[:200]}")
            messages.append({"role": "tool", "tool_name": name,
                             "content": json.dumps(outcome, default=str)})

    # Ran out of rounds with the model still calling tools.
    yield " (I stopped after too many tool calls.)"


def stream(wav_bytes: bytes):
    """Yield reply chunks for a spoken clip.

    `wav_bytes` is a complete 16 kHz mono WAV. Each clip stands alone — no
    conversation history is carried between turns.

    Gemma 4's audio runner wedges every few requests upstream (ollama#15333):
    the call just never returns. If that happens before any text arrives we
    bounce the server and try once more, which clears it. Once tokens have
    started we let the error through rather than replay a half-spoken answer.
    """
    ensure_server()
    for attempt in (1, 2):
        started = False
        try:
            for chunk in _converse(wav_bytes):
                started = True
                yield chunk
            return
        except Exception as e:
            if started or attempt == 2:
                raise RuntimeError(f"The model didn't answer ({type(e).__name__}).") from e
            restart_server()
