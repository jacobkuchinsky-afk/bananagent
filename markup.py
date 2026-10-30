"""What a reply's text means: markdown, links, buttons and math.

The Chat tab draws replies as rich text (richtext.py) and the corner overlay
shows them as plain text (plain()). Both start here, so the two can't disagree
about what a reply says.

The model writes ordinary markdown — **bold**, *italic*, `code`, headings,
lists, quotes, tables, fenced code — plus a few things that do something when
clicked:

    [notes](C:\\Users\\jacob\\notes.txt)     a file or folder; opens it
    [the docs](https://example.com)         a web page; opens the browser
    [Open it](open:C:\\Users\\jacob)         the same, drawn as a button
    [Count files](run:powershell (ls).Count)   runs a one-liner when clicked
    [Yes, do it](send:Yes, do it)           sends that as your next message
    [Copy](copy:some text)                  puts the text on the clipboard
    ```python                               a python / powershell / cmd block
    print("hi")                             gets a Run button
    ```
    $e^{i\\pi} + 1 = 0$   $$\\frac{a}{b}$$   inline and display math (TeX)

Bare https:// addresses and absolute Windows paths (C:\\..., in `backticks`
too) become links on their own, since that's how the model most often names
them.

Nothing here touches Tk: parse() turns text into blocks of inline spans,
math() turns TeX into a small tree, and act()/run() carry out a click.
"""

import os
import re
import subprocess
import sys
import threading
import time
import webbrowser

# -- blocks ------------------------------------------------------------------
# parse() returns a list of blocks, each a tuple whose first item is its kind:
#   ("para", spans)
#   ("heading", level, spans)
#   ("item", depth, marker, spans)      one list item; "•" or "3."
#   ("quote", spans)
#   ("code", lang, code)
#   ("math", tex)                       display math, centred on its own line
#   ("rule",)
#   ("table", aligns, header, rows)     header and each row: a list of spans per cell
#
# and spans are:
#   ("text", text, style)               style: a frozenset of "b", "i", "s"
#   ("code", text, target)              target is set when it's a path
#   ("link", text, target, style)
#   ("button", label, action)           action: see act()
#   ("math", tex)
#   ("br",)

