"""Endpoints that touch the computer rather than the assistant.

Volume, clipboard, power state, time. Kept dependency-light: everything here
uses ctypes against Win32 except the volume mixer, which needs pycaw.
"""

import ctypes
import ctypes.wintypes as wintypes
import datetime
import subprocess
import webbrowser

from .registry import MAJOR, MINOR, endpoint, on_ui, param, app


def _mixer():
    """The default output device's volume interface.

    pycaw 2025+ hands back an AudioDevice that exposes EndpointVolume directly;
    the older CLSCTX_ALL/Activate dance you'll see in most examples fails here.
    COM is per-thread, and endpoints run on a worker, so initialise it first.
    """
    import comtypes
    from pycaw.utils import AudioUtilities

    try:
        comtypes.CoInitialize()
    except Exception:
        pass  # already initialised on this thread
    return AudioUtilities.GetSpeakers().EndpointVolume


# =============================================================================
# MINOR — read only
# =============================================================================

@endpoint(
    tier=MINOR,
    description=(
        "Returns the current local date and time on this computer, including the "
        "day of the week. The stamp on the user's latest message already gives the "
        "time it was sent; use this when you need it to the second right now, "
        "such as partway through a long job."
    ),
    returns="An object with iso, time_12h, date, weekday.",
)
def get_time():
    now = datetime.datetime.now()
    return {
        "iso": now.isoformat(timespec="seconds"),
        "time_12h": now.strftime("%I:%M %p").lstrip("0"),
        "date": now.strftime("%B %d, %Y"),
        "weekday": now.strftime("%A"),
    }


@endpoint(
    tier=MINOR,
    description=(
        "Reports this computer's memory use and battery state: gigabytes of RAM "
        "free and total, percent of memory in use, battery charge percent, and "
        "whether it is plugged in."
    ),
    returns="An object with ram_free_gb, ram_total_gb, ram_percent_used, battery_percent, on_ac_power.",
)
def system_stats():
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    mem = MEMORYSTATUSEX()
    mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))

    class SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [("ACLineStatus", wintypes.BYTE), ("BatteryFlag", wintypes.BYTE),
                    ("BatteryLifePercent", wintypes.BYTE), ("SystemStatusFlag", wintypes.BYTE),
                    ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]

    power = SYSTEM_POWER_STATUS()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(power))
    charge = int(power.BatteryLifePercent)

    return {
        "ram_free_gb": round(mem.ullAvailPhys / 1024**3, 2),
        "ram_total_gb": round(mem.ullTotalPhys / 1024**3, 2),
        "ram_percent_used": int(mem.dwMemoryLoad),
        "battery_percent": charge if charge <= 100 else None,
        "on_ac_power": bool(power.ACLineStatus == 1),
    }


@endpoint(
    tier=MINOR,
    description=(
        "Returns the computer's current output volume as a percentage and whether "
        "it is muted. Reads only; does not change anything."
    ),
    returns="An object with percent and muted.",
)
def get_volume():
    vol = _mixer()
    return {"percent": round(vol.GetMasterVolumeLevelScalar() * 100),
            "muted": bool(vol.GetMute())}


@endpoint(
    tier=MINOR,
    description=(
        "Returns the text currently on the Windows clipboard. Returns an empty "
        "string if the clipboard is empty or holds something that isn't text."
    ),
    returns="The clipboard text.",
)
def clipboard_read():
    def do():
        try:
            return app().ui.root.clipboard_get()
        except Exception:
            return ""
    return on_ui(do)


# =============================================================================
# MAJOR — these change the machine
# =============================================================================

@endpoint(
    tier=MAJOR,
    description=(
        "Sets this computer's master output volume to an exact percentage, "
        "immediately. 0 is silent and 100 is maximum. This does not unmute; use "
        "set_mute for that."
    ),
    params={"percent": param("integer", "Target volume, 0 to 100.", required=True)},
)
def set_volume(percent):
    pct = int(percent)
    if not 0 <= pct <= 100:
        raise ValueError("Volume must be between 0 and 100.")
    _mixer().SetMasterVolumeLevelScalar(pct / 100.0, None)
    return f"Volume set to {pct}%."


@endpoint(
    tier=MAJOR,
    description=(
        "Mutes or unmutes this computer's audio output. Muting leaves the volume "
        "level untouched, so unmuting restores it."
    ),
    params={"muted": param("boolean", "True to mute, false to unmute.", required=True)},
)
def set_mute(muted):
    _mixer().SetMute(bool(muted), None)
    return "Audio muted." if muted else "Audio unmuted."


@endpoint(
    tier=MAJOR,
    description=(
        "Sends a media key to whatever is currently playing: play or pause, next "
        "track, previous track, stop. Works with Spotify, YouTube in a browser, "
        "and anything else that responds to media keys."
    ),
    params={"action": param(
        "string", "Which media key to press.", required=True,
        enum=["play_pause", "next", "previous", "stop"])},
)
def media_control(action):
    keys = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}
    choice = str(action).strip().lower()
    if choice not in keys:
        raise ValueError(f"Unknown action '{action}'. Choose from: {', '.join(keys)}")
    vk = keys[choice]
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
    return f"Sent {choice.replace('_', '/')}."


@endpoint(
    tier=MAJOR,
    description=(
        "Replaces the contents of the Windows clipboard with the given text, so "
        "the user can paste it. Whatever was on the clipboard is lost."
    ),
    params={"text": param("string", "The text to place on the clipboard.", required=True)},
)
def clipboard_write(text):
    def do():
        root = app().ui.root
        root.clipboard_clear()
        root.clipboard_append(str(text))
        root.update_idletasks()
    on_ui(do)
    return f"Copied {len(str(text))} characters to the clipboard."


@endpoint(
    tier=MAJOR,
    description=(
        "Puts a web page on screen in the default browser and does nothing "
        "further — it cannot read the page, fill anything in, or click "
        "anything. Use it only when the user just wants to look at something. "
        "If they want something *done* on a website, that is the browsing "
        "agent's job, not this."
    ),
    params={"url": param("string", "The full URL to open.", required=True)},
)
def open_url(url):
    target = str(url).strip()
    if not target.startswith(("http://", "https://")):
        raise ValueError("The address must start with http:// or https://")
    webbrowser.open(target)
    return f"Opened {target}."


@endpoint(
    tier=MAJOR,
    description=(
        "Locks this computer, requiring the password to get back in. Nothing is "
        "closed and nothing is lost. Only call this when clearly asked to lock."
    ),
)
def lock_screen():
    if not ctypes.windll.user32.LockWorkStation():
        raise RuntimeError("Windows refused to lock the workstation.")
    return "Locked."
