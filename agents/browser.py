"""An agent that uses the web the way you would: a real Chrome window, driven.

Not a research agent. It is pointed at things that *change* something — filling
in a form, working through a signup, changing a setting on a site — so the
design question is not "how do I read pages" but "what is it allowed to do
without asking".

The answer: it navigates, reads, and types into fields on its own, and it stops
and asks a person before anything that is hard to take back — submitting a
form, creating an account, spending money, agreeing to terms, sending a message.
Two things decide that. The model labels its own next step, and a keyword pass
over the instruction labels it independently; either one is enough to trigger the
gate. A model that talks itself into "this submit button is harmless" still gets
stopped by the second check.

Two things it will not do at all. It never types a password — it opens the login
page, hands you the window, and waits, which works because the browser is real
and visible and yours. And it never touches a CAPTCHA: those exist to establish
that a person is present, so it stops and asks you to do it.

The browser stays open between tasks and keeps its own Chrome profile, so a site
you logged into once stays logged in for the next task.

Stagehand is async and the agent loop reads better as straight-line code, so one
thread owns an asyncio loop for the whole session and the task thread hands work
to it. That also keeps every Stagehand object bound to the loop that made it.
"""

import asyncio
import collections
import os
import re
import threading

import config
from . import llm
from .registry import Agent, Denied, register

# Steps that stop and ask. The key is what the model is told to call it; the
# value is how the question is put to the person.
GATES = {
    "submit": "Submit this form?",
    "account": "Create this account?",
    "purchase": "Go through with this purchase?",
    "consent": "Accept these terms?",
    "message": "Send this?",
    "delete": "Delete this?",
}

# The independent check, in the order it is tried. Deliberately blunt: a false
# stop costs one click, a missed one costs something that already happened.
# Each pattern also says what kind of step it caught, so the question put to the
# person matches what is about to happen rather than defaulting to "submit".
RISKY = [
    ("account", re.compile(r"\b(sign ?up|register|create (an? )?account|join now)\b", re.I)),
    ("purchase", re.compile(r"\b(buy|purchase|pay|checkout|check out|place (the )?order|"
                            r"place bid|continue to payment|add payment)\b", re.I)),
    ("consent", re.compile(r"\b(i agree|agree to|accept (the )?(terms|cookies)|consent)\b", re.I)),
    ("message", re.compile(r"\b(send|post|publish|reply|comment|tweet)\b", re.I)),
    ("delete", re.compile(r"\b(delete|remove|deactivate|close (the )?account)\b", re.I)),
    ("submit", re.compile(r"\b(submit|confirm|apply|subscribe|book|order|"
                          r"continue|next|finish)\b", re.I)),
]


def _kind_from_words(instruction):
    for kind, pattern in RISKY:
        if pattern.search(instruction):
            return kind
    return None

CAPTCHA = re.compile(r"captcha|recaptcha|hcaptcha|cloudflare|are you a robot|"
                     r"verify you are human|security check", re.I)

CREDENTIAL = re.compile(r"\b(password|passcode|2fa|two.factor|one.time code|otp|"
                        r"security code|card number|cvv|credit card|ssn)\b", re.I)

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string", "description": "One sentence on why this step."},
        "action": {"type": "string",
                   "enum": ["navigate", "act", "extract", "finish", "give_up"]},
        "instruction": {"type": "string",
                        "description": "For act: one concrete UI step, e.g. "
                                       "'click the Sign up button'. For extract: what to read."},
        "url": {"type": "string", "description": "For navigate only."},
        "risk": {"type": "string",
                 "enum": ["none", "submit", "account", "purchase", "consent",
                          "message", "delete", "credentials"]},
        "answer": {"type": "string", "description": "For finish: the answer to report."},
    },
    "required": ["thought", "action", "risk"],
}