RUNNERS = {
    "python": "python", "py": "python", "python3": "python",
    "powershell": "powershell", "ps1": "powershell", "ps": "powershell", "pwsh": "powershell",
    "cmd": "cmd", "bat": "cmd", "batch": "cmd",
}

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([^\s`]*)[^`]*$")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_ITEM = re.compile(r"^(\s*)([-*+•]|\d{1,3}[.)])\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def parse(text):
    """Markdown to blocks. Forgiving by design: it's fed a reply mid-stream,
    so an unclosed fence is still code and an unclosed $$ is still math."""
    lines = (text or "").replace("\r\n", "\n").replace("\t", "    ").split("\n")
    blocks, para = [], []

    def flush():
        if para:
            spans = inline("\n".join(para))
            solo = [s for s in spans if not (s[0] == "text" and not s[1].strip())]
            if len(solo) == 1 and solo[0][0] == "math":
                # $…$ alone on its line is display maths in all but name.
                blocks.append(("math", solo[0][1]))
            else:
                blocks.append(("para", spans))
            para.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        fence = _FENCE.match(line)
        if fence:
            flush()
            mark, lang = fence.group(1), fence.group(2).lower()
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(mark[0] * len(mark)):
                body.append(lines[i])
                i += 1
            blocks.append(("code", lang, "\n".join(body).rstrip("\n")))
            i += 1
            continue

        opener = "$$" if stripped.startswith("$$") else "\\[" if stripped.startswith("\\[") else None
        if opener:
            closer = "$$" if opener == "$$" else "\\]"
            rest = stripped[2:]
            if closer in rest:  # all on one line: $$ x $$ (maybe with text after)
                tex, _, after = rest.partition(closer)
                flush()
                blocks.append(("math", tex.strip()))
                if after.strip():
                    para.append(after.strip())
                i += 1
                continue
            flush()
            body = [rest]
            i += 1
            while i < len(lines) and closer not in lines[i]:
                body.append(lines[i])
                i += 1
            if i < len(lines):
                body.append(lines[i].split(closer, 1)[0])
            blocks.append(("math", "\n".join(body).strip()))
            i += 1
            continue

        if not stripped:
            flush()
            i += 1
            continue

        heading = _HEADING.match(line)
        if heading:
            flush()
            blocks.append(("heading", len(heading.group(1)), inline(heading.group(2))))
            i += 1
            continue

        if _RULE.match(line) and not para:
            blocks.append(("rule",))
            i += 1
            continue

        if "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush()
            header = _cells(line)
            aligns = [_align(c) for c in _cells(lines[i + 1])]
            rows = []
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append([inline(c) for c in _cells(lines[i])])
                i += 1
            blocks.append(("table", aligns, [inline(c) for c in header], rows))
            continue

        item = _ITEM.match(line)
        if item and not _RULE.match(line):
            flush()
            indent, marker, body = item.groups()
            depth = min(3, len(indent) // 2)
            if marker[0].isdigit():
                marker = marker[:-1] + "."
            else:
                marker = "•" if depth % 2 == 0 else "◦"
            i += 1
            # Lines indented under an item carry on its text.
            while (i < len(lines) and lines[i].strip() and lines[i].startswith(" ")
                   and not _ITEM.match(lines[i]) and not _FENCE.match(lines[i])):
                body += "\n" + lines[i].strip()
                i += 1
            blocks.append(("item", depth, marker, inline(body)))
            continue

        quote = _QUOTE.match(line)
        if quote:
            flush()
            body = [quote.group(1)]
            i += 1
            while i < len(lines) and _QUOTE.match(lines[i]):
                body.append(_QUOTE.match(lines[i]).group(1))
                i += 1
            blocks.append(("quote", inline("\n".join(body).strip())))
            continue

        para.append(stripped)
        i += 1
    flush()
    return blocks


def _cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def _align(cell):
    cell = cell.strip()
    if cell.startswith(":") and cell.endswith(":"):
        return "center"
    return "right" if cell.endswith(":") else "left"


# -- inline ------------------------------------------------------------------
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"'`]+", re.I)
_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\[\w.$-]+\\)[^\s<>\"'`|*?]*")
_ESCAPABLE = set("\\`*_{}[]()#+-.!|~$<>")
_TRAILING = ".,;:!?'\""


def inline(text):
    """One stretch of text to spans. Hard line breaks are kept: a reply
    written as plain lines should still read as lines."""
    spans = []
    _inline(text, frozenset(), spans)
    return _merge(spans)


def _inline(text, style, out):
    i, n = 0, len(text)
    buf = []

    def flush():
        if buf:
            _words(("".join(buf)), style, out)
            buf.clear()

    while i < n:
        ch = text[i]

        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "(":  # \( inline math \)
                end = text.find("\\)", i + 2)
                if end != -1:
                    flush()
                    out.append(("math", text[i + 2:end].strip()))
                    i = end + 2
                    continue
            if nxt in _ESCAPABLE:
                buf.append(nxt)
                i += 2
                continue

        if ch == "\n":
            flush()
            out.append(("br",))
            i += 1
            continue

        if ch == "`":
            run = len(text[i:]) - len(text[i:].lstrip("`"))
            fence = text[i:i + run]
            end = text.find(fence, i + run)
            if end != -1:
                flush()
                code = text[i + run:end]
                if code.startswith(" ") and code.endswith(" ") and code.strip():
                    code = code[1:-1]
                out.append(("code", code, code if _is_path(code) else None))
                i = end + run
                continue

        if ch == "$":
            double = text.startswith("$$", i)
            if double:
                end = text.find("$$", i + 2)
                if end != -1 and text[i + 2:end].strip():
                    flush()
                    out.append(("math", text[i + 2:end].strip()))
                    i = end + 2
                    continue
            else:
                end = _math_end(text, i)
                if end is not None:
                    flush()
                    out.append(("math", text[i + 1:end]))
                    i = end + 1
                    continue

        if ch == "[":
            link = _link(text, i)
            if link:
                label, target, end = link
                flush()
                out.append(_link_span(label, target, style))
                i = end
                continue

        if ch == "<":
            end = text.find(">", i)
            inner = text[i + 1:end] if end != -1 else ""
            if inner and " " not in inner and (_URL.match(inner) or _is_path(inner)):
                flush()
                out.append(("link", inner, inner, style))
                i = end + 1
                continue

        if ch in "*_~":
            got = _emphasis(text, i, style, out, flush)
            if got:
                i = got
                continue

        if ch in "hHwW" and (i == 0 or not text[i - 1].isalnum()):
            m = _URL.match(text, i)
            if m:
                url = m.group(0).rstrip(_TRAILING)
                url = _balance(url)
                flush()
                out.append(("link", url, url, style))
                i += len(url)
                continue

        if (ch.isalpha() or ch == "\\") and (i == 0 or text[i - 1] in " (\n\t"):
            m = _PATH.match(text, i)
            if m and len(m.group(0)) > 3:
                path = _balance(m.group(0).rstrip(_TRAILING))
                flush()
                out.append(("link", path, path, style))
                i += len(path)
                continue

        buf.append(ch)
        i += 1
    flush()


