"""Discord: a bot account the user texts BananaGent through.

The bot runs on a thread of its own with its own asyncio loop, signed in with
the bot token in secrets.env (DISCORD_BOT_TOKEN — see the README). It takes
messages from the people in config.DISCORD_ALLOWED_USERS, or when that's
empty from whoever owns the bot in Discord's developer portal: every DM, and
in a server, a message that mentions it or replies to it (or follows up in a
channel it's just answered in). Anyone else is ignored, so nobody but the user
can reach the tools behind it.

An incoming message is handed to listen()'s callback as an Incoming, with its
attachments already saved under DISCORD_FILES_DIR, images ready for the model
to look at, and small text files read in. main.App takes it from there: the
same conversation as the ` box and the Chat tab, so the model sees one history.

The answer goes back through a Reply. The model's text arrives a chunk at a
time, and every blank line outside a code block ends one text — so a reply
written as three short paragraphs goes out as three messages, each as soon as
it's written, the way a person texts. The typing indicator is on from the
moment the message lands until the last text is out, and only then: Discord
drops it whenever the bot sends something, so it's put straight back after
every text but the last, and it's never re-sent once the reply is over.

Everything else here is for the endpoints (endpoints/texting.py): sending to a
chat, reading one, reacting, saving a message's files. They're called from
worker threads and block on the bot's loop.
"""

import asyncio
import base64
import concurrent.futures
import glob
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field

import config
from . import NotConnected, Platform, register

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = os.path.join(APP, config.DISCORD_FILES_DIR)

LIMIT = 2000  # characters in one Discord message
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}  # what the model can look at
_TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".log", ".html",
             ".css", ".xml", ".yaml", ".yml", ".ini", ".toml", ".sql", ".sh", ".ps1", ".bat"}
# Markup only the Chat tab understands (see prompts/main.md). On Discord a
# button is just its label, and a local path is shown as the path.
_APP_LINK = re.compile(r"\[([^\]\n]+)\]\(((?:send|run|open):[^)\n]*|[A-Za-z]:\\[^)\n]*)\)")
_BREAK = re.compile(r"```|\n[ \t]*\n")

_lock = threading.RLock()
_client = None
_loop = None
_thread = None
_ready = threading.Event()
_error = ""
_allowed = set()   # user ids it takes messages from, settled once it's signed in
_handler = None    # where incoming messages go (listen)
_home = None       # the chat the user last texted from: where "here" sends
_active = {}       # channel id -> the Reply going out there right now
_answered = {}     # channel id -> when it last sent something there
_latest = {}       # channel id -> the user's latest message there, for reacting to
_questions = {}    # message id -> the question asked in it (see ask)


# -- signing in ------------------------------------------------------------------
def _token():
    try:
        with open(os.path.join(APP, config.SECRETS_FILE), encoding="utf-8") as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key.strip() == "DISCORD_BOT_TOKEN":
                    return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def running():
    return _ready.is_set() and _client is not None and not _client.is_closed()


def start():
    """Sign the bot in and wait until it's ready. Returns who it signed in as."""
    global _client, _loop, _thread, _error
    with _lock:
        if _thread is not None and _thread.is_alive():
            return _wait_ready()
        token = _token()
        if not token:
            raise NotConnected(
                "There's no DISCORD_BOT_TOKEN in secrets.env yet. The user has to make "
                "a bot in Discord's developer portal and paste its token there (see the "
                "README), then connect again.")
        import discord

        intents = discord.Intents.default()
        intents.message_content = True  # privileged: switched on in the developer portal
        _client = discord.Client(intents=intents)
        # Set by name: discord.py dispatches to on_<event> attributes, and its
        # @event decorator would register these under their own names instead.
        _client.on_ready = _on_ready
        _client.on_message = _on_message
        _client.on_interaction = _on_interaction
        _loop, _error = asyncio.new_event_loop(), ""
        _ready.clear()
        _thread = threading.Thread(target=_run, args=(_client, _loop, token),
                                   name="discord", daemon=True)
        _thread.start()
        return _wait_ready()


def _run(client, loop, token):
    global _error
    import discord

    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(client.start(token))
    except discord.LoginFailure:
        _error = ("Discord refused the bot token in secrets.env — it's wrong or was "
                  "reset. Copy a fresh one from the developer portal.")
    except discord.PrivilegedIntentsRequired:
        _error = ("Discord won't let the bot read messages yet: switch on \"Message "
                  "Content Intent\" on the bot's page in the developer portal.")
    except Exception as e:
        _error = f"{type(e).__name__}: {e}"
    finally:
        _ready.clear()
        if _error:
            print(f"[discord] stopped: {_error}")
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass


def _wait_ready():
    deadline = time.time() + config.DISCORD_CONNECT_SECONDS
    while time.time() < deadline:
        if _ready.wait(0.25):
            return str(_client.user)
        if _thread is None or not _thread.is_alive():
            break
    raise RuntimeError(_error or "The Discord bot didn't finish signing in. Try again.")