SYSTEM = """You are operating a real web browser on someone's own computer, on their behalf.

You are given a task, and after every step you are shown the page you are now on.
Choose exactly one next step each time.

navigate  go to a URL. Use this first if the task names a site.
act       one concrete interaction, described the way you would tell a person:
          "click the Sign up button", "type jacob@example.com into the email field".
          One step at a time. Never chain steps with "and then".
extract   read something off the page and remember it.
finish    the task is done. Put what you found, or what you did, in `answer`.
give_up   it cannot be done from here. Explain why in `answer`.

Label every step honestly in `risk`:
  none         moving around, reading, typing into a field
  submit       submitting a form
  account      creating an account
  purchase     spending money
  consent      agreeing to terms or a cookie banner
  message      sending or posting something other people will see
  delete       removing something
  credentials  a password, a card number, or a one-time code is being asked for

Anything other than `none` stops and asks the person first, so labelling honestly
costs you nothing and mislabelling wastes their time. Never type a password or a
card number yourself — label it `credentials` and the person will type it.

Prefer the simplest step that makes progress. If a page didn't change the way you
expected, look at what is actually on it now rather than repeating yourself.

Before anything else, read WHAT YOU HAVE DONE. If the answer to the task is
already there, choose `finish` immediately and put it in `answer`. Never read the
same thing twice."""


def _release_profile(profile):
    """Kill a leftover Chrome still holding our profile directory.

    Only ours: matched on this exact profile path in the command line, so the
    Chrome you have open with your own tabs is never touched. Without this, one
    hard kill of the assistant leaves an orphan and every later launch times out
    waiting for a directory it will never get.
    """
    import subprocess

    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{profile}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       capture_output=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def _snip(text, n):
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    return text if len(text) <= n else text[:n] + f"\n… [{len(text) - n} more characters]"


def _readable(extracted):
    """The value Stagehand extracted, as plain text the model can act on.

    It comes back wrapped in a result object; handing the model that object's
    repr tells it nothing, so it reads the same thing again on the next step.
    """
    import json

    value = extracted
    for attribute in ("extraction", "data", "result"):
        inner = getattr(value, attribute, None)
        if inner is not None:
            value = inner
            break
    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        return str(value)


