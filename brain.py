"""The model: qwen3.8:27b on the DGX Spark, streamed, with text-protocol tools.

Ollama is stateless, so every request is rebuilt from scratch: the system
prompt (prompts/) with long-term memory appended, the short-term window of
recent conversation, and the new message last (see memory/). Every message
opens with a stamp of when it was sent, the new one included, which is how the
model knows the time. The system message deliberately carries no clock: it
would change on every request and stop Ollama reusing the cached prompt.

converse() is the tool loop on its own, over whatever messages it's given.
reply() uses it for the assistant; subagents use it with their own messages,
their own prompt and a narrower set of tools.

Tools don't use Ollama's tools API. The model writes a line

    [[call tool_name {"arg": "value"}]]

and we run the first one it writes, hand the result back as a new user turn
("Tool result for NAME: {json}"), and go again until a reply has no call in
it. Bad JSON and unknown tools go back to the model as {"ok": false, ...}
rather than raising, so it can correct itself.

Replies stream as they're generated. A call line is held back as soon as it
could be one, so "[[call get_time {}]]" never flashes up on screen, and the
stream is closed the moment a call is complete — the model is told the call
ends its turn, and there's no point paying for anything it writes after.

Text that could be the start of "Tool result for" is held back the same way.
The model occasionally skips the call and invents the result itself — a
confident, wrong time — so if it writes one, the stream is cut there, nothing
of it is shown, and it's told to make the call for real.

A reply can take more messages while it's being written (an Inbox: texting
on Discord, where people send a burst). Each one goes in front of the model
before its next round; one that lands mid-stream cuts the stream off there and
the round starts again with it, so "actually make it Saturday" steers the
answer already under way instead of waiting behind it.
"""

import itertools
import json
import re
import threading
import time
from dataclasses import dataclass

import httpx

import business
import config
import endpoints
import memory
import prompts

CALL = re.compile(r"\[\[\s*call\s+(\w+)\s*(\{.*?\})?\s*\]\]", re.S)
# What a tool result starts with. Only we write these; now and then the model
# skips the call and writes one itself, made-up numbers and all.
FAKE = "Tool result for"
CORRECTION = ("You wrote a tool result yourself. Tool results only ever come from "
              "me, after you call a tool. If you need a tool, call it now with "
              "[[call tool_name {...}]]; otherwise answer without one.")

# The time stamp memory puts at the start of every message ("[Wed Sep 30 2026,
# 4:28:22 PM] "). Only we write these; a reply that opens with one has copied
# the pattern, and the copy is dropped before anyone sees it.
STAMP = re.compile(r"^\s*\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [^\]\n]*\]\s*")

# A reply with no tool call that still reads as if it used one. Checked at most
# twice per reply, only when no tool ran: naming a tool, or ending on a promise
# like "let me check…" with nothing after it. (Stating the time is fine now:
# every message is stamped with it.)
_PROMISE = re.compile(r"\b(?:let me(?! know)|i'?ll|i will|i'?m going to|calling|checking|"
                      r"one sec(?:ond)?|hang on|give me a (?:sec|moment))\b", re.I)
UNBACKED = ("You didn't call any tool in that reply — tools only run when you write a "
            "[[call tool_name {...}]] line, and saying you will, or giving a result, "
            "doesn't run anything. The user has already seen what you wrote. If you "
            "meant to use a tool, call it now. If what you wrote claimed something a "
            "tool never told you, correct it in one short sentence. If it was fine "
            "as it was, reply with nothing.")

# keep_going (endpoints/flow.py) does nothing but give the model another turn,
# for when it's said what it's about to do and stopped short of the call. It
# isn't shown as a tool use and doesn't use up the tool budget — it's how the
# model asks to keep talking, not work — but a run of them with no real call
# in between is a model going round in circles, so that many in a row ends the
# reply.
KEEP_GOING = "keep_going"
KEEP_GOING_STREAK = 2
CARRY_ON = ("Go ahead. The user has already seen what you wrote, so don't repeat it "
            "or announce the step again: make the call that does what you said, "
            "now. If there's nothing left to do, answer.")