def stop():
    """Sign the bot out. Safe to call when it isn't running."""
    global _thread
    with _lock:
        client, loop, thread = _client, _loop, _thread
        if client is None or thread is None or not thread.is_alive():
            return False
        for reply in list(_active.values()):
            reply.abort()
        try:
            asyncio.run_coroutine_threadsafe(client.close(), loop).result(10)
        except Exception:
            pass
        thread.join(10)
        _thread = None
        _ready.clear()
        return True


async def _on_ready():
    global _allowed
    allowed = {int(u) for u in config.DISCORD_ALLOWED_USERS}
    if not allowed:
        info = await _client.application_info()
        allowed = ({m.id for m in info.team.members} if info.team else {info.owner.id})
    _allowed = allowed
    names = []
    for uid in allowed:
        user = _client.get_user(uid)
        names.append(str(user) if user else str(uid))
    print(f"[discord] signed in as {_client.user}; taking messages from {', '.join(names)}")
    _ready.set()


def listen(fn):
    """fn(Incoming) is called, on the bot's thread, for every message to answer."""
    global _handler
    _handler = fn


def set_home(channel_id):
    """Where a send to "here" goes: the chat the user is texting from."""
    global _home
    _home = channel_id


def home():
    return _home


# -- messages in -----------------------------------------------------------------
@dataclass
class Incoming:
    """One message from the user, as the app takes it."""
    channel_id: int
    message_id: int
    where: str          # "DM", or "#general in My Server"
    text: str           # what they typed, as the Chat tab shows it
    prompt: str         # what the model reads: where it's from, the text, the files
    sent_at: float
    in_server: bool
    files: list = field(default_factory=list)   # where the attachments were saved
    images: list = field(default_factory=list)  # base64, for the model to see
    # When this answers one of the bot's messages — a question's button or
    # form, or a Discord reply to it: that message's id, and the answer alone.
    # It's how an answer to a question Autopilot asked finds its way back.
    answer_to: int = None
    answer: str = None


async def _on_message(message):
    if message.author.bot or message.author.id not in _allowed or _handler is None:
        return
    in_server = message.guild is not None
    if in_server and not _for_us(message):
        # Logged, so "it didn't answer" can be told apart from "it never heard".
        print(f"[discord] not for me (no mention or reply), so not answered: "
              f"#{message.channel.name} in {message.guild.name}: "
              f"{_short(message.clean_content, 60)!r}")
        return
    set_home(message.channel.id)
    _latest[message.channel.id] = message.id
    try:
        incoming = await _receive(message)
    except Exception as e:
        print(f"[discord] couldn't read a message: {type(e).__name__}: {e}")
        return
    print(f"[discord] {incoming.where}: {incoming.text[:80]!r}"
          + (f" (+{len(incoming.files)} file(s))" if incoming.files else ""))
    _handler(incoming)


def _for_us(message):
    """In a server, only what's said to the bot: a mention (of it, or of its
    own role, which Discord's autocomplete often offers first), a reply to one
    of its messages, a follow-up in a channel it's answering or just answered,
    or anything in one of config.DISCORD_OPEN_CHANNELS."""
    if not config.DISCORD_ANSWER_IN_SERVERS:
        return False
    if message.channel.id in {int(c) for c in config.DISCORD_OPEN_CHANNELS}:
        return True
    if _client.user in message.mentions or _own_roles(message) & set(message.role_mentions):
        return True
    ref = message.reference
    if ref is not None and getattr(ref.resolved, "author", None) == _client.user:
        return True
    since = time.time() - _answered.get(message.channel.id, 0)
    return message.channel.id in _active or since < config.DISCORD_FOLLOW_UP_SECONDS


def _own_roles(message):
    """The bot's roles in that server, @everyone aside."""
    return {r for r in message.guild.me.roles if not r.is_default()}


async def _receive(message):
    in_server = message.guild is not None
    text = message.clean_content
    if in_server:
        names = {_client.user.name, message.guild.me.display_name,
                 *(r.name for r in _own_roles(message))}
        for name in names:
            text = text.replace(f"@{name}", "")
        where = f"#{message.channel.name} in {message.guild.name}"
    else:
        where = "DM"
    text = text.strip()

    lines = [f"(via Discord {where}" + (f", from {message.author.display_name})"
                                       if in_server else ")")]
    quoted = getattr(message.reference, "resolved", None) if message.reference else None
    if quoted is not None and getattr(quoted, "content", None) is not None:
        who = "you" if quoted.author == _client.user else quoted.author.display_name
        lines.append(f"(replying to {who}: \"{_short(quoted.clean_content, 300)}\")")
    if text:
        lines.append(text)

    files, images = [], []
    for att in message.attachments:
        note, path, image = await _save(att, message.id, want_image=len(images) < 4)
        lines.append(note)
        if path:
            files.append(path)
        if image:
            images.append(image)
    shown = text or ("(sent " + ", ".join(os.path.basename(p) for p in files) + ")"
                     if files else "(empty message)")
    answer_to = None
    if quoted is not None and getattr(quoted, "author", None) == _client.user:
        answer_to = quoted.id
    return Incoming(channel_id=message.channel.id, message_id=message.id, where=where,
                    text=shown, prompt="\n".join(lines),
                    sent_at=message.created_at.timestamp(), in_server=in_server,
                    files=files, images=images, answer_to=answer_to,
                    answer=text if answer_to else None)