class BrowserAgent(Agent):
    name = "browser"
    title = "Web Browser"
    description = (
        "Uses the web in a real Chrome window — opening sites, filling in forms, "
        "working through signups and checkouts — pausing to ask before anything "
        "that can't be taken back.")
    gated = tuple(GATES)

    def __init__(self):
        super().__init__()
        self.url = None
        self.page_title = None
        self.findings = []
        self._loop = None
        self._loop_thread = None
        self._browser = None
        self._sh = None
        self._page = None
        self._boot = threading.Lock()

    # -- the asyncio side ----------------------------------------------------
    def _ensure_loop(self):
        if self._loop is not None:
            return self._loop
        self._loop = asyncio.new_event_loop()

        def spin(loop):
            asyncio.set_event_loop(loop)
            try:
                loop.run_forever()
            finally:
                # Closed on its own thread once it has stopped, or Windows
                # complains about subprocess pipe handles on the way out.
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()

        self._loop_thread = threading.Thread(
            target=spin, args=(self._loop,), name="browser-loop", daemon=True)
        self._loop_thread.start()
        return self._loop

    def _call(self, coro, timeout=180.0):
        """Run one coroutine on the browser's loop and wait for it here."""
        future = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        try:
            return future.result(timeout=timeout)
        except Exception:
            future.cancel()
            raise

    async def _open_browser(self):
        from stagehand import Stagehand
        from stagehand.browser import LocalBrowser

        profile = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               config.AGENT_PROFILE)
        os.makedirs(profile, exist_ok=True)
        chrome = config.AGENT_CHROME if config.AGENT_CHROME and os.path.exists(
            config.AGENT_CHROME) else None

        _release_profile(profile)
        self._browser = await LocalBrowser().launch(
            executable_path=chrome,
            headless=config.AGENT_HEADLESS,
            user_data_dir=profile,          # so yesterday's logins are still there
            preserve_user_data_dir=True,
            # Not keep_alive: a Chrome that outlives us keeps the profile locked,
            # and the next launch then times out waiting for a directory the
            # orphan still owns. Logins survive in the profile, not the process.
            keep_alive=False,
            viewport_width=config.AGENT_VIEWPORT[0],
            viewport_height=config.AGENT_VIEWPORT[1],
            accept_downloads=False,         # nothing lands on disk unasked
        )
        self._sh = await Stagehand.create(
            browser=self._browser,
            model=llm.generate,             # local Ollama, no provider key
            system_prompt=SYSTEM,
            self_heal=True,
            logging={"level": "error"},
        )
        context = self._sh.browser.context
        open_pages = await context.pages()
        self._page = open_pages[0] if open_pages else await context.new_page()
        return self._page

    async def _close_browser(self):
        for closer in (getattr(self._sh, "close", None), getattr(self._browser, "close", None)):
            if closer is None:
                continue
            try:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
        self._sh = self._browser = self._page = None

    async def _part(self, what, coro, seconds, default=None):
        """One piece of looking at the page, on its own clock.

        Reading a page is four separate calls into Chrome and any of them can
        stall on a page that never finishes settling. Giving each its own budget
        means a slow screenshot costs the screenshot, not the whole task.
        """
        try:
            return await asyncio.wait_for(coro, seconds)
        except asyncio.TimeoutError:
            self.log(f"the page's {what} took more than {seconds:.0f}s — carrying on without it")
        except Exception as e:
            self.log(f"couldn't read the page's {what}: {type(e).__name__}")
        return default

    async def _look(self):
        """Where we are, what's on it, and optionally a picture of it."""
        page = self._page
        url = page.url if isinstance(page.url, str) else await self._part(
            "address", page.url(), 10.0, self.url or "")
        title = await self._part("title", page.title(), 10.0, "")

        text = await self._part("contents", page.snapshot(), 25.0)
        if text is None:
            text = await self._part(
                "text", page.evaluate("() => document.body.innerText"), 15.0, "")
        if not isinstance(text, str):
            text = str(text)

        shot = None
        if config.AGENT_SCREENSHOTS:
            shot = await self._part("picture", page.screenshot(), 25.0)
        return url, title, _snip(text, config.AGENT_PAGE_CHARS), shot

    # -- the agent loop ------------------------------------------------------
    def _run(self, task):
        with self._boot:
            if self._sh is None:
                self.step("opening Chrome")
                self._call(self._open_browser(), timeout=120.0)

        model = llm.resolve_model()
        self.log(f"driving with {model}")

        history = []
        recent = collections.deque(maxlen=3)
        self.findings = []
        answer = None
        for _ in range(config.AGENT_MAX_STEPS):
            self.check_stop()
            url, title, page_text, shot = self._call(self._look(), timeout=120.0)
            self.url, self.page_title = url, title

            if CAPTCHA.search(page_text or "") or CAPTCHA.search(title or ""):
                # Not something to work around — it is a request for a human,
                # and there is one right here.
                self.ask("captcha",
                         "This page is asking to confirm you're human. Solve it in "
                         "the browser window, then approve to carry on.",
                         detail=title or url, timeout=config.AGENT_APPROVAL_SECONDS)

            decision = self._decide(task, history, url, title, page_text, shot)
            action = decision.get("action", "give_up")
            instruction = (decision.get("instruction") or "").strip()
            risk = decision.get("risk", "none")
            thought = (decision.get("thought") or "").strip()

            if action == "finish":
                answer = decision.get("answer") or "Done."
                self.step(f"finished: {_snip(answer, 160)}")
                break
            if action == "give_up":
                answer = decision.get("answer") or "Couldn't do it."
                self.step(f"gave up: {_snip(answer, 160)}")
                break

            # A step the model didn't fill in properly is corrected and retried
            # rather than thrown: it costs one turn and the model can see what
            # it got wrong, which beats failing the whole task on a blank field.
            target = (decision.get("url") or "").strip()
            if action == "navigate" and not target and not instruction:
                history.append("REJECTED a navigate step: no url was given")
                self.log("model asked to navigate nowhere; asking again")
                continue
            if action in ("act", "extract") and not instruction:
                history.append(f"REJECTED an {action} step: `instruction` was empty. "
                               "Say exactly what to do or read, in one short phrase.")
                self.log(f"model gave an empty {action} instruction; asking again")
                continue

            # Going round in circles: say so rather than letting it burn every
            # remaining step repeating a call that already worked.
            recent.append((action, instruction))
            if len(recent) == recent.maxlen and len(set(recent)) == 1:
                history.append(
                    f"You have now done `{action}: {instruction}` three times and the "
                    "answer is already above. Either finish, or do something different.")
                self.log("model is repeating itself; nudged it")
                recent.clear()
                continue

            self._gate(action, instruction, risk, thought, url)

            if action == "navigate":
                target = target or instruction
                self.step(f"opening {target}")
                self._call(self._page.goto(target), timeout=90.0)
                history.append(f"navigated to {target}")
            elif action == "extract":
                self.step(f"reading: {instruction}")
                got = self._call(self._sh.extract(instruction), timeout=180.0)
                found = _readable(got)
                self.findings.append(found)
                # The value itself, not a repr of the wrapper object — otherwise
                # the model can't see what was read and just reads it again.
                history.append(f"read ({instruction}): {_snip(found, 500)}")
            else:
                self.step(instruction or thought or "acting")
                self._call(self._sh.act(instruction), timeout=180.0)
                history.append(f"did: {instruction}")
            history[:] = history[-config.AGENT_MEMORY:]
        else:
            # Out of steps, but anything it read along the way is still worth
            # handing back — that is usually most of what was asked for.
            answer = f"Stopped after {config.AGENT_MAX_STEPS} steps without finishing."
            if self.findings:
                answer += " What I did read: " + _snip(" | ".join(self.findings), 600)
            self.log(answer)

        return {"answer": answer, "url": self.url, "steps": self.steps,
                "found": self.findings}

    def _gate(self, action, instruction, risk, thought, url):
        """Stop and ask, if this step is one of the ones that can't be undone."""
        if risk == "credentials" or CREDENTIAL.search(instruction):
            # Never typed by the agent, whatever the model intended.
            self.ask("credentials",
                     "This needs a password or card number. Type it into the "
                     "browser window yourself, then approve to carry on.",
                     detail=instruction or url, timeout=config.AGENT_APPROVAL_SECONDS)
            return

        flagged = risk if risk in GATES else None
        if flagged is None and action == "act":
            # The model called it harmless; the wording says otherwise.
            flagged = _kind_from_words(instruction)
        if flagged is None:
            return

        self.ask(flagged, GATES[flagged],
                 detail=f"{instruction or thought}\non {url}",
                 timeout=config.AGENT_APPROVAL_SECONDS)

    def _decide(self, task, history, url, title, page_text, shot):
        """Ask the model for exactly one next step, as JSON."""
        import base64

        done = "\n".join(f"- {h}" for h in history) or "- nothing yet"
        prompt = (f"TASK\n{task}\n\n"
                  f"WHAT YOU HAVE DONE\n{done}\n\n"
                  f"CURRENT PAGE\n{title or '(untitled)'} — {url}\n\n"
                  f"{page_text}\n\nChoose the single next step.")
        message = {"role": "user", "content": prompt}
        if shot:
            message["images"] = [base64.b64encode(shot).decode("ascii")
                                 if isinstance(shot, (bytes, bytearray)) else shot]

        response = self._call(llm.client().chat(
            model=llm.resolve_model(),
            messages=[{"role": "system", "content": SYSTEM}, message],
            format=DECISION_SCHEMA,
            options=config.AGENT_OPTIONS,
            keep_alive=config.AGENT_KEEP_ALIVE,
            think=config.AGENT_THINK,
        ), timeout=config.AGENT_STEP_SECONDS)

        import json

        raw = llm._strip_thinking((response.get("message") or {}).get("content", "") or "")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"action": "give_up", "risk": "none",
                    "answer": f"The model didn't answer in the expected shape: {_snip(raw, 200)}"}

    def visit(self, url):
        """Just open a page — no task, no model. For logging in by hand."""
        if self.busy():
            raise RuntimeError("The agent is mid-task. Stop it first.")
        url = str(url).strip()
        if not re.match(r"^[a-z]+://", url, re.I):
            url = "https://" + url
        with self._boot:
            if self._sh is None:
                self._call(self._open_browser(), timeout=120.0)
        self._call(self._page.goto(url), timeout=90.0)
        self.url = url
        self.log(f"opened {url}")
        return f"Opened {url} in the agent's browser."

    # -- housekeeping --------------------------------------------------------
    def _cleanup(self):
        if not config.AGENT_KEEP_BROWSER and self._sh is not None:
            try:
                self._call(self._close_browser(), timeout=30.0)
            except Exception:
                pass

    def close(self):
        """Shut the browser down for good — called when the assistant quits."""
        self.stop("assistant quitting")
        if self._sh is not None:
            try:
                self._call(self._close_browser(), timeout=20.0)
            except Exception:
                pass
        if self._loop is not None:
            # Stopping a loop with work still on it makes asyncio complain on the
            # way down about handles it can no longer cancel. Drain first.
            async def _drain():
                pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

            try:
                self._call(_drain(), timeout=10.0)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop = None

    def snapshot(self):
        state = super().snapshot()
        state.update({
            "url": self.url,
            "page_title": self.page_title,
            "browser_open": self._sh is not None,
            "model": llm.available() or f"{config.AGENT_MODEL} (not pulled)",
        })
        return state


register(BrowserAgent())
