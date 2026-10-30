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
import os
import re
import threading
import time
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
        _client.event(_on_ready)
        _client.event(_on_message)
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


async def _on_message(message):
    if message.author.bot or message.author.id not in _allowed or _handler is None:
        return
    in_server = message.guild is not None
    if in_server and not _for_us(message):
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
    """In a server, only what's said to the bot: a mention, a reply to one of
    its messages, or a follow-up in a channel it's answering or just answered."""
    if not config.DISCORD_ANSWER_IN_SERVERS:
        return False
    if _client.user in message.mentions:
        return True
    ref = message.reference
    if ref is not None and getattr(ref.resolved, "author", None) == _client.user:
        return True
    since = time.time() - _answered.get(message.channel.id, 0)
    return message.channel.id in _active or since < config.DISCORD_FOLLOW_UP_SECONDS


async def _receive(message):
    in_server = message.guild is not None
    text = message.clean_content
    if in_server:
        for name in {_client.user.name, message.guild.me.display_name}:
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
    return Incoming(channel_id=message.channel.id, message_id=message.id, where=where,
                    text=shown, prompt="\n".join(lines),
                    sent_at=message.created_at.timestamp(), in_server=in_server,
                    files=files, images=images)


async def _save(att, message_id, want_image=True):
    """Save one attachment. Returns (a line telling the model about it, the
    path or None, the image as base64 if the model should see it)."""
    size = f"{att.size / 1024:.0f} KB" if att.size < 1024 * 1024 else \
        f"{att.size / 1024 / 1024:.1f} MB"
    if att.size > config.DISCORD_DOWNLOAD_MAX_MB * 1024 * 1024:
        return f"(attached {att.filename}, {size} — too big to download, not saved)", None, None
    folder = os.path.join(FILES, time.strftime("%Y-%m-%d"))
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", att.filename) or "file"
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

    def send(self, content=None, path=None, timeout=None):
        """Send now, in order with the reply's own texts. Blocks; returns the
        message id. For the endpoints, while this chat has a reply going out."""
        future = concurrent.futures.Future()
        self._push(("send", {"content": content, "path": path}, future, False))
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


async def _send(channel, content=None, path=None, reference=None):
    import discord

    if not content and not path:
        raise ValueError("Nothing to send: give some text or a file.")
    kwargs = {"allowed_mentions": discord.AllowedMentions(everyone=False, roles=False,
                                                         users=True, replied_user=False)}
    if reference is not None:
        kwargs["reference"] = reference
    if path:
        kwargs["file"] = discord.File(path)
    msg = await channel.send(content or None, **kwargs)
    _answered[channel.id] = time.time()
    return msg


def _reference(channel, message_id):
    import discord

    return discord.MessageReference(message_id=message_id, channel_id=channel.id,
                                    fail_if_not_exists=False)


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


def save_files(message_id, chat=None):
    async def go():
        channel = await _resolve(chat)
        msg = await channel.fetch_message(int(message_id))
        if not msg.attachments:
            raise ValueError("That message has no files.")
        return [(await _save(a, msg.id, want_image=False))[0] for a in msg.attachments]
    return _call(go(), timeout=config.DISCORD_SEND_SECONDS * 4)


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