# Added to the user's message every config.MEMORY_REVIEW_EVERY messages (see
# memory.review_due). It rides on the message sent to the model only; the
# window keeps what the user actually said, so it never shows up again.
REVIEW = ("\n\n[Memory review — this note is from BananaGent itself, not the user, "
          "and they can't see it. Before you reply, go back over the last {n} "
          "messages of this conversation, this one included, and save to long-term "
          "memory every lesson you learned and every important thing worth "
          "remembering: facts about the user and the people and projects in their "
          "life, their preferences and decisions, corrections they gave you, and "
          "anything you got wrong along with how to get it right next time. Use "
          "memory_add, one fact per call, and memory_edit for a saved fact that's "
          "now wrong or out of date. Anything about the business goes in the "
          "business record instead: business_note for lasting facts, business_log "
          "for decisions and findings, business_lead and business_lead_touch for "
          "people, business_task_add and business_task_update for to-dos, "
          "business_metric for numbers. Skip what's already in your long-term "
          "memory or the business record, and skip small talk; if nothing new is "
          "worth keeping, save nothing. "
          "Don't mention this review — once you're done, reply to their message as "
          "you normally would.]")

# What a message that arrives during a reply is labelled with (see Inbox).
# Where the reply had got to decides which: cut off mid-stream, between tool
# calls, or just after it finished.
CUT_IN = ("[They sent this while you were still writing, so your reply was cut off "
          "where it stands — the unfinished end of it never reached them. Read it "
          "together with what they asked before: if it changes, corrects or adds to "
          "the job, go with the newest, now. Don't start over or repeat what they've "
          "already seen; carry on from it.]")
MEANWHILE = ("[They sent this while you were working on their last message. If it "
             "changes, corrects or adds to the job, go with the newest.]")
AFTER = ("[They sent this just as you finished replying. Answer it, without "
         "repeating what you already said.]")

_TIMEOUT = httpx.Timeout(10.0, read=300.0)  # a cold load is ~30s; a wedge shouldn't be forever


def _url(route):
    return config.OLLAMA_HOST.rstrip("/") + route


def _body(messages):
    return {
        "model": config.MODEL,
        "stream": True,
        "think": config.THINK,
        "keep_alive": config.KEEP_ALIVE,
        "options": {"num_ctx": config.NUM_CTX, "temperature": config.TEMPERATURE},
        "messages": messages,
    }


def warm():
    """Load the model now, with the same num_ctx a real request uses.

    A different num_ctx would make Ollama reload on the first real message,
    which is exactly the wait this is here to avoid.
    """
    body = _body([])
    body["stream"] = False
    r = httpx.post(_url("/api/chat"), json=body, timeout=httpx.Timeout(10.0, read=300.0))
    r.raise_for_status()


# -- the reply ------------------------------------------------------------------
def system_message():
    """The prompts, then long-term memory, then the business — exactly what
    the model is sent."""
    saved = memory.render() or "Nothing saved yet."
    return (f"{prompts.system_prompt()}\n\n# Long-term memory\n\n"
            f"Notes you've saved with the memory tools. They're kept forever and "
            f"shown to you in every conversation.\n\n{saved}\n\n"
            f"# The business: {business.name()}\n\n"
            f"The business the user is building, kept forever with the business "
            f"tools, apart from long-term memory. How each date stands against "
            f"today is worked out for you in brackets.\n\n"
            f"{business.render() or 'Nothing saved yet.'}")


_ids = itertools.count(1)


@dataclass
class ToolUse:
    """A tool call, as the conversation shows it: yielded by converse() among
    the text chunks once when it starts (status "running") and again, the same
    object, when it finishes ("ok" or "failed")."""
    id: int
    name: str
    args: object  # the parsed arguments, or the raw text if they weren't valid JSON
    status: str = "running"
    result: str = ""  # what the model was handed back, trimmed for keeping in memory

    def snapshot(self):
        """A plain copy, for handing to another thread."""
        return {"id": self.id, "name": self.name, "args": self.args,
                "status": self.status, "result": self.result}


class Interrupted:
    """Yielded by converse() when a new message cut the stream off: the text
    since the last one stops there, and what follows answers both."""