def _words(text, style, out):
    out.append(("text", text, style))


def _merge(spans):
    merged = []
    for s in spans:
        if merged and s[0] == "text" and merged[-1][0] == "text" and merged[-1][2] == s[2]:
            merged[-1] = ("text", merged[-1][1] + s[1], s[2])
        else:
            merged.append(s)
    return merged


def _math_end(text, i):
    """Where a $…$ closes, by pandoc's rule — so "$5 and $10" stays money:
    no space just inside either $, and no digit right after the closing one.
    Only the very next $ can close it; if that one doesn't qualify, it's not
    math at all."""
    if i + 1 >= len(text) or text[i + 1] in " \t\n$" or (i and text[i - 1].isalnum()):
        return None
    j = i + 1
    while True:
        j = text.find("$", j)
        if j == -1 or "\n" in text[i:j]:
            return None
        if text[j - 1] != "\\":
            break
        j += 1
    if text[j - 1] in " \t" or (j + 1 < len(text) and text[j + 1].isdigit()):
        return None
    return j


def _link(text, i):
    """[label](target) starting at i: the label, the target and where it ends.
    The target may hold balanced parentheses, which run: one-liners need."""
    depth, j = 0, i
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                break
        elif c == "\n" and text[j - 1:j] == "\n":
            return None
        j += 1
    else:
        return None
    if j + 1 >= len(text) or text[j + 1] != "(":
        return None
    label = text[i + 1:j]
    depth, k = 0, j + 1
    while k < len(text):
        c = text[k]
        if c == "\\" and k + 1 < len(text):
            k += 2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        elif c == "\n":
            return None
        k += 1
    else:
        return None
    target = text[j + 2:k].strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    return label, target, k + 1


def _link_span(label, target, style):
    label = _plain_inline(label).strip() or target
    scheme, _, rest = target.partition(":")
    scheme = scheme.lower().strip()
    if scheme == "open":
        return ("button", label, ("open", rest.strip()))
    if scheme == "run":
        lang, _, code = rest.strip().partition(" ")
        runner = RUNNERS.get(lang.lower())
        if runner and code.strip():
            return ("button", label, ("run", runner, code.strip()))
        return ("button", label, ("run", "powershell", rest.strip()))
    if scheme in ("send", "say", "ask"):
        return ("button", label, ("send", rest.strip() or label))
    if scheme == "copy":
        return ("button", label, ("copy", rest))
    return ("link", label, target, style)


def _emphasis(text, i, style, out, flush):
    """**bold**, *italic*, _italic_, ***both***, ~~struck~~. Returns where it
    ends, or None if the marker doesn't open anything."""
    for mark, adds in (("***", ("b", "i")), ("___", ("b", "i")), ("**", ("b",)), ("__", ("b",)),
                       ("~~", ("s",)), ("*", ("i",)), ("_", ("i",))):
        if not text.startswith(mark, i):
            continue
        start = i + len(mark)
        if start >= len(text) or text[start].isspace():
            return None
        if mark[0] == "_" and i > 0 and text[i - 1].isalnum():
            return None  # snake_case
        if mark[0] == "~" and len(mark) == 1:
            return None
        j = start
        while True:
            j = text.find(mark, j)
            if j == -1:
                return None
            ok = not text[j - 1].isspace()
            if mark[0] == "_" and j + len(mark) < len(text) and text[j + len(mark)].isalnum():
                ok = False
            # A lone * must not be the start of a ** closing something else.
            if len(mark) == 1 and text.startswith(mark * 2, j):
                ok = j > start and ok
                if ok and text.startswith(mark * 3, j):
                    ok = False
            if ok and j > start:
                break
            j += len(mark)
        flush()
        _inline(text[start:j], style | frozenset(adds), out)
        return j + len(mark)
    return None