async def _save(att, message_id, want_image=True, folder=None):
    """Save one attachment. Returns (a line telling the model about it, the
    path or None, the image as base64 if the model should see it). With
    `folder`, it's saved there under its own name (numbered if one's taken)
    rather than in the day's folder under DISCORD_FILES_DIR."""
    size = f"{att.size / 1024:.0f} KB" if att.size < 1024 * 1024 else \
        f"{att.size / 1024 / 1024:.1f} MB"
    if att.size > config.DISCORD_DOWNLOAD_MAX_MB * 1024 * 1024:
        return f"(attached {att.filename}, {size} — too big to download, not saved)", None, None
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", att.filename) or "file"
    if folder:
        os.makedirs(folder, exist_ok=True)
        path = _free_name(os.path.join(folder, safe))
    else:
        folder = os.path.join(FILES, time.strftime("%Y-%m-%d"))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{message_id}-{safe}")
    await att.save(path)

    kind = (att.content_type or "").split(";")[0].strip().lower()
    if want_image and kind in _IMAGE_TYPES and config.DISCORD_SHOW_IMAGES:
        with open(path, "rb") as f:
            image = base64.b64encode(f.read()).decode("ascii")
        return f"(attached image {path}, {size} — you can see it)", path, image
    if os.path.splitext(safe)[1].lower() in _TEXT_EXT or kind.startswith("text/"):
        try:
            with open(path, encoding="utf-8") as f:
                body = f.read(config.DISCORD_INLINE_TEXT_CHARS + 1)
        except (UnicodeDecodeError, OSError):
            body = None
        if body is not None:
            cut = " (only the start of it)" if len(body) > config.DISCORD_INLINE_TEXT_CHARS else ""
            return (f"(attached text file {path}, {size}{cut}:\n```\n"
                    f"{body[:config.DISCORD_INLINE_TEXT_CHARS]}\n```)"), path, None
    return f"(attached file {path}, {size} — saved on this computer)", path, None


# -- messages out ------------------------------------------------------------------
class Reply:
    """One answer, texted back as it's written.

    feed() takes the model's text a chunk at a time and sends each finished
    text as soon as there's a blank line after it. finish() sends the rest and
    turns the typing indicator off; cut() drops a text that was only half
    written when the user sent something new; abort() gives up on the whole
    reply. Every method is safe to call from any thread, and once the bot
    isn't running they do nothing.
    """

    def __init__(self, channel_id, reply_to=None):
        self.channel_id = channel_id
        self.reply_to = reply_to  # in a server, the first text answers this message
        self.sent = 0
        self._buffer = ""
        self._mutex = threading.Lock()
        self._over = False
        self._loop = _loop if running() else None
        if self._loop is None:
            return
        self._queue = asyncio.Queue()
        self._poke = asyncio.Event()
        self._turn = asyncio.Lock()  # typing and sending take turns, never overlap
        self._ending = False
        _active[channel_id] = self
        asyncio.run_coroutine_threadsafe(self._work(), self._loop)

    # called from the app's threads
    def feed(self, chunk):
        with self._mutex:
            if self._over:
                return
            self._buffer += chunk
            pieces, self._buffer = _split(self._buffer)
        for piece in pieces:
            self._text(piece)

    def cut(self):
        with self._mutex:
            self._buffer = ""

    def finish(self):
        with self._mutex:
            if self._over:
                return
            self._over = True
            rest, self._buffer = self._buffer, ""
        self._text(rest, last=True)

    def fail(self, error):
        with self._mutex:
            if self._over:
                return
            self._over = True
            self._buffer = ""
        self._text(f"Something went wrong on my end: {_short(str(error), 300)}", last=True)

    def abort(self):
        with self._mutex:
            if self._over:
                return
            self._over = True
            self._buffer = ""
        self._push(None)

    def send(self, content=None, path=None, timeout=None, view=None, paths=None):
        """Send now, in order with the reply's own texts. Blocks; returns the
        message id. For the endpoints, while this chat has a reply going out.
        `view` makes the message's buttons (see _send)."""
        future = concurrent.futures.Future()
        self._push(("send", {"content": content, "path": path, "view": view,
                             "paths": paths}, future, False))
        return future.result(timeout or config.DISCORD_SEND_SECONDS)

    # the bot's side
    def _text(self, text, last=False):
        messages = _messages(text)
        if not messages:
            if last:
                self._push(None)
            return
        for i, piece in enumerate(messages):
            self._push(("send", {"content": piece}, None, last and i == len(messages) - 1))
        if last:
            self._push(None)

    def _push(self, item):
        if self._loop is None:
            if item and item[2] is not None:
                item[2].set_exception(NotConnected("The Discord bot isn't running."))
            return
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:  # the loop is gone: the bot was stopped
            if item and item[2] is not None:
                item[2].set_exception(NotConnected("The Discord bot isn't running."))

    async def _work(self):
        typing = None
        try:
            channel = await _channel(self.channel_id)
            typing = asyncio.ensure_future(self._typing(channel))
            while True:
                item = await self._queue.get()
                if item is None:
                    break
                _, kwargs, future, last = item
                if last:
                    self._ending = True
                async with self._turn:
                    try:
                        ref = None
                        if self.reply_to and not self.sent:
                            ref = _reference(channel, self.reply_to)
                        msg = await _send(channel, reference=ref, **kwargs)
                        self.sent += 1
                        if future is not None:
                            future.set_result(msg.id)
                    except Exception as e:
                        print(f"[discord] couldn't send: {type(e).__name__}: {e}")
                        if future is not None:
                            future.set_exception(e)
                if not self._ending:
                    self._poke.set()  # sending cleared the indicator; put it back
        except Exception as e:
            print(f"[discord] reply failed: {type(e).__name__}: {e}")
        finally:
            self._ending = True
            if typing is not None:
                typing.cancel()
            if _active.get(self.channel_id) is self:
                del _active[self.channel_id]
            while not self._queue.empty():  # anyone still waiting on a send
                item = self._queue.get_nowait()
                if item and item[2] is not None and not item[2].done():
                    item[2].set_exception(RuntimeError("The reply ended before it was sent."))

    async def _typing(self, channel):
        # One trigger lasts about ten seconds, or until the bot next sends.
        while True:
            async with self._turn:
                if self._ending:
                    return
                try:
                    await channel.typing()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(self._poke.wait(), 8)
            except asyncio.TimeoutError:
                pass
            self._poke.clear()


