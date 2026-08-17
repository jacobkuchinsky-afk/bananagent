"""Endpoints for the assistant's own voice.

None of these touch Tk, so unlike the overlay and dashboard endpoints they run
straight on the worker thread.
"""

from .registry import MAJOR, MINOR, app, endpoint, param

ACCENTS = {
    "a": "American", "b": "British", "e": "Spanish", "f": "French",
    "h": "Hindi", "i": "Italian", "j": "Japanese", "p": "Portuguese",
    "z": "Chinese",
}


def _speaker():
    speaker = getattr(app(), "speaker", None)
    if speaker is None:
        raise RuntimeError("speech isn't set up")
    return speaker


# =============================================================================
# MINOR — read only
# =============================================================================

@endpoint(
    tier=MINOR,
    description=(
        "Reports how the assistant's spoken voice is configured: whether it "
        "speaks replies aloud at all, which voice it uses, how fast, and "
        "whether it is talking right now."
    ),
    returns="An object with enabled, ready, voice, speed, speaking and voices_available.",
)
def speech_status():
    return _speaker().status()


@endpoint(
    tier=MINOR,
    description=(
        "Lists the voices available for speaking, grouped by accent. Voice names "
        "look like 'af_heart' or 'bm_george', where the first letter is the "
        "accent and the second is the speaker's gender. Call this before "
        "changing the voice."
    ),
    returns="An object mapping each accent to its voice names.",
)
def list_voices():
    grouped = {}
    for name in _speaker().voices():
        accent = ACCENTS.get(name[:1], "other")
        gender = "female" if name[1:2] == "f" else "male"
        grouped.setdefault(f"{accent} {gender}", []).append(name)
    return grouped


# =============================================================================
# MAJOR — these change how it sounds
# =============================================================================

@endpoint(
    tier=MAJOR,
    description=(
        "Changes the voice the assistant speaks in, for every reply from now on. "
        "Takes an exact voice name such as 'af_heart' or 'bm_george'; call "
        "list_voices first if the user asked for something like a British man "
        "rather than naming one."
    ),
    params={"name": param("string", "The exact voice name.", required=True)},
)
def set_voice(name):
    return f"Now speaking as {_speaker().set_voice(name)}."


@endpoint(
    tier=MAJOR,
    description=(
        "Changes how fast the assistant talks. 1.0 is normal, 0.5 is half speed "
        "and 2.0 is double. Applies to every reply from now on."
    ),
    params={"speed": param("number", "Speaking rate, 0.5 to 2.0.", required=True)},
)
def set_speech_speed(speed):
    return f"Speaking at {_speaker().set_speed(speed)}x."


@endpoint(
    tier=MAJOR,
    description=(
        "Turns speaking replies aloud on or off. When off the assistant still "
        "answers, but only in text on the overlay. Use when asked to be quiet, "
        "to stop talking, or to speak up again."
    ),
    params={"enabled": param("boolean", "True to speak replies aloud.", required=True)},
)
def set_speech_enabled(enabled):
    return "Speaking replies aloud." if _speaker().set_enabled(enabled) else "Going quiet."


@endpoint(
    tier=MAJOR,
    description=(
        "Stops talking immediately, cutting off the sentence in progress and "
        "discarding the rest of the answer. Speech stays enabled for the next "
        "question. Use when told to be quiet, hush, or stop talking."
    ),
)
def stop_speaking():
    _speaker().stop()
    return "Stopped."


@endpoint(
    tier=MAJOR,
    description=(
        "Says a specific line out loud in the assistant's current voice, "
        "interrupting anything it is already saying. Use when asked to read "
        "something out or to say a particular phrase."
    ),
    params={"text": param("string", "Exactly what to say.", required=True)},
)
def speak_text(text):
    _speaker().say(text)
    return f"Saying: {text}"