def _balance(s):
    """Trailing ")" that doesn't belong to the URL or path: (see C:\\x)."""
    while s.endswith(")") and s.count(")") > s.count("("):
        s = s[:-1]
    return s


def _is_path(s):
    s = s.strip()
    return bool(s) and "\n" not in s and (
        bool(re.match(r"^[A-Za-z]:[\\/]", s)) or s.startswith("\\\\") or s.startswith("~\\")
        or s.startswith("%") and "%\\" in s)


def _plain_inline(text):
    return "".join(_span_text(s) for s in inline(text))


# -- plain text, for the overlay ---------------------------------------------
def plain(text):
    """The reply with its markup taken off, for places that can only show
    plain text: the corner overlay. Buttons read as their labels, math as a
    linear Unicode version of itself."""
    out = []
    for b in parse(text):
        kind = b[0]
        if kind == "para":
            out.append(_spans_text(b[1]))
        elif kind == "heading":
            out.append(_spans_text(b[2]))
        elif kind == "item":
            out.append("  " * b[1] + f"{b[2]} " + _spans_text(b[3]))
        elif kind == "quote":
            out.append("“" + _spans_text(b[1]) + "”")
        elif kind == "code":
            out.append(b[2])
        elif kind == "math":
            out.append(linear(b[1]))
        elif kind == "rule":
            out.append("—")
        elif kind == "table":
            rows = [b[2]] + b[3]
            out.append("\n".join("  ·  ".join(_spans_text(c) for c in row) for row in rows))
    return "\n".join(out)


def _spans_text(spans):
    return "".join(_span_text(s) for s in spans)


def _span_text(s):
    kind = s[0]
    if kind in ("text", "code", "link"):
        return s[1]
    if kind == "button":
        return f"[{s[1]}]"
    if kind == "math":
        return linear(s[1])
    if kind == "br":
        return "\n"
    return ""


# -- math --------------------------------------------------------------------
# math(tex) returns a tree the renderer lays out:
#   ("row", [nodes])
#   ("sym", text, italic)        a letter (italic), digit or punctuation
#   ("op", text)                 a relation or binary operator, spaced out
#   ("big", text)                ∑ ∏ ∫ — drawn larger, limits as scripts
#   ("text", text)               \text{…}, upright
#   ("scripts", base, sup, sub)  sup/sub may be None
#   ("frac", num, den)
#   ("sqrt", body, index)        index may be None
#   ("accent", body, mark)       mark: "bar" or a character drawn above
#   ("space", ems)

SYMBOLS = {
    # Greek
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ϵ", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "varpi": "ϖ", "rho": "ρ",
    "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "ϕ", "varphi": "φ",
    "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
    "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    # letter-likes and misc
    "infty": "∞", "partial": "∂", "nabla": "∇", "hbar": "ℏ", "ell": "ℓ", "emptyset": "∅",
    "varnothing": "∅", "forall": "∀", "exists": "∃", "neg": "¬", "lnot": "¬", "angle": "∠",
    "degree": "°", "prime": "′", "ldots": "…", "cdots": "⋯", "dots": "…", "vdots": "⋮",
    "ddots": "⋱", "therefore": "∴", "because": "∵", "Re": "ℜ", "Im": "ℑ", "aleph": "ℵ",
    "triangle": "△", "square": "□", "checkmark": "✓", "dagger": "†",
    "lbrace": "{", "rbrace": "}", "{": "{", "}": "}", "langle": "⟨", "rangle": "⟩",
    "lceil": "⌈", "rceil": "⌉", "lfloor": "⌊", "rfloor": "⌋", "|": "‖", "vert": "|",
    "Vert": "‖", "backslash": "\\", "%": "%", "$": "$", "#": "#", "&": "&", "_": "_",
}
OPERATORS = {
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓", "ast": "∗", "star": "⋆",
    "circ": "∘", "bullet": "•", "oplus": "⊕", "otimes": "⊗", "cup": "∪", "cap": "∩",
    "setminus": "∖", "wedge": "∧", "land": "∧", "vee": "∨", "lor": "∨",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈",
    "equiv": "≡", "sim": "∼", "simeq": "≃", "cong": "≅", "propto": "∝", "ll": "≪", "gg": "≫",
    "in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "supset": "⊃", "subseteq": "⊆",
    "supseteq": "⊇", "perp": "⊥", "parallel": "∥", "mid": "∣",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "gets": "←", "leftrightarrow": "↔",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔", "implies": "⇒", "iff": "⇔",
    "mapsto": "↦", "longrightarrow": "⟶", "uparrow": "↑", "downarrow": "↓",
}
BIG = {"sum": "∑", "prod": "∏", "coprod": "∐", "int": "∫", "iint": "∬", "iiint": "∭",
       "oint": "∮", "bigcup": "⋃", "bigcap": "⋂"}