def _split(buffer):
    """The finished texts at the front of buffer, and what's still being
    written. A text ends at a blank line, except inside a code block."""
    pieces, start, fenced = [], 0, False
    for m in _BREAK.finditer(buffer):
        if m.group() == "```":
            fenced = not fenced
        elif not fenced:
            pieces.append(buffer[start:m.start()])
            start = m.end()
    return pieces, buffer[start:]


def _messages(text):
    """One finished text as Discord messages: app-only markup turned into
    plain text, and anything over Discord's limit split at a line or a space."""
    text = _APP_LINK.sub(lambda m: m.group(1) if m.group(2).startswith(("send:", "run:"))
                         else f"{m.group(1)} (`{m.group(2).removeprefix('open:')}`)", text)
    text = text.strip()
    out = []
    while len(text) > LIMIT:
        at = text.rfind("\n", 0, LIMIT)
        if at < LIMIT // 2:
            at = text.rfind(" ", 0, LIMIT)
        if at < LIMIT // 2:
            at = LIMIT
        out.append(text[:at].rstrip())
        text = text[at:].strip()
    if text:
        out.append(text)
    return out


async def _send(channel, content=None, path=None, reference=None, view=None, paths=None):
    """`view`, if given, is a function that makes the message's discord.ui.View:
    a View has to be made on the bot's own loop, so it's built here. `paths`
    attaches several files (up to 10) to the one message."""
    import discord

    if not content and not path and not paths:
        raise ValueError("Nothing to send: give some text or a file.")
    kwargs = {"allowed_mentions": discord.AllowedMentions(everyone=False, roles=False,
                                                         users=True, replied_user=False)}
    if reference is not None:
        kwargs["reference"] = reference
    if paths:
        kwargs["files"] = [discord.File(p) for p in paths]
    elif path:
        kwargs["file"] = discord.File(path)
    if view is not None:
        kwargs["view"] = view()
    msg = await channel.send(content or None, **kwargs)
    _answered[channel.id] = time.time()
    return msg


def _reference(channel, message_id):
    import discord

    return discord.MessageReference(message_id=message_id, channel_id=channel.id,
                                    fail_if_not_exists=False)


# -- questions (endpoints/questions.py) ------------------------------------------------
# A question is a message with buttons. Choice: one per option, plus
# "Something else…" for an answer of their own. Short and long: an Answer
# button that opens Discord's pop-up form with a one-line or a paragraph box.
# The button ids say what they are ("bq|c|2" is the third choice), and clicks
# are handled in _on_interaction rather than by the View's own callbacks, so
# a question asked before a restart still answers after one.
_HINT = "-# tap to answer, or just text me"


def _question_text(question, kind, choices):
    text = f"**{_short(question, 1500)}**"
    if kind == "choice" and any(len(c) > 80 for c in choices):
        # Too long for a button label: list them, and number the buttons.
        text += "\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(choices, 1))
    return text


def _question_view(kind, choices):
    import discord

    view = discord.ui.View(timeout=None)
    if kind == "choice":
        numbered = any(len(c) > 80 for c in choices)
        for i, c in enumerate(choices):
            view.add_item(discord.ui.Button(
                label=str(i + 1) if numbered else c, custom_id=f"bq|c|{i}",
                style=discord.ButtonStyle.primary))
        view.add_item(discord.ui.Button(label="Something else…", custom_id="bq|o",
                                        style=discord.ButtonStyle.secondary))
    else:
        view.add_item(discord.ui.Button(
            label="Answer" if kind == "short" else "Write my answer", emoji="✏️",
            custom_id="bq|s" if kind == "short" else "bq|l",
            style=discord.ButtonStyle.primary))
    return view


