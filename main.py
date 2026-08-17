"""BananaGent — hold ` (tilde), talk, release. Your voice goes to a local LLM.

Hold the key: the corner overlay wakes up and the circle pulses with your
voice. Release it: the recording goes straight to gemma4:e4b as audio — no
speech-to-text step — and the reply streams into the overlay.

Run it with the venv interpreter:  .venv\\Scripts\\python.exe main.py
Quit with ctrl+shift+q (the window is frameless, so there's no X to click).
"""

import collections
import os
import queue
import sys
import threading
import time
import traceback

import keyboard


def _ensure_output():
    """Give ourselves somewhere to print when launched without a console.

    At boot we run under pythonw.exe so no terminal flashes up, and there
    sys.stdout is None — the first print() would take the whole app down.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bananagent.log")
    log = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


_ensure_output()

import agents
import brain
import config
import endpoints
import recorder
import voice
from dashboard import Dashboard
from overlay import Overlay
from recorder import Recorder

QUIT_HOTKEY = "ctrl+shift+q"


class App:
    def __init__(self):
        self.ui = Overlay()
        self.rec = Recorder()
        self.speaker = voice.Speaker()
        self.events = queue.Queue()

        self.gen = 0  # bumps every interaction; stale workers check it and bail
        self.held = False
        self.pressed_at = 0.0
        self._hide_job = None

        # What the dashboard reports about this session.
        self.started_at = time.time()
        self.turns = 0
        self.last_reply = None
        self.reply_times = collections.deque(maxlen=40)

        # Starts minimized: the taskbar gets a banana to click, and nothing
        # opens a window in your face at login.
        self.dash = Dashboard(self.ui.root, self)

        endpoints.bind(self)  # lets endpoints act on the running app
        self._hook_keys()
        threading.Thread(target=self._warm_up, daemon=True).start()
        self.ui.root.after(16, self._drain)

    def run(self):
        print(f"[bananagent] ready — hold  {config.HOTKEY}  to talk, {QUIT_HOTKEY} to quit")
        try:
            self.ui.root.mainloop()
        finally:
            self.rec.close()
            self.speaker.close()
            # Agents outlive a single question by design, so quitting has to go
            # and shut them down — otherwise the agent's Chrome is left running.
            for agent in agents.all_agents():
                try:
                    (getattr(agent, "close", None) or agent.stop)()
                except Exception:
                    pass

    # -- input --------------------------------------------------------------
    def _hook_keys(self):
        # These callbacks fire on the keyboard library's own thread, so they
        # only ever post to the queue — never touch Tk from here.
        keyboard.hook_key(
            config.HOTKEY,
            lambda e: self.events.put((e.event_type, None)),
            suppress=config.SUPPRESS_HOTKEY,
        )
        keyboard.add_hotkey("esc", lambda: self.events.put(("dismiss", None)))
        keyboard.add_hotkey(QUIT_HOTKEY, lambda: self.events.put(("quit", None)))

    def _warm_up(self):
        """Preload the model so the first question isn't a cold-start wait."""
        try:
            print(f"[bananagent] ollama {brain.ensure_server()} at {config.OLLAMA_HOST}")
            if not config.WARM_ON_START:
                print("[bananagent] warm-up off; the first question will be slow")
                return
            print(f"[bananagent] loading {brain.resolve_model()} (first time is slow)…")
            brain.warm()
            print("[bananagent] model warm")
        except Exception as e:
            print(f"[bananagent] {e}")

    # -- event loop (main thread) -------------------------------------------
    def _drain(self):
        self.ui.root.after(16, self._drain)
        self.ui.level = self.rec.level if self.rec.running else 0.0

        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                return
            try:
                self._handle(kind, payload)
            except Exception:
                traceback.print_exc()

    def _handle(self, kind, payload):
        # Key events are immediate; everything else comes from a worker thread
        # and is dropped if a newer interaction has already started.
        if kind == "down":
            return self._start_recording()
        if kind == "up":
            return self._finish_recording()
        if kind == "dismiss":
            if self.ui.state != Overlay.LISTENING:
                self._hide()
            return
        if kind == "quit":
            self.rec.close()
            return self.ui.root.quit()

        gen, data = payload
        if gen != self.gen:
            return

        if kind == "token":
            if self.ui.state != Overlay.ANSWERING:
                self.ui.set_state(Overlay.ANSWERING)
            self.ui.append(data)
        elif kind == "done":
            self.ui.set_state(Overlay.ANSWERING)
            self._autohide(6.0 + min(20.0, len(data) / 25.0))
        elif kind == "empty":
            # Not a failure — you just didn't say anything loud enough.
            self.ui.set_error("Didn't catch that — hold the key while you talk.")
            self._autohide(2.5)
        elif kind == "error":
            self.ui.set_error(data)
            self._autohide(8.0)

    # -- interaction --------------------------------------------------------
    def _start_recording(self):
        if self.held:
            return  # key repeat while held, not a new press
        self.held = True
        self.pressed_at = time.time()
        self.gen += 1
        self.speaker.stop()  # asking again cuts the previous answer off
        self._cancel_autohide()
        self.ui.clear()
        self.ui.set_state(Overlay.LISTENING)
        try:
            self.rec.start()
        except Exception as e:
            self.held = False
            self.ui.set_error(str(e))

    def _finish_recording(self):
        if not self.held:
            return
        self.held = False
        audio = self.rec.stop()

        if time.time() - self.pressed_at < config.MIN_HOLD_SECONDS:
            self._hide()  # a tap, not a question
            return

        gen = self.gen
        if recorder.is_silent(audio):
            self.events.put(("empty", (gen, None)))
            return

        self.ui.set_state(Overlay.THINKING)
        threading.Thread(target=self._ask, args=(gen, audio), daemon=True).start()

    def _ask(self, gen, audio):
        """Send the clip and stream the reply. Runs off the main thread."""
        try:
            wav = recorder.to_wav_bytes(audio)
            print(f"\n[you: {audio.size / config.SAMPLE_RATE:.1f}s of audio]")

            started = time.time()
            reply = []
            for chunk in brain.stream(wav):
                if gen != self.gen:
                    return
                reply.append(chunk)
                self.events.put(("token", (gen, chunk)))
                self.speaker.feed(chunk)
                sys.stdout.write(chunk)
                sys.stdout.flush()
            print()
            self.speaker.finish()

            self.last_reply = round(time.time() - started, 2)
            self.reply_times.append(self.last_reply)
            self.turns += 1
            self.events.put(("done", (gen, "".join(reply))))
        except Exception as e:
            traceback.print_exc()
            if gen == self.gen:
                self.events.put(("error", (gen, str(e))))

    # -- overlay housekeeping ------------------------------------------------
    def _autohide(self, seconds):
        self._cancel_autohide()
        self._hide_job = self.ui.root.after(int(seconds * 1000), self._hide)

    def _cancel_autohide(self):
        if self._hide_job is not None:
            try:
                self.ui.root.after_cancel(self._hide_job)
            except Exception:
                pass
            self._hide_job = None

    def _hide(self):
        self._cancel_autohide()
        self.ui.set_state(Overlay.IDLE)


if __name__ == "__main__":
    App().run()
