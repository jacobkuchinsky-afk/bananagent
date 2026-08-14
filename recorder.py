"""Push-to-talk microphone capture.

Records into memory while the hotkey is held, and exposes a live level so the
overlay's circle can react to your voice. Returns float32 mono at 16 kHz —
what Gemma's audio encoder expects — ready to wrap as a WAV.
"""

import io
import threading
import wave

import numpy as np
import sounddevice as sd

import config


def resolve_device():
    """Which input device index to record from.

    An explicit config.INPUT_DEVICE wins. Otherwise match INPUT_DEVICE_NAME
    against the device list and prefer the host API that behaves best. Falls
    back to the Windows default if nothing matches.
    """
    if config.INPUT_DEVICE is not None:
        return config.INPUT_DEVICE
    name = (config.INPUT_DEVICE_NAME or "").strip().lower()
    if not name:
        return None

    try:
        hostapis = sd.query_hostapis()
        devices = sd.query_devices()
    except Exception:
        return None

    matches = [
        (i, d) for i, d in enumerate(devices)
        if d["max_input_channels"] > 0 and name in d["name"].lower()
    ]
    if not matches:
        print(f"[recorder] no input device matching {config.INPUT_DEVICE_NAME!r}; "
              "using the Windows default")
        return None

    def rank(item):
        api = hostapis[item[1]["hostapi"]]["name"]
        order = config.HOST_API_ORDER
        return order.index(api) if api in order else len(order)

    index, dev = min(matches, key=rank)
    api = hostapis[dev["hostapi"]]["name"]
    print(f"[recorder] mic: [{index}] {dev['name']} ({api})")
    return index


class Recorder:
    def __init__(self):
        self._stream = None
        self._frames = []
        self._lock = threading.Lock()
        self._level = 0.0
        self._rate = config.SAMPLE_RATE
        self.running = False
        self.device = resolve_device()

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        if self.running:
            return
        with self._lock:
            self._frames = []
        self._level = 0.0
        self._open()
        self.running = True

    def stop(self):
        """Stop capturing and return the audio as float32 mono @ 16 kHz."""
        if not self.running:
            return np.zeros(0, dtype=np.float32)
        self.running = False
        self._close()
        self._level = 0.0

        with self._lock:
            frames = self._frames
            self._frames = []
        if not frames:
            return np.zeros(0, dtype=np.float32)

        audio = np.concatenate(frames).astype(np.float32)
        if self._rate != config.SAMPLE_RATE:
            audio = _resample(audio, self._rate, config.SAMPLE_RATE)
        return audio

    @property
    def level(self):
        """Smoothed 0..1 loudness, for the pulsing circle."""
        return self._level

    def close(self):
        self.running = False
        self._close()

    # -- internals ----------------------------------------------------------
    def _open(self):
        # Most mics happily give us 16 kHz mono. The ones that don't, we open
        # at their preferred rate and resample on the way out.
        rates = [config.SAMPLE_RATE]
        try:
            dev = sd.query_devices(self.device, "input")
            native = int(dev["default_samplerate"])
            if native != config.SAMPLE_RATE:
                rates.append(native)
        except Exception:
            rates.append(44100)

        last = None
        for rate in rates:
            try:
                stream = sd.InputStream(
                    samplerate=rate,
                    channels=1,
                    dtype="float32",
                    blocksize=config.CHUNK,
                    device=self.device,
                    callback=self._on_audio,
                )
                stream.start()
                self._stream, self._rate = stream, rate
                return
            except Exception as e:
                last = e
        raise RuntimeError(f"Could not open the microphone: {last}")

    def _close(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _on_audio(self, indata, _frames, _time, _status):
        block = indata[:, 0].copy()
        with self._lock:
            # Stop accumulating if the key somehow gets stuck down.
            if len(self._frames) * block.size < config.MAX_SECONDS * self._rate:
                self._frames.append(block)

        rms = float(np.sqrt(np.mean(block**2))) if block.size else 0.0
        # Speech RMS sits down around 0.02-0.2, so stretch it out, then ease
        # toward the target so the animation doesn't twitch.
        target = min(1.0, rms * 6.0)
        self._level += (target - self._level) * 0.35


def _resample(audio, src_rate, dst_rate):
    if src_rate == dst_rate or audio.size == 0:
        return audio
    dst_n = int(audio.size / float(src_rate) * dst_rate)
    if dst_n <= 0:
        return np.zeros(0, dtype=np.float32)
    src_idx = np.linspace(0, audio.size - 1, dst_n)
    return np.interp(src_idx, np.arange(audio.size), audio).astype(np.float32)


def to_wav_bytes(audio, rate=None):
    """Wrap float32 mono samples as a 16-bit PCM WAV, in memory."""
    rate = rate or config.SAMPLE_RATE
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def is_silent(audio):
    """True if this clip is too quiet to be worth sending anywhere."""
    return audio.size == 0 or float(np.abs(audio).max()) < config.SILENCE_PEAK


def list_devices():
    """Print input device indices, for setting config.INPUT_DEVICE."""
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            print(f"{i:>3}  {dev['name']}  @{int(dev['default_samplerate'])}Hz")


if __name__ == "__main__":
    list_devices()