FUNCTIONS = {"sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan", "sinh",
             "cosh", "tanh", "log", "ln", "lg", "exp", "lim", "max", "min", "sup", "inf", "det",
             "gcd", "deg", "dim", "ker", "arg", "mod", "Pr"}
ACCENTS = {"hat": "^", "widehat": "^", "tilde": "~", "widetilde": "~", "vec": "→",
           "dot": "˙", "ddot": "¨", "bar": "bar", "overline": "bar"}
BLACKBOARD = {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "P": "ℙ", "H": "ℍ"}
SPACES = {",": 0.17, ":": 0.22, ";": 0.28, " ": 0.25, "quad": 1.0, "qquad": 2.0, "!": -0.1}
_PLAIN_OPS = set("+-=<>*/")


def math(tex):
    """TeX to a layout tree. Unknown commands come out as their own names,
    so a typo shows as \\foo rather than vanishing."""
    tokens = _tokens(tex or "")
    pos = [0]
    return _row(tokens, pos, stop=None)


def _tokens(tex):
    out, i = [], 0
    while i < len(tex):
        c = tex[i]
        if c == "\\" and i + 1 < len(tex):
            if tex[i + 1].isalpha():
                j = i + 1
                while j < len(tex) and tex[j].isalpha():
                    j += 1
                out.append("\\" + tex[i + 1:j])
                i = j
            else:
                out.append("\\" + tex[i + 1])
                i += 2
            continue
        if c.isspace():
            i += 1
            continue
        out.append(c)
        i += 1
    return out


def _row(tokens, pos, stop):
    nodes = []
    while pos[0] < len(tokens):
        t = tokens[pos[0]]
        if t == stop or (stop == "}" and t == "\\right"):
            if t == stop:
                pos[0] += 1
            break
        if t in ("^", "_"):
            pos[0] += 1
            arg = _arg(tokens, pos)
            base = nodes.pop() if nodes else ("sym", "", False)
            if base[0] != "scripts":
                base = ("scripts", base, None, None)
            _, b, sup, sub = base
            nodes.append(("scripts", b, arg if t == "^" else sup, arg if t == "_" else sub))
            continue
        if t == "'":
            pos[0] += 1
            nodes.append(("sym", "′", False))
            continue
        nodes.append(_atom(tokens, pos))
    return ("row", nodes)


def _arg(tokens, pos):
    """One argument: a {group}, or a single token."""
    if pos[0] >= len(tokens):
        return ("row", [])
    if tokens[pos[0]] == "{":
        pos[0] += 1
        return _row(tokens, pos, "}")
    return _atom(tokens, pos)


def _raw_arg(tokens, pos):
    """A {group}'s tokens as plain text, for \\text and friends."""
    if pos[0] >= len(tokens):
        return ""
    if tokens[pos[0]] != "{":
        t = tokens[pos[0]]
        pos[0] += 1
        return t.lstrip("\\")
    depth, out = 0, []
    while pos[0] < len(tokens):
        t = tokens[pos[0]]
        pos[0] += 1
        if t == "{":
            depth += 1
            if depth == 1:
                continue
        elif t == "}":
            depth -= 1
            if depth == 0:
                break
        out.append(t)
    return " ".join(out).replace("\\ ", " ") if any(len(t) > 1 for t in out) else "".join(out)


