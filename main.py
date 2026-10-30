"""BananaGent — press ` (tilde), type, press Enter.

The key pops a text box up in the corner beside the orb and hands it the
keyboard. Enter sends what you typed to the model (brain.py: qwen3.8:27b on the
DGX Spark) and the answer streams into the overlay above the box. Esc, or the
key again, puts it away.

The dashboard's Chat tab is the other way in, and Discord a third: text the
bot and it texts back (integrations/discord_bot.py). All go through
App.send(), so they share one conversation — a message typed in the corner
shows up in the chat with its answer, and the model sees it all as one history.

Run it with the venv interpreter:  .venv\\Scripts\\python.exe main.py
Quit with ctrl+shift+q (the window is frameless, so there's no X to click).
"""

import collections
import ctypes
import os
import queue
import subprocess
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


RESTARTED = os.environ.pop("BANANAGENT_RESTARTED_FROM", "")


def _wait_for_predecessor():
    """After a restart, let the copy that launched us finish shutting down
    before this one hooks the hotkey — otherwise for a moment both would."""
    if not RESTARTED.isdigit():
        return
    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(RESTARTED))
    if handle:
        ctypes.windll.kernel32.WaitForSingleObject(handle, 15000)
        ctypes.windll.kernel32.CloseHandle(handle)


_wait_for_predecessor()

_INSTANCE = None  # held for the life of the process


def _only_copy():
    """Make sure this is the only BananaGent running, or quit.

    Two copies both hook the ` key, both answer, and each has its own agents —
    a job started in one reports back (or doesn't) in the other. A named mutex
    is held for as long as a copy runs, and Windows releases it when the
    process ends, however it ends. A restart waits for its predecessor to let
    go of it; anything else finding it taken just leaves.
    """
    global _INSTANCE
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _INSTANCE = kernel32.CreateMutexW(None, True, "Local\\BananaGent-single-copy")
    if kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS: someone else has it
        return
    # WAIT_OBJECT_0, or WAIT_ABANDONED when the holder died without letting go.
    wait = 60000 if RESTARTED else 0
    if kernel32.WaitForSingleObject(ctypes.c_void_p(_INSTANCE), wait) in (0, 0x80):
        return
    print("[bananagent] already running — press ` to use it, or ctrl+shift+q to quit it first")
    sys.exit(0)

import agents
import brain
import config
import endpoints
import memory
import prompts
from dashboard import Dashboard
from integrations import discord_bot
from overlay import Overlay

QUIT_HOTKEY = "ctrl+shift+q"


class DiscordTurn:
    """A reply going out to a Discord chat: where to, the texts it's being
    sent as, and the inbox that takes the user's follow-ups while it's written."""

    def __init__(self, channel_id, reply_to=None):
        self.channel_id = channel_id
        self.sink = discord_bot.Reply(channel_id, reply_to)
        self.inbox = brain.Inbox()


# A reply's "parts" are its text and its tool calls, in the order they
# happened — what the Chat tab draws. These build them up as a reply streams.
def add_text(parts, chunk):
    if parts and parts[-1]["kind"] == "text":
        parts[-1]["text"] += chunk
    else:
        parts.append({"kind": "text", "text": chunk})


def _status_line(parts):
    """The overlay's grey line for a reply's tools. Most finish in a few
    milliseconds, too fast to ever see "running", so what ran stays up for the
    rest of the answer: "ran get_time, system_stats"."""
    tools = [p for p in parts if p["kind"] == "tool"]
    running = [p["name"] for p in tools if p["status"] == "running"]
    if running:
        return f"running {running[-1]}…"
    names = [p["name"] + (" (failed)" if p["status"] == "failed" else "") for p in tools]
    return "ran " + ", ".join(names) if names else None


def add_tool(parts, info):
    """A tool starting, or one already listed finishing (matched by id)."""
    for part in parts:
        if part["kind"] == "tool" and part["id"] == info["id"]:
            part.update(status=info["status"], result=info.get("result", ""))
            if info.get("run"):
                part.update(agent=info["agent"], run=info["run"])
            return
    parts.append({"kind": "tool", **info})


def _asked(message):
    """A message to the model as the chat shows it. An agent's announcement
    isn't something you said, so it's a small labelled line ("Coding Agent
    finished") rather than one of your bubbles — the model's reply to it is
    the part worth reading."""
    if message.startswith(agents.NOTICE):
        label = message[len(agents.NOTICE):].strip().split("\n", 1)[0].rstrip(".")
        return {"role": "notice", "text": label or "An agent reported back"}
    return {"role": "you", "text": message, "source": "memory"}


