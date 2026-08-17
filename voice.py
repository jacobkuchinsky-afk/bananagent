"""Says the model's replies out loud, with Kokoro, while they are still arriving.

Synthesis runs about three times faster than real time on the CPU, so waiting
for a complete reply before speaking would put a second or two of silence in
front of every answer. Instead the text is cut at sentence boundaries the
moment each one is complete, and three stages run at once: tokens arrive and
are split, a synth thread turns sentences into audio, and a playback thread
drains them into one long-lived output stream. Speaking therefore starts on the
first sentence and the rest arrives behind it, gaplessly.

Everything is keyed on a generation number. Pressing the hotkey again bumps it,
which makes both threads drop whatever they were carrying and aborts the audio
already handed to the sound card — so a new question cuts the old answer off
mid-word instead of queueing behind it.

If the weights aren't on disk this degrades to silence and the assistant runs
exactly as it did before, which is why nothing here is imported at module load.
"""

import os
import queue
import re
import threading

import numpy as np
import sounddevice as sd

import config

RATE = 24000  # what Kokoro emits

# A sentence ends at . ! ? … followed by space or end of text, or at a newline.
# The digit guard keeps version numbers and decimals ("0.20.7", "3.5 seconds")
# from being chopped into pieces and read with a pause in the middle.
_END = re.compile(r"(?<!\d)[.!?…](?=\s|$)|\n")
_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")
_NOISE = re.compile(r"[*_`#]+")          # markdown the model slipped in anyway
_FLUSH_AT = 220                          # speak anyway past this, punctuation or not


def _here(name):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def _split(buffer):
    """Split off every complete sentence, returning them and the remainder."""
    end = 0
    for match in _END.finditer(buffer):
        end = match.end()
    if end <= 0:
        if len(buffer) < _FLUSH_AT:
            return [], buffer
        # A very long clause with no punctuation in sight: break at a word so
        # the speech keeps up with the text instead of stalling.
        cut = buffer.rfind(" ", 0, _FLUSH_AT)
        if cut <= 0:
            return [], buffer
        return [buffer[:cut].strip()], buffer[cut:]
    head, tail = buffer[:end], buffer[end:]
    return [p.strip() for p in _SPLIT.split(head) if p.strip()], tail


def _drain(q):
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