def _atom(tokens, pos):
    t = tokens[pos[0]]
    pos[0] += 1
    if t == "{":
        return _row(tokens, pos, "}")
    if not t.startswith("\\"):
        if t.isalpha():
            return ("sym", t, True)
        if t in _PLAIN_OPS:
            return ("op", "−" if t == "-" else "∗" if t == "*" else t)
        if t in ",;":
            return ("row", [("sym", t, False), ("space", 0.17)])
        return ("sym", t, False)

    name = t[1:]
    if name in ("frac", "dfrac", "tfrac", "cfrac"):
        return ("frac", _arg(tokens, pos), _arg(tokens, pos))
    if name == "binom":
        top, bottom = _arg(tokens, pos), _arg(tokens, pos)
        return ("row", [("sym", "(", False), ("frac", top, bottom, False), ("sym", ")", False)])
    if name == "sqrt":
        index = None
        if pos[0] < len(tokens) and tokens[pos[0]] == "[":
            pos[0] += 1
            index = _row(tokens, pos, "]")
        return ("sqrt", _arg(tokens, pos), index)
    if name in ("text", "textrm", "mathrm", "operatorname", "textbf", "mathbf", "textit",
                "mbox", "mathit", "mathsf", "mathtt"):
        text = _raw_arg(tokens, pos)
        return ("text", text) if name != "mathit" else ("sym", text, True)
    if name == "mathbb":
        text = _raw_arg(tokens, pos)
        return ("sym", "".join(BLACKBOARD.get(c, c) for c in text), False)
    if name == "mathcal":
        return ("sym", _raw_arg(tokens, pos), True)
    if name in ACCENTS:
        return ("accent", _arg(tokens, pos), ACCENTS[name])
    if name in ("left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr"):
        if pos[0] < len(tokens):
            d = tokens[pos[0]]
            pos[0] += 1
            if d == ".":
                return ("row", [])
            return ("sym", SYMBOLS.get(d[1:], d.lstrip("\\")) if d.startswith("\\") else d, False)
        return ("row", [])
    if name in ("displaystyle", "textstyle", "limits", "nolimits"):
        return ("row", [])
    if name in SPACES:
        return ("space", SPACES[name])
    if name in BIG:
        return ("big", BIG[name])
    if name in OPERATORS:
        return ("op", OPERATORS[name])
    if name in SYMBOLS:
        sym = SYMBOLS[name]
        return ("sym", sym, name[0].islower() and len(sym) == 1 and sym.isalpha())
    if name in FUNCTIONS:
        return ("text", name)
    if name == "not" and pos[0] < len(tokens):
        nxt = _atom(tokens, pos)
        return (nxt[0], nxt[1] + "\u0338") if nxt[0] == "op" else nxt
    return ("text", t)  # unknown: show it as written