def _agent_runs():
    return {a.name: a.run for a in agents.all_agents()}


def _tag_run(info, before):
    """If the tool that just finished set an agent off, say which run it
    started — the Chat tab follows that run live under the tool's line."""
    if before is None:
        return
    for name, run in _agent_runs().items():
        if run and run != before.get(name):
            info.update(agent=name, run=run)
            return


class App:
    def __init__(self):
        self.ui = Overlay(on_submit=lambda text: self.send(text, "box"))
        self.events = queue.Queue()

        self.gen = 0  # bumps every interaction; stale workers check it and bail
        self.key_down = False  # the hotkey auto-repeats while held; act once
        self._hide_job = None

        # The conversation, oldest first, shared by the box and the Chat tab.
        # Each entry is {"role": "you" | "banana", "text", ...}; a reply's text
        # and its "parts" (see add_text) grow as it streams. Only ever touched
        # on the main thread. It opens on whatever is still in the short-term
        # window from last time.
        self.chat = []
        for message, reply, parts in memory.turns():
            self.chat += [_asked(message),
                          {"role": "banana", "text": reply, "done": True,
                           "parts": parts or [{"kind": "text", "text": reply}]}]
        # Agents announce when they finish or need you (agents.announce). Each
        # becomes a turn for the model, so it tells you unprompted; they queue
        # here while a reply is still streaming, rather than cutting it off.
        self.notices = collections.deque()
        agents.listen(lambda agent, event, label, message:
                      self.events.put(("notice", (label, message))))
        self.reply_source = None  # "box" or "chat": where the current reply was asked
        self.restarting = False  # set by restart(): relaunch once the loop has ended
        # Discord: the reply going out there now, if any, and where you last
        # wrote from — when it's Discord, agents' news is texted to you there.
        self.discord_turn = None
        self.last_source = None
        discord_bot.listen(lambda msg: self.events.put(("discord", msg)))
        if config.DISCORD_ENABLED:
            threading.Thread(target=self._discord_up, daemon=True).start()

        # What the dashboard reports about this session.
        self.started_at = time.time()
        self.turns = 0
        self.last_message = None
        self.last_reply = None
        self.reply_times = collections.deque(maxlen=40)

        # Starts minimized: the taskbar gets a banana to click, and nothing
        # opens a window in your face at login.
        self.dash = Dashboard(self.ui.root, self)

        endpoints.bind(self)  # lets endpoints act on the running app
        self._hook_keys()
        if config.WARM_ON_START:
            threading.Thread(target=self._warm_up, daemon=True).start()
        self.ui.root.after(16, self._drain)

    def run(self):
        # Assembling it once here restores any part whose file has gone
        # missing, and puts a broken prompt in the log before anyone asks.
        try:
            print(f"[prompts] system prompt is {len(prompts.system_prompt())} chars "
                  f"({', '.join(prompts.PARTS)})")
        except Exception as e:
            print(f"[prompts] couldn't assemble the system prompt: {e}")
        print(f"[bananagent] ready — press  {config.HOTKEY}  to type, {QUIT_HOTKEY} to quit")
        try:
            self.ui.root.mainloop()
        finally:
            # Agents outlive a single question by design, so quitting has to go
            # and shut them down — otherwise the agent's Chrome is left running.
            for agent in agents.all_agents():
                try:
                    (getattr(agent, "close", None) or agent.stop)()
                except Exception:
                    pass
            discord_bot.stop()
            if self.restarting:
                self._relaunch()

    def restart(self):
        """Quit and come straight back, picking up any change to the code.

        The shutdown is the ordinary one — agents closed, Chrome let go of —
        and the fresh copy is started only once it's done. Main thread only.
        """
        print("[bananagent] restarting")
        self.restarting = True
        self.ui.root.quit()

    def _relaunch(self):
        # Always the windowless interpreter, the way login starts it, so the
        # new copy doesn't depend on a console that's about to close. Output
        # goes to bananagent.log (see _ensure_output).
        exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(exe):
            exe = sys.executable
        here = os.path.dirname(os.path.abspath(__file__))
        env = dict(os.environ, BANANAGENT_RESTARTED_FROM=str(os.getpid()))
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([exe, os.path.join(here, "main.py")], cwd=here, env=env,
                         creationflags=flags, close_fds=True)

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
        """Load the model on the Spark now, not on the first message."""
        started = time.time()
        try:
            brain.warm()
            print(f"[brain] {config.MODEL} loaded on {config.OLLAMA_HOST} "
                  f"in {time.time() - started:.1f}s")
        except Exception as e:
            print(f"[brain] couldn't load {config.MODEL} on {config.OLLAMA_HOST}: {e}")

    def _discord_up(self):
        """Sign the Discord bot in, if there's a token for it."""
        if not discord_bot._token():
            print("[discord] no DISCORD_BOT_TOKEN in secrets.env; texting is off")
            return
        try:
            discord_bot.start()
        except Exception as e:
            print(f"[discord] couldn't sign in: {e}")

    # -- event loop (main thread) -------------------------------------------
    def _drain(self):
        self.ui.root.after(16, self._drain)
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
            if not self.key_down:
                self.key_down = True
                self._toggle_box()
            return
        if kind == "up":
            self.key_down = False
            return
        if kind == "dismiss":
            if self.ui.state != Overlay.IDLE:
                self._hide()
            return
        if kind == "quit":
            return self.ui.root.quit()
        if kind == "restart":
            return self.restart()
        if kind == "notice":
            self.notices.append(payload)
            return self._next_notice()
        if kind == "discord":
            return self._from_discord(payload)

        gen, data = payload
        if gen != self.gen:
            return

        # The current reply is always the last entry: only send() appends, and
        # it bumps gen when it does, so older replies never get this far.
        reply = self.chat[-1]
        # The overlay only follows replies asked from the box, and only while
        # it's still showing one — not after Esc, or once it's been reopened
        # to type the next message.
        overlay = (self.reply_source in ("box", "agent")
                   and self.ui.state in (Overlay.THINKING, Overlay.ANSWERING))

        if kind == "token":
            reply["text"] += data
            add_text(reply["parts"], data)
            if overlay:
                if self.ui.state != Overlay.ANSWERING:
                    self.ui.set_state(Overlay.ANSWERING)
                self.ui.append(data)
        elif kind == "tool":
            add_tool(reply["parts"], data)
            if overlay:
                self.ui.set_status(_status_line(reply["parts"]))
        elif kind == "done":
            reply["done"] = True
            if overlay:
                self.ui.set_state(Overlay.ANSWERING)
                self._autohide(6.0 + min(20.0, len(data) / 25.0))
            self._next_notice()
        elif kind == "error":
            reply.update(text=data, done=True, error=True)
            if overlay:
                self.ui.set_status(None)
                self.ui.set_error(data)
                self._autohide(8.0)
            self._next_notice()

    # -- interaction --------------------------------------------------------
    def _toggle_box(self):
        if self.ui.state == Overlay.TYPING:
            print("[key] box closed")
            return self._hide()
        print("[key] box open")
        self._cancel_autohide()
        self.ui.clear()
        self.ui.set_state(Overlay.TYPING)

    def _replying(self):
        return bool(self.chat) and self.chat[-1]["role"] == "banana" \
            and not self.chat[-1].get("done")

    def _next_notice(self):
        """Hand the model the next agent announcement, once it's free."""
        if self.notices and not self._replying():
            label, message = self.notices.popleft()
            print(f"[agents] {label} — prompting the model")
            self.send(message, "agent")

    def _from_discord(self, msg):
        """A text from Discord (a discord_bot.Incoming). One to the chat a reply
        is already going out to joins that reply — the model takes it in
        mid-answer and nothing is cut off. Anything else starts a reply of its own."""
        turn = self.discord_turn
        if (turn is not None and self._replying() and turn.channel_id == msg.channel_id
                and turn.inbox.put(msg.prompt, msg.sent_at, msg.images, msg)):
            print(f"\n[you, via discord, mid-reply] {msg.text}")
            self.last_message, self.last_source = msg.text, "discord"
            self.chat.insert(len(self.chat) - 1,
                             {"role": "you", "text": msg.text, "source": "discord"})
            return
        self.send(msg.prompt, "discord", incoming=msg)

    def send(self, text, source, incoming=None):
        """One message in, from the ` box ("box"), the Chat tab ("chat"),
        Discord ("discord", with the discord_bot.Incoming), or an agent
        announcing it's finished or needs you ("agent").

        Main thread only. Sending cuts off a reply that's still streaming, the
        same as asking again always has — except a Discord text to the chat
        being answered, which joins the reply (_from_discord). Announcements
        wait for it instead (see _next_notice).
        """
        text = text.strip()
        if not text:
            return
        if self._replying():
            # A Discord reply whose inbox is shut has already said all it will.
            over = self.discord_turn is not None and self.discord_turn.inbox.closed
            self.chat[-1].update(done=True, cut=not over)
        if self.discord_turn is not None:
            self.discord_turn.sink.abort()  # nothing, if it had already finished
            self.discord_turn = None

        self.gen += 1
        gen = self.gen
        turn, sent_at = None, time.time()
        if source == "agent":
            if self.last_source == "discord" and discord_bot.running() and discord_bot.home():
                # You were last texting from Discord, so that's where you'll hear it.
                source, turn = "discord", DiscordTurn(discord_bot.home())
            else:
                # The reply pops up in the corner like an answer would — unless
                # you're in the middle of typing there, which it mustn't take over.
                source = "chat" if self.ui.state == Overlay.TYPING else "agent"
            self.chat.append(_asked(text))
        elif source == "discord":
            turn = DiscordTurn(incoming.channel_id,
                               incoming.message_id if incoming.in_server else None)
            sent_at = incoming.sent_at
            self.last_message, self.last_source = incoming.text, source
            self.chat.append({"role": "you", "text": incoming.text, "source": source})
        else:
            self.last_message, self.last_source = text, source
            self.chat.append({"role": "you", "text": text, "source": source})
        self.discord_turn = turn
        self.reply_source = source
        self.chat.append({"role": "banana", "text": "", "parts": [], "done": False})
        if source in ("box", "agent"):
            self._cancel_autohide()
            self.ui.clear()
            self.ui.set_state(Overlay.THINKING)
        images = incoming.images if incoming is not None else None
        threading.Thread(target=self._ask, args=(gen, text, source, sent_at, turn, images),
                         daemon=True).start()

    def _ask(self, gen, text, source, sent_at, turn=None, images=None):
        """Stream the reply to one message. Runs off the main thread.

        A Discord reply (`turn`) is also texted out as it streams, and takes in
        the follow-ups that arrive meanwhile; any that came too late for it are
        handed back to be answered on their own."""
        try:
            print(f"\n[you, via {source}] {text}")
            started = time.time()
            reply, parts = [], []  # this thread's own copy of the parts, for memory
            runs_before = {}  # tool id -> every agent's run as that tool began
            inbox = turn.inbox if turn is not None else None
            for chunk in brain.reply(text, sent_at, images=images, inbox=inbox):
                if gen != self.gen:
                    return
                if isinstance(chunk, brain.Interrupted):
                    # A follow-up cut the text being written; what was already
                    # sent stands, the half-written rest is dropped.
                    turn.sink.cut()
                    chunk = ("—" if reply and reply[-1].endswith(" ") else " —") + "\n\n"
                    reply.append(chunk)
                    add_text(parts, chunk)
                    self.events.put(("token", (gen, chunk)))
                    continue
                if isinstance(chunk, brain.ToolUse):
                    info = chunk.snapshot()
                    if info["status"] == "running":
                        runs_before[info["id"]] = _agent_runs()
                    else:
                        _tag_run(info, runs_before.pop(info["id"], None))
                    add_tool(parts, dict(info))
                    self.events.put(("tool", (gen, info)))
                    continue
                reply.append(chunk)
                add_text(parts, chunk)
                self.events.put(("token", (gen, chunk)))
                if turn is not None:
                    turn.sink.feed(chunk)
                sys.stdout.write(chunk)
                sys.stdout.flush()
            print()
            asked = text
            if turn is not None:
                turn.sink.finish()
                self._late(turn)
                # The follow-ups it took are part of what was asked. An image
                # was only shown with its message, so later turns don't claim it.
                asked = "\n\n".join([text] + [m["text"] for m in turn.inbox.received])
                asked = asked.replace(" — you can see it)", ")")
            # Only a finished reply joins short-term memory — one that failed
            # or was cut off isn't something the model should think it said.
            if reply:
                memory.remember(asked, "".join(reply), parts, sent_at)

            self.last_reply = round(time.time() - started, 2)
            self.reply_times.append(self.last_reply)
            self.turns += 1
            self.events.put(("done", (gen, "".join(reply))))
        except Exception as e:
            traceback.print_exc()
            if gen == self.gen:
                self.events.put(("error", (gen, str(e))))
                if turn is not None:
                    turn.sink.fail(e)
        finally:
            if turn is not None:
                self._late(turn)

    def _late(self, turn):
        """Shut a Discord reply's inbox, and answer anything that arrived too
        late for it as a message of its own."""
        for item in turn.inbox.close():
            self.events.put(("discord", item["payload"]))

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
    _only_copy()
    App().run()