def ask(channel_id, question, kind, choices=()):
    """Post a question to a chat, in order with the reply going out there if
    there is one. Returns the message id."""
    content = f"{_question_text(question, kind, choices)}\n{_HINT}"
    make = lambda: _question_view(kind, list(choices))  # noqa: E731
    reply = _active.get(channel_id)
    if reply is not None and not reply._over:
        mid = reply.send(content, view=make)
    else:
        async def go():
            return (await _send(await _channel(channel_id), content=content, view=make)).id
        mid = _call(go())
    _questions[mid] = {"question": question, "kind": kind, "choices": list(choices)}
    return mid


def _asked_in(message):
    """The question a message asked: from memory, or read back off the message
    itself when the app has restarted since it went out."""
    q = _questions.get(message.id)
    if q is not None:
        return q
    lines = [l for l in (message.content or "").split("\n") if not l.startswith("-# ")]
    question = lines[0].strip().strip("*") if lines else "(your question)"
    listed = [re.sub(r"^\d+\.\s*", "", l) for l in lines[1:]]
    choices = []
    for row in message.components:
        for item in getattr(row, "children", []):
            cid = getattr(item, "custom_id", "") or ""
            if cid.startswith("bq|c|"):
                i = int(cid.rsplit("|", 1)[1])
                choices.append(listed[i] if i < len(listed) else item.label)
    kind = "choice" if choices else "short"
    return {"question": question, "kind": kind, "choices": choices}


def _answer_form(title, label, long, placeholder, on_submit):
    import discord

    class Form(discord.ui.Modal):
        box = discord.ui.TextInput(
            label=label, placeholder=placeholder or None, required=True,
            style=discord.TextStyle.paragraph if long else discord.TextStyle.short,
            max_length=4000 if long else 300)

        async def on_submit(self, interaction):
            await on_submit(interaction, self.box.value)

        async def on_error(self, interaction, error):
            print(f"[discord] answer form failed: {type(error).__name__}: {error}")

    return Form(title=title)


async def _on_interaction(interaction):
    import discord

    if interaction.type != discord.InteractionType.component:
        return  # the answer forms handle their own submits (Form.on_submit)
    cid = (interaction.data or {}).get("custom_id", "")
    if not cid.startswith("bq|") or interaction.message is None:
        return
    if interaction.user.id not in _allowed:
        await interaction.response.send_message(
            "That question was for someone else.", ephemeral=True)
        return
    q = _asked_in(interaction.message)
    if cid.startswith("bq|c|"):
        i = int(cid.rsplit("|", 1)[1])
        if i < len(q["choices"]):
            await _answered_with(interaction, q, q["choices"][i])
        return
    long = cid == "bq|l"
    question = q["question"]
    # A form's box label holds 45 characters, so a longer question goes in
    # its placeholder (100) instead; it's on the message right above anyway.
    fits = len(question) <= 45
    form = _answer_form(
        title="Your answer" if cid != "bq|o" else "Something else",
        label=question if fits else "Your answer",
        long=long, placeholder=None if fits else _short(question, 100),
        on_submit=lambda inter, value: _answered_with(inter, q, value))
    await interaction.response.send_modal(form)


async def _answered_with(interaction, q, answer):
    """Show the answer on the question (and take its buttons away), then hand
    it to the app as the user's next message."""
    from endpoints.questions import answer_message

    answer = str(answer).strip()
    if not answer:
        return
    message = interaction.message
    body = "\n".join(l for l in (message.content or "").split("\n") if not l.startswith("-# "))
    try:
        await interaction.response.edit_message(
            content=f"{body}\n✅ {_short(answer, 300)}"[:LIMIT], view=None)
    except Exception as e:
        print(f"[discord] couldn't mark the question answered: {type(e).__name__}: {e}")
        if not interaction.response.is_done():
            await interaction.response.defer()
    _questions.pop(message.id, None)
    if _handler is None:
        return
    channel = interaction.channel
    in_server = interaction.guild is not None
    where = f"#{channel.name} in {interaction.guild.name}" if in_server else "DM"
    text = answer_message(q["question"], answer)
    head = f"(via Discord {where}" + (f", from {interaction.user.display_name})"
                                      if in_server else ")")
    set_home(channel.id)
    print(f"[discord] {where}: answered {q['question'][:50]!r} with {answer[:60]!r}")
    _handler(Incoming(channel_id=channel.id, message_id=message.id, where=where,
                      text=text, prompt=f"{head}\n{text}", sent_at=time.time(),
                      in_server=in_server, answer_to=message.id, answer=answer))


# -- for the endpoints ---------------------------------------------------------------
def _call(coro, timeout=None):
    """Run a coroutine on the bot's loop from any other thread, and wait."""
    if not running():
        coro.close()
        raise NotConnected(_error or "Discord isn't connected. Call integration_connect "
                                     "with platform 'discord' (needs DISCORD_BOT_TOKEN "
                                     "in secrets.env).")
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout or config.DISCORD_SEND_SECONDS)


async def _channel(channel_id):
    channel = _client.get_channel(channel_id)
    if channel is None:
        channel = await _client.fetch_channel(channel_id)
    return channel