_SUP = str.maketrans("0123456789+-=()niax", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱᵃˣ")
_SUB = str.maketrans("0123456789+-=()aeiox", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑᵢₒₓ")


def linear(tex):
    """Math as one line of Unicode, for plain text: x² + y², (a)/(b), √(x)."""
    return _lin(math(tex)).strip()


def _lin(node):
    kind = node[0]
    if kind == "row":
        return "".join(_lin(n) for n in node[1])
    if kind in ("sym", "text"):
        return node[1]
    if kind == "op":
        return f" {node[1]} "
    if kind == "big":
        return node[1]
    if kind == "space":
        return " " if node[1] > 0.2 else ""
    if kind == "scripts":
        out = _lin(node[1])
        for part, table, mark in ((node[3], _SUB, "_"), (node[2], _SUP, "^")):
            if part is None:
                continue
            s = _lin(part).strip().replace(" ", "").replace("−", "-")
            if s and all(ord(c) in table for c in s):
                out += s.translate(table)
            else:
                out += f"{mark}({s})" if len(s) > 1 else f"{mark}{s}"
        return out
    if kind == "frac":
        a, b = _lin(node[1]).strip(), _lin(node[2]).strip()
        wrap = lambda s: s if len(s) <= 1 or s.isalnum() else f"({s})"  # noqa: E731
        return f"{wrap(a)}/{wrap(b)}"
    if kind == "sqrt":
        body = _lin(node[1]).strip()
        root = "√" if node[2] is None else _lin(node[2]).translate(_SUP) + "√"
        return root + (body if len(body) <= 1 else f"({body})")
    if kind == "accent":
        body = _lin(node[1])
        return body + ("\u0305" if node[2] == "bar" else "\u0302" if node[2] == "^" else "")
    return ""


# -- doing things ------------------------------------------------------------
def resolve(target):
    """A link's target as something to open: a URL, or an absolute path with
    ~ and %VARS% expanded and any :line suffix dropped. None if it's neither."""
    target = (target or "").strip().strip("<>").strip()
    if not target:
        return None
    if re.match(r"^(?:https?|mailto|ms-settings|file):", target, re.I):
        return target
    if target.lower().startswith("www."):
        return "https://" + target
    path = os.path.expandvars(os.path.expanduser(target.replace("/", os.sep)
                                                 if re.match(r"^[A-Za-z]:/", target) else target))
    m = re.match(r"^(.*?)(?::\d+(?::\d+)?)$", path)
    if m and not os.path.exists(path) and os.path.exists(m.group(1)):
        path = m.group(1)
    if os.path.isabs(path) or path.startswith("\\\\"):
        return path
    if re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", target, re.I):
        return "https://" + target  # example.com/page
    return None


def is_url(target):
    return bool(re.match(r"^[a-z][\w+.-]*:", target or "", re.I)) and not re.match(
        r"^[A-Za-z]:[\\/]", target)


def open_target(target):
    """Open a file, folder or web page. Returns None, or why it couldn't."""
    where = resolve(target)
    if where is None:
        return f"Don't know how to open {target!r}"
    if is_url(where):
        webbrowser.open(where)
        return None
    if not os.path.exists(where):
        return f"Not found: {where}"
    os.startfile(where)  # noqa: S606 — the whole point: open it with its app
    return None


def reveal(target):
    """Show a file selected in Explorer (a link's right-click)."""
    where = resolve(target)
    if where is None or is_url(where) or not os.path.exists(where):
        return open_target(target)
    subprocess.Popen(["explorer", "/select,", os.path.normpath(where)])
    return None


RUN_SECONDS = 120
RUN_OUTPUT_CHARS = 20000


def run(runner, code):
    """Start a piece of code on a click. Returns a dict that fills in as it
    runs — status "running", then "done" or "failed", with the output so far —
    which the Chat tab reads every frame."""
    state = {"status": "running", "runner": runner, "code": code, "output": "",
             "started": time.time(), "seconds": None, "exit": None}
    threading.Thread(target=_run, args=(state,), daemon=True).start()
    return state


def _command(runner, code):
    if runner == "python":
        exe = os.path.join(os.path.dirname(sys.executable), "python.exe")
        return [exe if os.path.exists(exe) else sys.executable, "-X", "utf8", "-c", code]
    if runner == "cmd":
        return ["cmd", "/d", "/c", code]
    prelude = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "
    return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-Command", prelude + code]


def _run(state):
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        proc = subprocess.Popen(
            _command(state["runner"], state["code"]), cwd=os.path.expanduser("~"), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        state["proc"] = proc
        timer = threading.Timer(RUN_SECONDS, proc.kill)
        timer.start()
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
                if len(state["output"]) < RUN_OUTPUT_CHARS:
                    state["output"] += line
            proc.wait()
        finally:
            timer.cancel()
        state["exit"] = proc.returncode
        if time.time() - state["started"] >= RUN_SECONDS - 0.5:
            state["output"] += f"\n(stopped after {RUN_SECONDS}s)"
        state["status"] = "done" if proc.returncode == 0 else "failed"
    except Exception as e:
        state["output"] += f"{type(e).__name__}: {e}"
        state["status"] = "failed"
    state["seconds"] = time.time() - state["started"]
    state.pop("proc", None)


if __name__ == "__main__":
    import json

    sample = " ".join(sys.argv[1:]) or (
        "**Bold** and *italic*, `C:\\Windows\\notepad.exe`, [site](https://example.com),\n"
        "[Open](open:C:\\Users) [Hi](run:python print('hi')) and $x^2 + \\frac{1}{2}$.")
    print(json.dumps(parse(sample), indent=1, ensure_ascii=False, default=list))
    print(plain(sample))
