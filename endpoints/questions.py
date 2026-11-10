"""ask_question: the model asks the user something and waits for the answer.

Three kinds: "choice" (pick one of a few options), "short" (a word or a line)
and "long" (a paragraph). Wherever the question is asked it's drawn as
something to answer rather than a sentence to reply to:

- the Chat tab shows a card with a button per option, or a button that opens
  a box to type the answer in (one line, or a big multi-line one);
- on Discord it's a message with a button per option, or an Answer button
  that opens Discord's pop-up form, short or paragraph;
- the corner overlay can't draw buttons, so it shows the question with its
  options numbered, and the answer is just typed in the box.

Asking ends the reply (ends_reply — brain.converse stops there), so the model
can't answer its own question or carry on as if it had. The answer comes back
as the user's next message, "Answer to your question "…": …", and starts a
reply of its own like any message does. Typing an answer normally works too.
"""

from .registry import MINOR, app, endpoint, param

KINDS = ("choice", "short", "long")
MAX_CHOICES = 10
QUESTION_CHARS = 1000
CHOICE_CHARS = 200


def clean(question, kind, choices=None):
    """The question as asked, checked: (question, kind, choices). Raises with
    a sentence the model can fix its call from."""
    question = " ".join(str(question or "").split())
    if not question:
        raise ValueError("Give the question to ask.")
    if len(question) > QUESTION_CHARS:
        raise ValueError(f"Keep the question under {QUESTION_CHARS} characters; put "
                         "background in your reply before the call instead.")
    kind = str(kind or "").strip().lower()
    if kind not in KINDS:
        kind = "choice" if choices else "short"
    if kind != "choice":
        return question, kind, []
    if isinstance(choices, str):
        # Models sometimes write the list as one string.
        sep = "\n" if "\n" in choices else ("|" if "|" in choices else ",")
        choices = choices.split(sep)
    seen, out = set(), []
    for c in choices or []:
        c = " ".join(str(c).split())
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c[:CHOICE_CHARS])
    if len(out) < 2:
        raise ValueError("A choice question needs at least two choices. For an open "
                         "question use kind short or long.")
    if len(out) > MAX_CHOICES:
        raise ValueError(f"Give at most {MAX_CHOICES} choices — fewer reads better.")
    return question, kind, out


def answer_message(question, answer):
    """What the user's answer is sent to the model as."""
    answer = str(answer).strip()
    sep = "\n\n" if "\n" in answer or len(answer) > 200 else " "
    return f"Answer to your question \"{question}\":{sep}{answer}"


def as_plain(question, kind, choices):
    """The question for somewhere that only shows text: the corner overlay."""
    if kind == "choice":
        lines = [question] + [f"  {i}. {c}" for i, c in enumerate(choices, 1)]
        return "\n".join(lines) + "\n(press ` and type your answer)"
    return f"{question}\n(press ` and type your answer)"


@endpoint(
    tier=MINOR, ends_reply=True,
    description=(
        "Asks the user a question they answer with a tap or by typing, and ends your "
        "reply to wait for it. Use it when you need them to decide or tell you "
        "something before you can go on: pick between options (kind choice — they "
        "get a button for each), a quick fact like a name or a date (kind short — a "
        "one-line box), or something that takes a few sentences to explain (kind "
        "long — a big box). Works in the Chat tab and on Discord. The question is "
        "shown to them by itself, so don't write it or the options in your reply: "
        "make the call with nothing before it. Ask one question at a time. Their "
        "answer comes back as their next message, starting \"Answer to your "
        "question\"."
    ),
    params={
        "question": param("string", "The question, short and specific.", required=True),
        "kind": param("string", "choice, short or long.", required=True, enum=KINDS),
        "choices": param("array", f"For kind choice: 2 to {MAX_CHOICES} options, each a "
                                  "few words, as a JSON list of strings. They can also "
                                  "type an answer of their own."),
    },
    returns="That the question is up and waiting.",
)
def ask_question(question, kind, choices=None):
    question, kind, choices = clean(question, kind, choices)
    where = "on their screen"
    try:
        a = app()
    except RuntimeError:
        a = None
    turn = getattr(a, "discord_turn", None)
    if turn is not None:
        from integrations import discord_bot
        discord_bot.ask(turn.channel_id, question, kind, choices)
        where = "in the Discord chat"
    how = {"choice": "a button for each choice", "short": "a box for a short answer",
           "long": "a big box for a long answer"}[kind]
    return (f"Asked {where}, with {how}. Your reply is over: write nothing more. "
            "Their answer will come as their next message.")