async def _resolve(to):
    """A chat from what the model said: nothing or "here" (where the user is
    texting from), "me" (a DM with the user), a channel or user id, "#name"
    for a server channel, or someone's name for a DM with them."""
    import discord

    to = str(to or "").strip()
    if to.lower() in ("", "here", "this chat", "current"):
        if _home is None:
            raise ValueError("The user hasn't texted you on Discord yet, so there's no "
                             "\"here\". Say which chat with 'to'.")
        return await _channel(_home)
    if to.lower() in ("me", "user", "owner"):
        return await _dm(min(_allowed))
    if to.isdigit():
        n = int(to)
        try:
            return await _channel(n)
        except (discord.NotFound, discord.Forbidden, discord.InvalidData):
            return await _dm(n)
    name = to.lstrip("#@").lower()
    if to.startswith("#") or not to.startswith("@"):
        for guild in _client.guilds:
            for ch in guild.text_channels:
                if ch.name.lower() == name:
                    return ch
    for user in _known_users():
        if name in {(user.name or "").lower(), (user.global_name or "").lower(),
                    getattr(user, "display_name", "").lower()}:
            return await _dm(user.id)
    raise ValueError(f"No Discord chat or person called '{to}' that you can see. "
                     "Check discord_chats for the ids.")


def _known_users():
    seen = {}
    for ch in _client.private_channels:
        for user in getattr(ch, "recipients", None) or [getattr(ch, "recipient", None)]:
            if user is not None:
                seen[user.id] = user
    for guild in _client.guilds:
        for member in guild.members:
            seen.setdefault(member.id, member)
    for uid in _allowed:
        user = _client.get_user(uid)
        if user is not None:
            seen.setdefault(uid, user)
    return [u for u in seen.values() if u.id != _client.user.id]


async def _dm(user_id):
    user = _client.get_user(user_id) or await _client.fetch_user(user_id)
    return user.dm_channel or await user.create_dm()


def _describe(channel):
    if getattr(channel, "guild", None) is not None:
        return f"#{channel.name} in {channel.guild.name}"
    who = getattr(channel, "recipient", None)
    return f"DM with {who.display_name if who else 'someone'}"


def channel_for(to=None):
    """A chat's id from the same names send() takes; "home" is where the
    user last texted from, or a DM with them if they haven't yet."""
    to = str(to or "").strip()
    if to.lower() == "home":
        to = "" if _home is not None else "me"

    async def go():
        return (await _resolve(to)).id

    return _call(go())


def send(to=None, text=None, path=None):
    """Send text and/or a file. Goes through the chat's reply in progress if
    there is one, so it lands in order after what's already been texted."""
    if path:
        path = os.path.abspath(os.path.expanduser(str(path)))
        if not os.path.isfile(path):
            raise FileNotFoundError(f"There's no file at {path}.")
        mb = os.path.getsize(path) / 1024 / 1024
        if mb > config.DISCORD_UPLOAD_MAX_MB:
            raise ValueError(f"That file is {mb:.1f} MB; Discord only takes "
                             f"{config.DISCORD_UPLOAD_MAX_MB} MB from a bot.")
    if text:
        text = _APP_LINK.sub(lambda m: m.group(1), str(text)).strip()
        if len(text) > LIMIT:
            raise ValueError(f"That's {len(text)} characters; one Discord message holds "
                             f"{LIMIT}. Split it into a few.")

    async def go():
        channel = await _resolve(to)
        return channel, _active.get(channel.id)

    channel, reply = _call(go())
    if reply is not None and not reply._over:
        reply.send(text, path)
    else:
        _call(_send(channel, content=text, path=path))
    what = " and ".join(x for x in (text and "the message",
                                    path and os.path.basename(path)) if x)
    return f"Sent {what} to {_describe(channel)}."


def chats():
    async def go():
        dms = []
        for ch in _client.private_channels:
            who = getattr(ch, "recipient", None)
            if who is not None:
                dms.append({"id": str(ch.id), "with": who.display_name,
                            "username": who.name})
        servers = [{"server": g.name,
                    "channels": [{"id": str(c.id), "name": f"#{c.name}"}
                                 for c in g.text_channels
                                 if c.permissions_for(g.me).send_messages][:40]}
                   for g in _client.guilds]
        return {"signed_in_as": str(_client.user),
                "takes_messages_from": [str(_client.get_user(u) or u) for u in _allowed],
                "texting_from_now": _describe(await _channel(_home)) if _home else None,
                "dms": dms, "servers": servers}
    return _call(go())


def read(chat=None, limit=20):
    limit = max(1, min(int(limit or 20), 100))

    async def go():
        channel = await _resolve(chat)
        out = []
        async for m in channel.history(limit=limit):
            entry = {"id": str(m.id), "from": "you" if m.author == _client.user
                     else m.author.display_name,
                     "at": m.created_at.astimezone().strftime("%a %b %d %I:%M %p"),
                     "text": _short(m.clean_content, 600)}
            if m.attachments:
                entry["files"] = [a.filename for a in m.attachments]
            if m.reference and m.reference.message_id:
                entry["replying_to"] = str(m.reference.message_id)
            out.append(entry)
        return {"chat": _describe(channel), "messages": out[::-1]}
    return _call(go())