class Speaker:
    def __init__(self):
        self.enabled = bool(config.SPEAK)
        self.voice = config.VOICE
        self.speed = float(config.VOICE_SPEED)
        self.ready = False
        self.error = None

        self._kokoro = None
        self._voices = []
        self._buffer = ""
        self._gen = 0
        self._pending = 0
        self._lock = threading.Lock()
        self._text = queue.Queue()
        self._audio = queue.Queue(maxsize=6)
        self._stream = None

        threading.Thread(target=self._load, daemon=True).start()
        threading.Thread(target=self._synth_loop, daemon=True).start()
        threading.Thread(target=self._play_loop, daemon=True).start()

    # -- state ---------------------------------------------------------------
    @property
    def speaking(self):
        with self._lock:
            return self._pending > 0

    def voices(self):
        return list(self._voices)

    def status(self):
        return {
            "enabled": self.enabled,
            "ready": self.ready,
            "voice": self.voice,
            "speed": round(self.speed, 2),
            "speaking": self.speaking,
            "voices_available": len(self._voices),
            "error": self.error,
        }

    # -- setup ---------------------------------------------------------------
    def _load(self):
        """Bring Kokoro up in the background; the app must not wait on it."""
        model, voices = _here(config.TTS_MODEL), _here(config.TTS_VOICES)
        missing = [p for p in (model, voices) if not os.path.exists(p)]
        if missing:
            self.error = f"missing {', '.join(os.path.basename(p) for p in missing)}"
            print(f"[voice] {self.error}; replies won't be spoken (see the README)")
            return
        try:
            from kokoro_onnx import Kokoro

            self._kokoro = Kokoro(model, voices)
            self._voices = sorted(self._kokoro.get_voices())
            if self.voice not in self._voices:
                print(f"[voice] '{self.voice}' isn't a Kokoro voice; using af_heart")
                self.voice = "af_heart"
            # The first synthesis of a session is roughly half speed, so spend
            # that cost at startup rather than on the first answer.
            self._kokoro.create("Ready.", voice=self.voice, speed=1.0,
                                lang=config.VOICE_LANG)
            self.ready = True
            print(f"[voice] kokoro ready — {len(self._voices)} voices, using {self.voice}")
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            print(f"[voice] couldn't start Kokoro: {self.error}")

    # -- input ---------------------------------------------------------------
    def feed(self, chunk):
        """Take a streamed piece of the reply and speak whatever it completes."""
        if not (self.enabled and self.ready):
            return
        with self._lock:
            self._buffer += chunk
            sentences, self._buffer = _split(self._buffer)
            gen = self._gen
            self._pending += len(sentences)
        for sentence in sentences:
            self._text.put((gen, sentence))

    def finish(self):
        """Speak whatever is left once the reply has stopped arriving."""
        if not (self.enabled and self.ready):
            return
        with self._lock:
            tail, self._buffer = self._buffer.strip(), ""
            gen = self._gen
            if tail:
                self._pending += 1
        if tail:
            self._text.put((gen, tail))

    def say(self, text):
        """Speak one arbitrary line, cancelling anything in progress."""
        if not self.ready:
            raise RuntimeError(self.error or "the voice isn't ready yet")
        self.stop()
        with self._lock:
            gen = self._gen
            self._pending += 1
        self._text.put((gen, str(text).strip()))
        return True

    def stop(self):
        """Cut off immediately: drop queued text, queued audio, and playback."""
        with self._lock:
            self._gen += 1
            self._buffer = ""
            self._pending = 0
        _drain(self._text)
        _drain(self._audio)
        if self._stream is not None:
            try:
                self._stream.abort()  # discards what the sound card still holds
                self._stream.start()
            except Exception:
                self._stream = None
        return True

    # -- settings ------------------------------------------------------------
    def set_voice(self, name):
        name = str(name).strip().lower()
        if self._voices and name not in self._voices:
            raise ValueError(f"No voice called '{name}'.")
        self.voice = name
        return name

    def set_speed(self, speed):
        speed = float(speed)
        if not 0.5 <= speed <= 2.0:
            raise ValueError("Speed must be between 0.5 and 2.0.")
        self.speed = speed
        return speed

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        if not self.enabled:
            self.stop()
        return self.enabled

    # -- threads -------------------------------------------------------------
    def _stale(self, gen):
        with self._lock:
            return gen != self._gen

    def _done_one(self):
        with self._lock:
            self._pending = max(0, self._pending - 1)

    def _synth_loop(self):
        while True:
            gen, text = self._text.get()
            if self._stale(gen) or not self.ready:
                self._done_one()
                continue
            try:
                clean = _NOISE.sub("", text).strip()
                if not clean:
                    self._done_one()
                    continue
                samples, rate = self._kokoro.create(
                    clean, voice=self.voice, speed=self.speed, lang=config.VOICE_LANG)
                if self._stale(gen):
                    self._done_one()
                    continue
                self._audio.put((gen, np.asarray(samples, dtype=np.float32), rate))
            except Exception as e:
                print(f"[voice] couldn't speak {text[:40]!r}: {type(e).__name__}: {e}")
                self._done_one()

    def _play_loop(self):
        while True:
            gen, samples, rate = self._audio.get()
            try:
                if self._stale(gen):
                    continue
                stream = self._out(rate)
                if stream is not None:
                    stream.write(samples)
            except Exception as e:
                print(f"[voice] playback failed: {type(e).__name__}: {e}")
                self._stream = None
            finally:
                self._done_one()

    def _out(self, rate):
        """One output stream, kept open, so sentences run together seamlessly."""
        if self._stream is None:
            try:
                self._stream = sd.OutputStream(samplerate=rate, channels=1,
                                               dtype="float32")
                self._stream.start()
            except Exception as e:
                print(f"[voice] no output device: {type(e).__name__}: {e}")
                self._stream = None
        return self._stream

    def close(self):
        self.stop()
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None


if __name__ == "__main__":
    # `python -m voice` lists the voices; add names to hear them.
    import sys
    import time

    speaker = Speaker()
    while not (speaker.ready or speaker.error):
        time.sleep(0.1)
    if not speaker.ready:
        raise SystemExit(f"[voice] {speaker.error}")

    wanted = [v for v in sys.argv[1:]] or None
    if not wanted:
        by_accent = {}
        for name in speaker.voices():
            by_accent.setdefault(name[:2], []).append(name)
        for prefix, names in sorted(by_accent.items()):
            print(f"{prefix}  {' '.join(names)}")
        print(f"\n{len(speaker.voices())} voices. "
              f"Pass names to hear them: python -m voice af_heart bm_george")
        wanted = [config.VOICE]

    for name in wanted:
        print(f"speaking as {name}…")
        speaker.set_voice(name)
        speaker.say(f"This is {name.replace('_', ' ')}. "
                    "The model is holding a third of itself on the graphics card.")
        while speaker.speaking:
            time.sleep(0.1)
        time.sleep(0.3)
    speaker.close()
