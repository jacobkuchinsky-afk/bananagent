"""Everything the dashboard shows, as one plain dictionary.

The dashboard paints this and the dashboard_diagnostics endpoint returns it, so
what you read on screen and what the model says out loud can't disagree.

Collection touches the network, so it never runs on the Tk thread — the
dashboard polls it from a worker and the endpoint is already off-thread. Every
field degrades to None rather than raising: a dashboard that renders "unknown"
is more useful than one that fails to draw because the server went away.

The model it reports on is the one answering messages: config.MODEL on
config.OLLAMA_HOST, the DGX Spark.
"""

import re
import subprocess
import time
from datetime import datetime, timezone

import httpx

import config

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_GB = 1024 ** 3

_gpu = None  # (name, total_vram_bytes), probed once


def _api(route, timeout=2.5, **body):
    url = config.OLLAMA_HOST.rstrip("/") + route
    try:
        r = httpx.post(url, json=body, timeout=timeout) if body else httpx.get(url, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _round(value, places=2, scale=_GB):
    return None if value is None else round(value / scale, places)


def _expires_in(stamp):
    """Seconds until Ollama unloads the model, from its ISO timestamp."""
    if not stamp:
        return None
    try:
        # Ollama emits nanoseconds; fromisoformat tops out at microseconds.
        trimmed = re.sub(r"(\.\d{6})\d+", r"\1", str(stamp))
        when = datetime.fromisoformat(trimmed)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        left = (when - datetime.now(timezone.utc)).total_seconds()
        return round(left, 1) if left > 0 else 0.0
    except Exception:
        return None


def gpu():
    """Name and total VRAM of the graphics card, probed once and remembered."""
    global _gpu
    if _gpu is None:
        _gpu = ("", None)
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=6, creationflags=_NO_WINDOW)
            if out.returncode == 0 and out.stdout.strip():
                name, mib = out.stdout.strip().splitlines()[0].split(",")
                _gpu = (name.strip(), int(float(mib)) * 1024 * 1024)
        except Exception:
            pass
    return _gpu


def snapshot(app=None):
    """One reading of everything. Never raises."""
    version = (_api("/api/version") or {}).get("version")
    model = config.MODEL

    tags = _api("/api/tags") or {}
    on_disk = next((m.get("size") for m in tags.get("models", [])
                    if (m.get("model") or m.get("name")) == model), None)

    shown = _api("/api/show", model=model) or {}
    details = shown.get("details") or {}
    info = shown.get("model_info") or {}
    context = next((v for k, v in info.items() if k.endswith(".context_length")), None)

    running = next((m for m in (_api("/api/ps") or {}).get("models", []) or []
                    if (m.get("model") or m.get("name")) == model), None)
    resident = running.get("size") if running else None
    in_vram = running.get("size_vram") if running else None

    gpu_name, gpu_total = gpu()

    return {
        "server": {
            "host": config.OLLAMA_HOST,
            "version": version,
            "up": bool(version),
        },
        "model": {
            "name": model,
            "parameters": details.get("parameter_size"),
            "quantization": details.get("quantization_level"),
            "family": details.get("family"),
            "context_tokens": context,
            "disk_gb": _round(on_disk),
        },
        "loaded": {
            "resident": running is not None,
            "size_gb": _round(resident),
            "vram_gb": _round(in_vram),
            # How much of the loaded model the card is holding; the rest is on
            # the CPU, which is the single biggest thing slowing a reply down.
            "on_gpu_percent": (round(100 * in_vram / resident) if resident and in_vram
                               else (0 if resident else None)),
            "unloads_in_seconds": _expires_in(running.get("expires_at")) if running else None,
            "keep_alive": config.KEEP_ALIVE,
        },
        "machine": {
            "ram_used_percent": _memory("percent"),
            "ram_free_gb": _round(_memory("available")),
            "ram_total_gb": _round(_memory("total")),
            "gpu": gpu_name or None,
            "vram_total_gb": _round(gpu_total),
        },
        "app": _app_stats(app),
        "taken_at": time.time(),
    }


def _memory(field):
    try:
        import psutil

        return getattr(psutil.virtual_memory(), field)
    except Exception:
        return None


def _app_stats(app):
    stats = {
        "uptime_seconds": None,
        "questions_answered": 0,
        "last_reply_seconds": None,
        "recent_reply_seconds": [],
        "last_message": None,
        "short_term_tokens": None,
        "long_term_facts": None,
        "hotkey": config.HOTKEY,
        "box_open": False,
    }
    if app is None:
        return stats
    try:
        stats["uptime_seconds"] = round(time.time() - app.started_at, 1)
        stats["questions_answered"] = app.turns
        stats["last_reply_seconds"] = app.last_reply
        stats["recent_reply_seconds"] = list(app.reply_times)
        stats["last_message"] = app.last_message
        import memory

        stats["short_term_tokens"] = memory.short_term_tokens()
        stats["long_term_facts"] = sum(1 for line in memory.render().splitlines()
                                       if line.lstrip().startswith(("- ", "* ")))
        stats["box_open"] = app.ui.state == app.ui.TYPING
    except Exception:
        pass
    return stats