def react(message_id=None, emoji="👍", chat=None):
    async def go():
        channel = await _resolve(chat)
        mid = message_id or _latest.get(channel.id)
        if not mid:
            raise ValueError("Which message? Give its id from discord_read.")
        msg = await channel.fetch_message(int(mid))
        await msg.add_reaction(str(emoji).strip())
        return f"Reacted {emoji} to that message in {_describe(channel)}."
    return _call(go())


def save_files(message_id, chat=None, save_to=None):
    folder = None
    if save_to:
        folder = os.path.abspath(os.path.expandvars(os.path.expanduser(str(save_to))))
        if os.path.isfile(folder):
            raise ValueError(f"{folder} is a file; save_to takes a folder.")

    async def go():
        channel = await _resolve(chat)
        msg = await channel.fetch_message(int(message_id))
        if not msg.attachments:
            raise ValueError("That message has no files.")
        return [(await _save(a, msg.id, want_image=False, folder=folder))[0]
                for a in msg.attachments]
    return _call(go(), timeout=config.DISCORD_TRANSFER_SECONDS)


# -- file transfers (discord_send_files) -----------------------------------------------
# Many files, folders and files too big for one upload. Folders go as a zip;
# a file over the limit is zipped, and if that's still too big, cut into
# numbered parts (name.zip.001, .002 …) that 7-Zip opens as one, each on a
# message of its own. Everything else is packed up to ten to a message.
_MAX_ATTACHMENTS = 10           # files on one Discord message
_HEADROOM = 256 * 1024          # bytes kept under the limit for the upload's own overhead
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache"}
# Already compressed: zipping these again only costs time.
_PACKED_EXT = {".zip", ".7z", ".rar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".jpg",
               ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".mkv", ".webm",
               ".mp3", ".m4a", ".ogg", ".flac", ".pdf", ".docx", ".xlsx", ".pptx"}


def _free_name(path):
    """path, or "name (2).ext" and so on when that's taken."""
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        n += 1
        path = f"{base} ({n}){ext}"
    return path