class Inbox:
    """Messages that come in for a reply while it's still being written.

    put() from any thread; converse() takes them before every round, and
    `arrived` stops a stream mid-way. Once the reply is over, close() shuts it
    and hands back anything that came too late to be seen, which is the
    caller's to answer as a new message; put() then refuses, for the same reason.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._news = []
        self._closed = False
        self.arrived = threading.Event()
        self.received = []  # every message it took, in order

    def put(self, text, at=None, images=None, payload=None):
        item = {"text": text, "at": at or time.time(), "images": images or [],
                "payload": payload}
        with self._lock:
            if self._closed:
                return False
            self._news.append(item)
            self.received.append(item)
            self.arrived.set()
            return True

    @property
    def closed(self):
        return self._closed

    def pending(self):
        with self._lock:
            return bool(self._news)

    def take(self):
        with self._lock:
            news, self._news = self._news, []
            self.arrived.clear()
            return news

    def close(self):
        with self._lock:
            self._closed = True
            late, self._news = self._news, []
            for item in late:
                self.received.remove(item)
            return late


def reply(text, sent_at=None, images=None, inbox=None):
    """Answer one message from the user, yielding the reply a chunk at a time.

    Chunks are text, with a ToolUse among them wherever a tool ran.
    `sent_at` is when the user sent it (default: now); it's stamped on the
    message the same way the window's messages are.

    The short-term window supplies the conversation so far. Recording this
    exchange into it is the caller's job, once the reply is complete — a reply
    that fails or is cut off shouldn't become something the model thinks it said.

    Every config.MEMORY_REVIEW_EVERY messages the message also carries REVIEW,
    and the reply gets extra tool calls for the saving. A reply cut off before
    it finishes doesn't count as a review, so the next message tries again.

    `images` (base64) go with the message for the model to look at. `inbox`
    takes more messages while the reply is written; see Inbox and converse().
    """
    review = memory.review_due()
    note = REVIEW.format(n=config.MEMORY_REVIEW_EVERY) if review else ""
    if review:
        print("[brain] memory review due; asking the model to save what it learned")
    asked = {"role": "user", "content": memory.stamp(sent_at or time.time(),
                                                     memory.last_at()) + text + note}
    if images:
        asked["images"] = list(images)
    messages = [{"role": "system", "content": system_message()}, *memory.window(), asked]
    rounds = config.MAX_TOOL_ROUNDS + (config.MEMORY_REVIEW_ROUNDS if review else 0)
    yield from converse(messages, max_rounds=rounds, inbox=inbox)
    if review:
        memory.reviewed()


def converse(messages, allowed=None, max_rounds=None, before_round=None, inbox=None):
    """The tool loop over `messages`, which it appends to as it goes.

    `allowed` limits which tools may be called (None means the assistant's
    own); anything else goes back to the model as an error, as an unknown tool
    would. Stops after max_rounds tool calls, telling the model so on the
    last one so it answers with what it has.

    `before_round(messages)`, if given, runs before every request to the
    model. A long-running agent uses it to notice a stop, pass on messages
    that arrived meanwhile, and shrink old tool results before they outgrow
    the context. It may raise to end the loop.

    `inbox`, if given, is an Inbox of messages from the user that arrive
    while this runs. Each is added before the next round; one that arrives
    mid-stream cuts it off, yields an Interrupted, and the round is run again
    with it. Neither uses up a tool round. One that arrives as the reply ends
    gets the reply carried on to answer it.

    Yields text chunks, and a ToolUse before and after each tool runs.
    """
    max_rounds = config.MAX_TOOL_ROUNDS if max_rounds is None else max_rounds
    allowed = endpoints.names() if allowed is None else allowed
    room = config.NUM_CTX * config.CHARS_PER_TOKEN
    last = ""  # the last character that went out, across tool rounds
    called = False  # whether any tool ran in this reply
    checks = 0      # unbacked claims sent back so far; see _unbacked
    free = 0        # keep_going calls, which don't count against max_rounds
    streak = 0      # keep_going calls since the last real one
    for step in itertools.count():
        round_no = step - free
        if round_no > max_rounds:
            return
        if before_round is not None:
            before_round(messages)
        if inbox is not None:
            _hand_over(messages, inbox, MEANWHILE)
        if sum(len(m["content"]) for m in messages) > room:
            raise RuntimeError("This conversation has outgrown the model's context.")
        said, call, faked, cut = [], None, False, False
        for kind, value in _stream(messages, inbox.arrived if inbox is not None else None):
            if kind == "call":
                call = value
            elif kind == "fake":
                faked = True
            elif kind == "cut":
                cut = True
            elif value:
                # Text from before a tool call and the answer after it would
                # otherwise run together with no space between — or, when the
                # earlier text already ended a line, start with a stray one.
                if last and not said:
                    if last.isspace():
                        value = value.lstrip(" ")
                        if not value:
                            continue
                    elif not value[0].isspace():
                        yield " "
                said.append(value)
                last = value[-1]
                yield value

        if cut:
            print("[brain] a new message came in mid-reply; going again with it")
            free += 1
            if not "".join(said).strip():
                _hand_over(messages, inbox, MEANWHILE)  # nothing written yet to cut
                continue
            yield Interrupted()
            last = "\n"
            messages.append({"role": "assistant", "content": "".join(said).rstrip() + " —"})
            _hand_over(messages, inbox, CUT_IN)
            continue
        if faked:
            print("[brain] the model wrote its own tool result; asking it to call instead")
            messages.append({"role": "assistant", "content": "".join(said) or "(nothing)"})
            messages.append({"role": "user", "content": CORRECTION})
            continue
        if call is None:
            reason = None if called or checks >= 2 else _unbacked("".join(said), allowed)
            if reason is None:
                if inbox is not None and inbox.pending():
                    free += 1
                    messages.append({"role": "assistant", "content": "".join(said) or "(nothing)"})
                    _hand_over(messages, inbox, AFTER)
                    continue
                return
            # It claimed or promised a tool without calling one. Left alone
            # that gets saved to memory, and the model copies its own example
            # on the next message — the lie repeats itself. Twice, because the
            # first answer to being caught is sometimes "let me do it properly"
            # followed by nothing.
            checks += 1
            print(f"[brain] {reason}; sending it back")
            messages.append({"role": "assistant", "content": "".join(said)})
            messages.append({"role": "user", "content": UNBACKED})
            continue
        if round_no >= max_rounds:
            # It was told the last call was its last and called again anyway.
            yield ("" if last.isspace() or not last else " ") + \
                "(I ran out of tool calls before I could finish that.)"
            return

        name, raw_args, call_text = call
        if name == KEEP_GOING and name in allowed:
            streak += 1
            if streak > KEEP_GOING_STREAK:
                print("[brain] keep_going over and over with nothing done; ending the reply")
                return
            free += 1
            messages.append({"role": "assistant", "content": "".join(said) + call_text})
            messages.append({"role": "user", "content": f"Tool result for {name}: {CARRY_ON}"})
            continue
        streak = 0
        called = True
        use = ToolUse(next(_ids), name, _shown_args(raw_args))
        yield use
        result, ok = _run(name, raw_args, allowed)
        use.status = "ok" if ok else "failed"
        use.result = result if len(result) <= config.MEMORY_RESULT_CHARS else \
            result[:config.MEMORY_RESULT_CHARS] + " …(cut)"
        yield use
        messages.append({"role": "assistant", "content": "".join(said) + call_text})
        feedback = f"Tool result for {name}: {result}"
        if "".join(said).strip():
            # It answered first and called afterwards (typically saving a
            # memory). Left alone, it writes the whole answer out again.
            feedback += ("\n\nThe user has already seen everything you wrote before "
                         "this call. Carry on from there without repeating any of it. "
                         "If what you wrote already answered them completely, reply "
                         "with nothing; otherwise give the answer now.")
        if round_no + 1 >= max_rounds:
            feedback += ("\n\nThat was your last tool call for this message. Answer "
                         "now with what you have, without calling anything else.")
        messages.append({"role": "user", "content": feedback})


def _hand_over(messages, inbox, note):
    """Put whatever has come into the inbox in front of the model: on the end
    of the last user message (a tool result, typically), or as a new one."""
    news = inbox.take()
    if not news:
        return
    for item in news:
        print(f"[brain] took a message mid-reply: {item['text'][:80]!r}")
    block = "\n\n".join(memory.stamp(n["at"]) + n["text"] for n in news) + "\n\n" + note
    images = [i for n in news for i in n["images"]]
    if messages[-1]["role"] == "user":
        messages[-1]["content"] += "\n\n" + block
        if images:
            messages[-1]["images"] = messages[-1].get("images", []) + images
    else:
        messages.append({"role": "user", "content": block, **({"images": images} if images else {})})


def _stream(messages, stop=None):
    """One request. Yields ("text", str) as it arrives, then ("call", ...) if
    the model wrote a tool call — at which point the stream is closed. If the
    `stop` event is set partway, it yields ("cut", None) and closes it there."""
    pending = ""  # text that might be the start of a call line, not shown yet
    opening = True  # still at the start, where a copied time stamp would be
    with httpx.stream("POST", _url("/api/chat"), json=_body(messages), timeout=_TIMEOUT) as r:
        if r.status_code != 200:
            r.read()
            raise RuntimeError(f"The model server said {r.status_code}: {_error_text(r)}")
        for line in r.iter_lines():
            if stop is not None and stop.is_set():
                yield "cut", None
                return
            if not line:
                continue
            chunk = json.loads(line)
            if "error" in chunk:
                raise RuntimeError(f"ollama: {chunk['error']}")
            # With THINK on, reasoning arrives in message.thinking. It's never
            # part of the answer, so only content is read.
            pending += chunk.get("message", {}).get("content", "")
            if opening:
                lead = pending.lstrip()
                if not lead or (lead[0] == "[" and not lead.startswith("[[")
                                and "]" not in lead and len(lead) < 80):
                    continue  # might still turn out to be a stamp
                pending, opening = STAMP.sub("", pending, count=1), False

            match = CALL.search(pending)
            fake = pending.find(FAKE)
            if fake != -1 and (not match or fake < match.start()):
                if pending[:fake].strip():
                    yield "text", pending[:fake]
                yield "fake", None
                return
            if match:
                before = pending[:match.start()]
                if before:
                    yield "text", before
                yield "call", (match.group(1), match.group(2), match.group(0))
                return

            show, pending = _split_safe(pending)
            if show:
                yield "text", show
            if chunk.get("done"):
                break
    if pending:
        yield "text", pending  # it looked like a call starting, but never became one


def _split_safe(pending):
    """Split text into what can be shown now and what might begin a call line
    or a made-up tool result."""
    show, hold = _split_call(pending)
    for n in range(min(len(FAKE) - 1, len(show)), 0, -1):
        if show.endswith(FAKE[:n]):
            return show[:-n], show[-n:] + hold
    return show, hold


def _split_call(pending):
    i = pending.find("[[")
    if i == -1:
        return (pending[:-1], "[") if pending.endswith("[") else (pending, "")
    after = pending[i + 2:].lstrip()
    if after and not "call".startswith(after[:4]):
        # Brackets, but not a call: show through them and look again after.
        show, hold = _split_call(pending[i + 2:])
        return pending[:i + 2] + show, hold
    return pending[:i], pending[i:]


def _unbacked(text, allowed=None):
    """Why this tool-less reply looks like it claims or promises a tool, or None."""
    text = text.strip()
    if not text:
        return None
    names = allowed if allowed is not None else endpoints.names()
    if any(re.search(rf"\b{re.escape(n)}\b", text) for n in names):
        return "it named a tool without calling one"
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    final = sentences[-1]
    # "If you're happy with it, I'll approve it." after asking the user
    # something is a promise for later, not one it broke — common when
    # relaying an agent's question.
    later = re.match(r"(?:if|when|once|as soon as|whenever)\b", final.strip(), re.I) or \
        (len(sentences) > 1 and sentences[-2].rstrip().endswith("?"))
    if _PROMISE.search(final) and not final.rstrip().endswith("?") and not later:
        return "it said it would do something, then stopped"
    return None


def _shown_args(raw_args):
    """The arguments as the UI shows them: parsed if possible, else as written."""
    try:
        return json.loads(raw_args) if raw_args else {}
    except ValueError:
        return raw_args


def _run(name, raw_args, allowed=None):
    """Run one tool call. Returns (JSON text for the model, whether it worked);
    never raises."""
    try:
        args = json.loads(raw_args) if raw_args else {}
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except ValueError as e:
        result = {"ok": False, "error": f"Couldn't read the arguments as JSON: {e}"}
    else:
        if allowed is not None and name not in allowed:
            result = {"ok": False, "error": f"'{name}' isn't one of your tools. "
                                            f"Yours are: {', '.join(sorted(allowed))}"}
        else:
            result = endpoints.dispatch(name, args)
    out = json.dumps(result, default=str, ensure_ascii=False)
    print(f"[tool] {name} {raw_args or '{}'} -> {'ok' if result.get('ok') else 'failed'}")
    # Tools that hand back a big piece of work — a subagent's result, Claude's
    # reply — get more room than a lookup does.
    ep = endpoints.get(name)
    limit = (ep.result_chars if ep is not None and ep.result_chars
             else config.TOOL_RESULT_CHARS)
    if len(out) > limit:
        out = out[:limit] + " …(cut)"
    return out, bool(result.get("ok"))


def _error_text(response):
    try:
        return response.json().get("error") or response.text
    except Exception:
        return response.text


if __name__ == "__main__":
    import sys

    for piece in reply(" ".join(sys.argv[1:]) or "Say hi in five words."):
        if isinstance(piece, ToolUse):
            if piece.status != "running":
                print(f"\n  ({piece.name} {json.dumps(piece.args)} -> {piece.status})")
            continue
        print(piece, end="", flush=True)
    print()