def _expand(paths):
    """The files and folders the model named: a list, or one string with a
    path per line. A path that doesn't exist as written is tried as a
    wildcard (*.png, **/*.py)."""
    if isinstance(paths, str):
        paths = re.split(r"[\r\n]+", paths)
    found, missing = [], []
    for p in paths or []:
        p = str(p).strip().strip('"').strip("'")
        if not p:
            continue
        p = os.path.abspath(os.path.expandvars(os.path.expanduser(p)))
        if os.path.exists(p):
            found.append(p)
        elif any(c in p for c in "*?["):
            hits = sorted(glob.glob(p, recursive=True))
            if hits:
                found.extend(hits)
            else:
                missing.append(p)
        else:
            missing.append(p)
    if missing:
        raise FileNotFoundError("There's nothing at " + ", ".join(missing) + ".")
    seen, out = set(), []
    for p in found:
        key = os.path.normcase(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    if not out:
        raise ValueError("Give at least one file or folder to send.")
    return out


def _zip(sources, dest):
    """Zip files and folders into dest; a folder keeps its own name inside.
    Returns how many folders were left out (_SKIP_DIRS)."""
    skipped, used = 0, set()

    def add(z, full, arc):
        arc = arc.replace("\\", "/")
        base, ext = os.path.splitext(arc)
        n = 1
        while arc.lower() in used:  # two files with one name: keep both
            n += 1
            arc = f"{base} ({n}){ext}"
        used.add(arc.lower())
        z.write(full, arc)

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for src in sources:
            if not os.path.isdir(src):
                add(z, src, os.path.basename(src))
                continue
            root = os.path.dirname(src.rstrip("\\/"))
            for dirpath, dirs, files in os.walk(src):
                keep = [d for d in dirs if d not in _SKIP_DIRS]
                skipped += len(dirs) - len(keep)
                dirs[:] = keep
                for f in files:
                    full = os.path.join(dirpath, f)
                    add(z, full, os.path.relpath(full, root))
    return skipped


def _cut(path, part_bytes, out_dir):
    """path in numbered pieces of part_bytes: name.001, name.002 …"""
    parts = []
    with open(path, "rb") as f:
        while True:
            chunk = f.read(part_bytes)
            if not chunk:
                break
            part = os.path.join(out_dir, f"{os.path.basename(path)}.{len(parts) + 1:03d}")
            with open(part, "wb") as out:
                out.write(chunk)
            parts.append(part)
    return parts


def _size(n):
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def send_files(to=None, paths=None, text=None, zip_all=False, zip_name=None):
    """Send files and folders, however many and however big (within
    DISCORD_TRANSFER_MAX_PARTS). Blocks until the last one is up."""
    items = _expand(paths)
    if text:
        text = _APP_LINK.sub(lambda m: m.group(1), str(text)).strip()
        if len(text) > LIMIT:
            raise ValueError(f"That's {len(text)} characters; one Discord message holds "
                             f"{LIMIT}. Split it into a few.")
    limit = config.DISCORD_UPLOAD_MAX_MB * 1024 * 1024 - _HEADROOM

    async def find():
        return await _resolve(to)

    channel = _call(find())   # before any packing: a wrong `to` fails fast
    outgoing = os.path.join(FILES, "outgoing")
    os.makedirs(outgoing, exist_ok=True)
    work = tempfile.mkdtemp(prefix="send-", dir=outgoing)
    notes, skipped = [], 0
    try:
        # 1. Everything as files: folders (or all of it, with zip_all) zipped.
        files = []
        if zip_all:
            name = str(zip_name or "").strip() or (
                os.path.basename(items[0].rstrip("\\/")) if len(items) == 1 else "files")
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
            if not name.lower().endswith(".zip"):
                name += ".zip"
            dest = os.path.join(work, name)
            skipped += _zip(items, dest)
            files.append(dest)
            notes.append(f"zipped {len(items)} item(s) into {name}")
        else:
            for item in items:
                if os.path.isdir(item):
                    folder = os.path.basename(item.rstrip("\\/"))
                    dest = _free_name(os.path.join(work, folder + ".zip"))
                    skipped += _zip([item], dest)
                    files.append(dest)
                    notes.append(f"zipped the folder {folder}")
                else:
                    files.append(item)

        # 2. Anything still too big: zipped if that helps, else cut into parts.
        singles, split = [], []   # split: (original name, [parts])
        for f in files:
            size = os.path.getsize(f)
            if size <= limit:
                singles.append(f)
                continue
            if os.path.splitext(f)[1].lower() not in _PACKED_EXT:
                dest = _free_name(os.path.join(work, os.path.basename(f) + ".zip"))
                _zip([f], dest)
                if os.path.getsize(dest) <= limit:
                    notes.append(f"zipped {os.path.basename(f)} ({_size(size)}) down to "
                                 f"{_size(os.path.getsize(dest))}")
                    singles.append(dest)
                    continue
                f, size = dest, os.path.getsize(dest)
            count = -(-size // limit)
            already = sum(len(p) for _, p in split)
            if already + count > config.DISCORD_TRANSFER_MAX_PARTS:
                raise ValueError(
                    f"{os.path.basename(f)} is {_size(size)}: that's {count} uploads of "
                    f"{config.DISCORD_UPLOAD_MAX_MB} MB, over the "
                    f"{config.DISCORD_TRANSFER_MAX_PARTS} one transfer is allowed "
                    "(DISCORD_TRANSFER_MAX_PARTS in config.py). Share it another way, "
                    "like Google Drive.")
            split.append((os.path.basename(f), _cut(f, limit, work)))

        # 3. Into messages: singles up to ten and the limit per message, then
        # each part on its own, with how to put them back together.
        messages, batch, total = [], [], 0
        for f in singles:
            size = os.path.getsize(f)
            if batch and (len(batch) >= _MAX_ATTACHMENTS or total + size > limit):
                messages.append((None, batch))
                batch, total = [], 0
            batch.append(f)
            total += size
        if batch:
            messages.append((None, batch))
        for name, parts in split:
            for i, part in enumerate(parts, 1):
                caption = f"-# {name} — part {i} of {len(parts)}"
                if i == 1:
                    caption = (f"**{name}** comes in {len(parts)} parts. Save them all to "
                               f"one folder, then open the .001 with 7-Zip — or on Mac/"
                               f"Linux: `cat \"{name}\".0* > \"{name}\"`\n{caption}")
                messages.append((caption, [part]))
            notes.append(f"split {name} into {len(parts)} parts")
        if text:
            first = messages[0][0] if messages else None
            messages[0] = ((text + ("\n" + first if first else "")), messages[0][1])

        # 4. Out, in order with the reply going to that chat if there is one.
        for content, batch in messages:
            reply = _active.get(channel.id)
            if reply is not None and not reply._over:
                reply.send(content, paths=batch, timeout=config.DISCORD_TRANSFER_SECONDS)
            else:
                _call(_send(channel, content=content, paths=batch),
                      timeout=config.DISCORD_TRANSFER_SECONDS)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    sent = len(singles) + sum(len(p) for _, p in split)
    if skipped:
        notes.append(f"left {skipped} folder(s) like .git and node_modules out of the zip")
    out = (f"Sent {sent} file(s) in {len(messages)} message(s) to {_describe(channel)}.")
    if notes:
        said = "; ".join(notes)
        out += " " + said[0].upper() + said[1:] + "."
    return out


def _short(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


# -- as a platform -------------------------------------------------------------------
def _connect():
    return f"Discord is connected as {start()}. Text it a DM and it answers."


def _disconnect():
    return "Discord bot signed out." if stop() else "Discord wasn't connected."


def _status():
    if running():
        return {"connected": True, "account": str(_client.user)}
    if not _token():
        return {"connected": False, "note": "No DISCORD_BOT_TOKEN in secrets.env yet."}
    return {"connected": False, "note": _error or "Not signed in."}


register(Platform(
    name="discord", title="Discord",
    description=("The user texts you on Discord: you answer their DMs and mentions, "
                 "and can send messages and files to chats."),
    connect=_connect, disconnect=_disconnect, status=_status))
