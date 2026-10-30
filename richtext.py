"""Rich text on a Tk canvas: how the Chat tab draws a reply.

markup.py says what a reply means; this lays it out. A RichText owns every
canvas item for one reply bubble — words, code blocks, buttons, math — all
carrying one tag, so moving the whole reply as the chat scrolls is a single
canvas call. Nothing is an embedded widget: the Chat tab hides bubbles that
scroll under its title and text box by laying flat masks over them, and
widgets would draw on top of those.

Layout is cached per block. A reply streaming in only ever changes its last
block, so each frame re-renders that one block and slides the rest into
place instead of rebuilding the whole reply for every token.

Words are measured with the font (cached), wrapped greedily, and drawn one
canvas text item per run of same-styled words on a line. Buttons and math are
boxes with a width, an ascent and a descent, which sit on the line's baseline
like any word. Anything with a curve — button pills, code panels, the radical
sign — is rasterized by paint.py, same as the rest of the dashboard.
"""

import itertools
import re
import time
import tkinter.font as tkfont

import markup
import paint

# The dashboard's palette (dashboard.py), plus the few colours only rich text needs.
INK = "#101010"
MUTED = "#7d7059"
GOOD = "#1f9254"
BAD = "#c0392b"
BUTTON = "#ffc93c"        # the orb's yellow
LINK = "#8f5b00"          # amber, darkened to read as text and still look clickable
CODE_INK = "#4a3f2e"
CODE_BG = "#f8f1de"       # a code block's panel
CODE_EDGE = "#e0cf9f"
CHIP_BG = "#f3e8c8"       # behind `inline code`
RULE = "#e2d4ad"
QUOTE_BAR = "#e8a317"
TABLE_HEAD = "#fbf3dc"
OUT_BG = "#26221c"        # a run's output: a little terminal
OUT_INK = "#f1e8d0"

BODY = ("Segoe UI", 13, "")
CODE = ("Consolas", 12, "")
CHIP = ("Segoe UI", 12, "b")    # button labels
SMALL = ("Segoe UI", 10, "b")
HEADINGS = {1: 20, 2: 17, 3: 15, 4: 14, 5: 13, 6: 13}
MATH_SIZE = 15                  # Cambria reads small next to Segoe UI at the same size
ICONS = {"open": "↗", "run": "▶", "send": "➤", "copy": "⧉"}
OUTPUT_LINES = 40

_ids = itertools.count()


class Fonts:
    """Fonts by (family, size, flags), with their metrics and text widths
    cached — measuring is most of what laying out text costs. Flags: b bold,
    i italic, u underline, s struck through."""

    def __init__(self, px):
        self.px = px
        self._fonts, self._metrics, self._widths = {}, {}, {}

    def font(self, key):
        f = self._fonts.get(key)
        if f is None:
            family, size, flags = key
            f = self._fonts[key] = tkfont.Font(
                family=family, size=-self.px(size),
                weight="bold" if "b" in flags else "normal",
                slant="italic" if "i" in flags else "roman",
                underline="u" in flags, overstrike="s" in flags)
        return f

    def metrics(self, key):
        """(ascent, descent, linespace) in pixels."""
        m = self._metrics.get(key)
        if m is None:
            f = self.font(key)
            m = self._metrics[key] = (f.metrics("ascent"), f.metrics("descent"),
                                      f.metrics("linespace"))
        return m

    def width(self, key, text):
        k = (key, text)
        w = self._widths.get(k)
        if w is None:
            if len(self._widths) > 50000:
                self._widths.clear()
            w = self._widths[k] = self.font(key).measure(text)
        return w


def _flags(style):
    return "".join(f for f in "bis" if f in style)


def _clip(text, n):
    text = str(text)
    return text if len(text) <= n else text[:n - 1] + "…"


LIMIT_FUNCTIONS = {"lim", "max", "min", "sup", "inf", "det", "gcd", "Pr"}


def _is_function(node):
    """sin, log, log_2: a named function wants a space after it."""
    if node is None:
        return False
    if node[0] == "scripts":
        node = node[1]
    return node[0] == "text" and node[1] in markup.FUNCTIONS


def _dots(now):
    return "." * (1 + int(now * 3) % 3)


class RichText:
    """One reply's rich text. layout() it at a width, place() it, and ask
    hit() what's under the pointer; act() carries out a click."""

    def __init__(self, canvas, fonts, photos):
        self.c = canvas
        self.f = fonts
        self.px = fonts.px
        self._photos = photos     # shared image cache: key -> PhotoImage
        self.tag = f"rich{next(_ids)}"
        self.ox = self.oy = 0.0   # where the items currently sit
        self.size = (0, 0)
        self.runs = {}            # button key -> run state from markup.run(), or a copy flash
        self._state = "normal"
        self._key = None
        self._source = None
        self._blocks = []
        self._cache = []          # per block: {"sig", "tag", "y", "w", "h", "hits"}
        self._items = None        # the block being rendered: its tag and hits

    # -- the outside ---------------------------------------------------------
    def layout(self, text, width, now):
        """Lay the text out at this width; returns its (width, height). Cheap
        when nothing changed, which is most frames."""
        width = int(width)
        runsig = self._runsig(now)
        key = (text, width, runsig)
        if key == self._key:
            return self.size
        self._key = key
        if text != self._source:
            self._source = text
            self._blocks = markup.parse(text)

        y, used, cache = 0, 0, []
        prev = None
        for bi, block in enumerate(self._blocks):
            if prev is not None:
                y += self._gap(prev, block)
            sig = (block, width, tuple(r for r in runsig if r[0][0] == bi))
            old = self._cache[bi] if bi < len(self._cache) else None
            if old and old["sig"] == sig:
                if old["y"] != y:
                    self.c.move(old["tag"], 0, y - old["y"])
                    old["y"] = y
                entry = old
            else:
                btag = f"{self.tag}.{bi}"
                self.c.delete(btag)
                self._items = {"tag": btag, "hits": []}
                bx, by = self.ox, self.oy + y
                w, h = self._block(bi, block, bx, by, width)
                entry = {"sig": sig, "tag": btag, "y": y, "w": w, "h": h,
                         "hits": [(x0 - bx, y0 - by, x1 - bx, y1 - by, hit)
                                  for x0, y0, x1, y1, hit in self._items["hits"]]}
                if self._state != "normal":
                    self.c.itemconfig(btag, state=self._state)
            cache.append(entry)
            y += entry["h"]
            used = max(used, entry["w"])
            prev = block
        for old in self._cache[len(cache):]:
            self.c.delete(old["tag"])
        self._cache = cache
        self.size = (max(1, int(used)), max(1, int(y)))
        return self.size

    def place(self, x, y):
        if (x, y) != (self.ox, self.oy):
            self.c.move(self.tag, x - self.ox, y - self.oy)
            self.ox, self.oy = x, y

    def show(self, on):
        state = "normal" if on else "hidden"
        if state != self._state:
            self._state = state
            self.c.itemconfig(self.tag, state=state)

    def destroy(self):
        self.c.delete(self.tag)
        self._cache = []

    def hit(self, px, py):
        """(action, tip, rect) for what's under the pointer, or None."""
        if self._state != "normal":
            return None
        rx = px - self.ox
        for entry in self._cache:
            ry = py - self.oy - entry["y"]
            if ry < -4 or ry > entry["h"] + 4:
                continue
            for x0, y0, x1, y1, hit in entry["hits"]:
                if x0 <= rx <= x1 and y0 <= ry <= y1:
                    rect = (x0 + self.ox, y0 + self.oy + entry["y"],
                            x1 + self.ox, y1 + self.oy + entry["y"])
                    return hit[0], hit[1], rect
        return None

    def act(self, action):
        """Do what a click on this means. Returns a note to show if there's
        something to say (a file that isn't there), or ("send", text) for the
        caller to send as a message."""
        kind = action[0]
        if kind == "open":
            return markup.open_target(action[1])
        if kind == "run":
            _, runner, code, key = action
            old = self.runs.get(key)
            if old and old.get("status") == "running":
                return "Still running"
            self.runs[key] = markup.run(runner, code)
            return None
        if kind == "copy":
            _, text, key = action
            self.c.clipboard_clear()
            self.c.clipboard_append(text)
            self.runs[key] = {"status": "copied", "at": time.time()}
            return None
        if kind == "close":
            self.runs.pop(action[1], None)
            return None
        if kind == "send":
            return action
        return None

    def reveal(self, action):
        """A right-click: a file link shows the file in Explorer."""
        if action[0] == "open":
            return markup.reveal(action[1])
        return None

    def _runsig(self, now):
        """What about the runs changes how this reply looks, per button."""
        sig = []
        for key, st in self.runs.items():
            status = st.get("status")
            if status == "copied":
                if time.time() - st["at"] > 1.6:
                    continue
                sig.append((key, "copied"))
            elif status == "running":
                sig.append((key, "running", len(st["output"]), int(now * 3) % 3))
            else:
                sig.append((key, status, len(st["output"]), st.get("exit")))
        return tuple(sorted(sig, key=repr))

    # -- blocks --------------------------------------------------------------
    def _gap(self, prev, block):
        if block[0] == "heading":
            return self.px(14)
        if prev[0] == "heading":
            return self.px(4)
        if prev[0] == "item" and block[0] == "item":
            return self.px(3)
        return self.px(9)

    def _block(self, bi, block, x, y, width):
        """Draw one block with its top-left at (x, y). Returns (width, height)."""
        kind = block[0]
        if kind == "para":
            w, h = self._flow(block[1], x, y, width, BODY, INK, bi)
        elif kind == "heading":
            level = block[1]
            base = ("Segoe UI", HEADINGS[level], "b")
            w, h = self._flow(block[2], x, y, width, base, INK if level < 4 else MUTED, bi)
        elif kind == "item":
            _, depth, marker, spans = block
            indent = self.px(18) * depth
            gutter = max(self.px(18), self.f.width(BODY, marker) + self.px(8))
            asc = self.f.metrics(BODY)[0]
            baseline = y + self._first_ascent(spans, asc)
            self._text(x + indent, baseline - asc, marker,
                       BODY, INK if marker[0].isdigit() else LINK)
            w, h = self._flow(spans, x + indent + gutter, y, width - indent - gutter, BODY, INK, bi)
            w += indent + gutter
        elif kind == "quote":
            bar = self.px(3)
            w, h = self._flow(block[1], x + self.px(14), y, width - self.px(14),
                              ("Segoe UI", 13, "i"), CODE_INK, bi)
            self._rect(x, y, x + bar, y + h, QUOTE_BAR)
            w += self.px(14)
        elif kind == "code":
            return self._code(bi, block[1], block[2], x, y, width)
        elif kind == "math":
            box = self._math(block[1], True)
            bw, asc, desc, prims = box
            pad = self.px(4)
            left = x + max(0, (width - bw) / 2)
            self._prims(prims, left, y + pad + asc)
            return width, asc + desc + 2 * pad
        elif kind == "rule":
            self._rect(x, y + self.px(6), x + width, y + self.px(6) + max(1, self.px(1.5)), RULE)
            return width, self.px(13)
        elif kind == "table":
            return self._table(bi, block, x, y, width)
        else:
            return 0, 0
        return self._outputs(bi, x, y, w, h, width)

    def _outputs(self, bi, x, y, w, h, width):
        """What the block's inline run buttons printed, below it."""
        keys = sorted((k for k in self.runs if k[0] == bi and k[1] == "btn"
                       and self.runs[k].get("status") != "copied"), key=repr)
        for key in keys:
            h += self.px(8)
            h += self._output(self.runs[key], key, x, y + h, width)
            w = width
        return w, h

    def _code(self, bi, lang, code, x, y, width):
        runner = markup.RUNNERS.get(lang)
        pad, head = self.px(12), self.px(30)
        cw = max(1, self.f.width(CODE, "0"))
        cols = max(10, int((width - 2 * pad) // cw))
        lines = self._hard_wrap(code, cols)
        ls = self.f.metrics(CODE)[2]
        h = head + ls * max(1, len(lines)) + pad
        self._panel(x, y, width, h, CODE_BG, CODE_EDGE, self.px(10))
        self._text(x + pad, y + self.px(9), lang or "code", SMALL, MUTED)

        # The header's buttons, right to left: Run (if it can), then Copy.
        right = x + width - self.px(8)
        chips = []
        if runner:
            st = self.runs.get((bi, "code"))
            label = "Running" + _dots(time.time()) if st and st.get("status") == "running" else "Run"
            chips.append(("▶  " + label, ("run", runner, code, (bi, "code")),
                          f"Runs this {runner} on your computer"))
        copied = self.runs.get((bi, "copy"))
        fresh = copied and time.time() - copied.get("at", 0) < 1.6
        chips.append(("✓  Copied" if fresh else "⧉  Copy", ("copy", code, (bi, "copy")),
                      "Copies the code"))
        for label, action, tip in chips:
            w, hh = self._chip(label, right, y + self.px(5), SMALL, anchor="ne",
                               fill=BUTTON if action[0] == "run" else "#ffffff")
            self._hit(right - w, y + self.px(5), right, y + self.px(5) + hh, (action, tip))
            right -= w + self.px(8)

        self._text(x + pad, y + head, "\n".join(lines), CODE, CODE_INK)
        h_total = h
        st = self.runs.get((bi, "code"))
        if st and st.get("status") != "copied":
            h_total += self.px(8)
            h_total += self._output(st, (bi, "code"), x, y + h_total, width)
        return width, h_total

    def _output(self, st, key, x, y, width):
        """A run's output, as a small dark terminal with its status on top.
        Click its ✕ to put it away."""
        pad = self.px(10)
        status = st.get("status")
        if status == "running":
            head, colour = "Running" + _dots(time.time()), "#ffc93c"
        elif status == "done":
            head, colour = f"✓  Finished in {st.get('seconds') or 0:.1f}s", "#7ad39a"
        else:
            code = st.get("exit")
            head = f"✕  Failed{f' (exit {code})' if code is not None else ''}"
            colour = "#ff8a7a"
        body = st.get("output", "").rstrip("\n")
        if not body and status != "running":
            body = "(no output)"
        lines = body.split("\n") if body else []
        if len(lines) > OUTPUT_LINES:
            lines = [f"… {len(lines) - OUTPUT_LINES} earlier lines"] + lines[-OUTPUT_LINES:]
        cw = max(1, self.f.width(CODE, "0"))
        lines = self._hard_wrap("\n".join(lines), max(10, int((width - 2 * pad) // cw)))
        ls = self.f.metrics(CODE)[2]
        head_h = self.px(24)
        h = head_h + (ls * len(lines) + pad if lines else self.px(4))
        self._panel(x, y, width, h, OUT_BG, OUT_BG, self.px(10))
        self._text(x + pad, y + self.px(6), head, SMALL, colour)
        close = "✕"
        cx = x + width - pad - self.f.width(SMALL, close)
        self._text(cx, y + self.px(6), close, SMALL, "#b8ad94")
        self._hit(cx - self.px(6), y, x + width, y + head_h, (("close", key), "Close the output"))
        if lines:
            self._text(x + pad, y + head_h, "\n".join(lines), CODE, OUT_INK)
        return h

    def _table(self, bi, block, x, y, width):
        _, aligns, header, rows = block
        ncol = max([len(header)] + [len(r) for r in rows])
        pad_x, pad_y = self.px(10), self.px(6)

        def natural(spans, base):
            return sum(self._atom_width(a) for a in self._atoms(spans, base, INK, bi)
                       if a[0] != "br") + 2 * pad_x

        bold = ("Segoe UI", 13, "b")
        nat = [self.px(40)] * ncol
        for r, cells in enumerate([header] + rows):
            for ci, spans in enumerate(cells):
                nat[ci] = max(nat[ci], natural(spans, bold if r == 0 else BODY))
        total = sum(nat)
        if total > width:
            floor = [min(n, self.px(70)) for n in nat]
            spare = width - sum(floor)
            extra = [n - f for n, f in zip(nat, floor)]
            share = sum(extra) or 1
            cols = [f + max(0, spare) * e / share for f, e in zip(floor, extra)]
        else:
            cols = nat
        cols = [int(c) for c in cols]
        tw = sum(cols)

        cy = y
        line = max(1, self.px(1))
        for r, cells in enumerate([header] + rows):
            base = bold if r == 0 else BODY
            # The header's shading goes down first, sized once its cells are.
            shade = self._rect(x, cy, x, cy, TABLE_HEAD) if r == 0 else None
            cx, heights = x, []
            for ci in range(ncol):
                spans = cells[ci] if ci < len(cells) else []
                inner = cols[ci] - 2 * pad_x
                align = aligns[ci] if ci < len(aligns) else "left"
                # Measure first so right/centre alignment knows where to start.
                w_nat = natural(spans, base) - 2 * pad_x
                off = 0
                if align == "right":
                    off = max(0, inner - w_nat)
                elif align == "center":
                    off = max(0, (inner - w_nat) / 2)
                _w, h = self._flow(spans, cx + pad_x + off, cy + pad_y, inner - off, base, INK, bi)
                heights.append(h)
                cx += cols[ci]
            row_h = max(heights or [0]) + 2 * pad_y
            if shade is not None:
                self.c.coords(shade, x, cy, x + tw, cy + row_h)
            cy += row_h
            self._rect(x, cy - line, x + tw, cy, RULE if r else CODE_EDGE)
        self._rect(x, y, x + tw, y + line, CODE_EDGE)
        return tw, cy - y

    # -- inline flow ---------------------------------------------------------
    def _atoms(self, spans, base, colour, bi):
        """Spans to things to place on a line:
            ("t" | "sp", text, font, colour, background, hit)   a word or a space
            ("box", width, ascent, descent, draw, hit)          a button or math
            ("br",)
        """
        family, size, flags0 = base
        out, n_btn = [], 0
        for s in spans:
            kind = s[0]
            if kind == "br":
                out.append(("br",))
            elif kind == "text":
                self._words(s[1], (family, size, flags0 + _flags(s[2])), colour, None, None, out)
            elif kind == "code":
                target = s[2]
                hit = (("open", target), markup.resolve(target) or target) if target else None
                self._words(s[1], ("Consolas", size - 1, "u" if target else ""),
                            LINK if target else CODE_INK, CHIP_BG, hit, out, keep_spaces=True)
            elif kind == "link":
                target = s[2]
                where = markup.resolve(target)
                tip = where or target
                if where and not markup.is_url(where):
                    tip = f"{where}\nclick to open · right-click to show in Explorer"
                self._words(s[1], (family, size, flags0 + _flags(s[3]) + "u"), LINK, None,
                            (("open", target), tip), out)
            elif kind == "button":
                out.append(self._button(s[1], s[2], (bi, "btn", n_btn)))
                n_btn += 1
            elif kind == "math":
                w, asc, desc, prims = self._math(s[1], False, size)
                out.append(("box", w + self.px(2), asc, desc,
                            lambda x, bl, p=prims: self._prims(p, x + self.px(1), bl), None))
        return out

    def _words(self, text, font, colour, bg, hit, out, keep_spaces=False):
        for piece in re.findall(r"\S+|\s+", text):
            if piece.isspace():
                out.append(("sp", piece if keep_spaces else " ", font, colour, bg, hit))
            else:
                out.append(("t", piece, font, colour, bg, hit))

    def _button(self, label, action, key):
        kind = action[0]
        st = self.runs.get(key) or {}
        icon = ICONS.get(kind, "•")
        if kind == "run" and st.get("status") == "running":
            icon = "◌"
        if kind in ("copy",) and st.get("status") == "copied" and time.time() - st["at"] < 1.6:
            icon, label = "✓", "Copied"
        text = f"{icon}  {label}"
        asc, desc, _ = self.f.metrics(CHIP)
        pad_x, pad_y, shadow = self.px(10), self.px(3), self.px(2)
        w = self.f.width(CHIP, text) + 2 * pad_x
        h = asc + desc + 2 * pad_y

        if kind == "run":
            _, runner, code = action
            hit = (("run", runner, code, key), f"Runs {runner}:\n{_clip(code, 400)}")
        elif kind == "copy":
            hit = (("copy", action[1], key), "Copies:\n" + _clip(action[1], 200))
        elif kind == "send":
            hit = (("send", action[1]), f"Sends “{_clip(action[1], 200)}”")
        else:
            where = markup.resolve(action[1])
            hit = (("open", action[1]), f"Opens {where or action[1]}")

        def draw(x, baseline):
            top = baseline - asc - pad_y
            self._pill(x + self.px(1), top, w, h, BUTTON)
            self._text(x + self.px(1) + pad_x, top + pad_y, text, CHIP, INK)

        return ("box", w + self.px(2) + shadow, asc + pad_y + self.px(1),
                desc + pad_y + shadow + self.px(1), draw, hit)

    def _atom_width(self, a):
        if a[0] == "box":
            return a[1]
        return self.f.width(a[2], a[1])

    def _atom_vert(self, a):
        if a[0] == "box":
            return a[2], a[3]
        asc, desc, _ = self.f.metrics(a[2])
        return asc, desc

    def _first_ascent(self, spans, floor):
        """How far the first line's baseline sits below its top — so a list
        marker lines up with a first line that starts with a button."""
        asc = floor
        for a in self._atoms(spans, BODY, INK, -1):
            if a[0] == "br":
                break
            asc = max(asc, self._atom_vert(a)[0])
        return asc

    def _flow(self, spans, x0, y0, width, base, colour, bi):
        """Wrap spans into lines within `width`, drawing them from (x0, y0).
        Returns the (width, height) used."""
        width = max(self.px(20), width)
        lines, line, x = [], [], 0

        for a in self._atoms(spans, base, colour, bi):
            if a[0] == "br":
                lines.append(line)
                line, x = [], 0
                continue
            w = self._atom_width(a)
            if a[0] == "sp":
                if line and x + w <= width:
                    line.append((a, w))
                    x += w
                elif line:
                    lines.append(line)
                    line, x = [], 0
                continue
            if x + w > width and line:
                lines.append(line)
                line, x = [], 0
            if a[0] == "t" and w > width:
                # A word too long for any line — a URL, a path — breaks anywhere.
                rest = a[1]
                while rest:
                    n = self._fit(a[2], rest, width - x)
                    piece, rest = rest[:n], rest[n:]
                    pw = self.f.width(a[2], piece)
                    line.append((("t", piece) + a[2:], pw))
                    x += pw
                    if rest:
                        lines.append(line)
                        line, x = [], 0
                continue
            line.append((a, w))
            x += w
        lines.append(line)

        base_asc, base_desc, _ = self.f.metrics(base)
        lead = self.px(3)
        y, used = y0, 0
        for line in lines:
            while line and line[-1][0][0] == "sp":
                line.pop()
            asc, desc = base_asc, base_desc
            for a, _w in line:
                aa, dd = self._atom_vert(a)
                asc, desc = max(asc, aa), max(desc, dd)
            baseline = y + asc
            x, i = x0, 0
            while i < len(line):
                a, w = line[i]
                if a[0] == "box":
                    a[4](x, baseline)
                    if a[5]:
                        self._hit(x, baseline - a[2], x + w, baseline + a[3] - self.px(2), a[5])
                    x += w
                    i += 1
                    continue
                # One text item for the run of words that share a style.
                j, text, gw = i, "", 0
                while j < len(line) and line[j][0][0] != "box" and line[j][0][2:] == a[2:]:
                    text += line[j][0][1]
                    gw += line[j][1]
                    j += 1
                font, fill, bg, hit = a[2], a[3], a[4], a[5]
                fa, fd, _ = self.f.metrics(font)
                if bg:
                    self._round(x - self.px(3), baseline - fa, gw + self.px(6), fa + fd,
                                bg, self.px(4))
                self._text(x, baseline - fa, text, font, fill)
                if hit:
                    self._hit(x, baseline - fa, x + gw, baseline + fd, hit)
                x += gw
                i = j
            used = max(used, x - x0)
            y = baseline + desc + lead
        return used, y - y0 - lead

    def _fit(self, font, text, room):
        """How many characters of text fit in `room` (at least one)."""
        lo, hi = 1, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.f.width(font, text[:mid]) <= room:
                lo = mid
            else:
                hi = mid - 1
        return lo

    @staticmethod
    def _hard_wrap(text, cols):
        out = []
        for line in text.split("\n"):
            while len(line) > cols:
                out.append(line[:cols])
                line = line[cols:]
            out.append(line)
        return out

    # -- math ----------------------------------------------------------------
    # A box is (width, ascent, descent, prims); prims are drawn relative to
    # the box's left edge and baseline:
    #   ("t", dx, dy, text, font)       text with its baseline at dy
    #   ("r", x0, y0, x1, y1)           a filled rectangle — fraction bars
    #   ("img", dx, top, key)           a rasterized shape — the radical sign

    def _math(self, tex, display, size=None):
        try:
            tree = markup.math(tex)
        except Exception:
            tree = ("text", tex)
        scale = (size or 13) / 13
        self._msize0 = MATH_SIZE * scale * (1.1 if display else 1.0)
        return self._mbox(tree, 0, display)

    def _msize(self, level):
        return round(self._msize0 * (1.0, 0.72, 0.58)[min(level, 2)] * 2) / 2

    def _mbox(self, node, lv, disp):
        size = self._msize(lv)
        em = self.px(size)
        kind = node[0]
        if kind == "row":
            return self._mrow(node[1], lv, disp)
        if kind in ("sym", "text"):
            text = node[1]
            italic = kind == "sym" and node[2]
            fam = "Cambria" if (kind == "text" or text.isalnum() or italic) else "Cambria Math"
            font = (fam, size, "i" if italic else "")
            w = self.f.width(font, text)
            return (w, em * 0.72, em * 0.22, [("t", 0, 0, text, font)])
        if kind == "op":
            font = ("Cambria Math", size, "")
            return (self.f.width(font, node[1]), em * 0.6, em * 0.15, [("t", 0, 0, node[1], font)])
        if kind == "big":
            big = round(size * (1.55 if disp else 1.2) * 2) / 2
            font = ("Cambria Math", big, "")
            e2 = self.px(big)
            drop = e2 * 0.1  # sit it on the maths axis rather than the baseline
            return (self.f.width(font, node[1]) + em * 0.1, e2 * 0.75 - drop, e2 * 0.25 + drop,
                    [("t", 0, drop, node[1], font)])
        if kind == "space":
            return (em * node[1], 0, 0, [])
        if kind == "frac":
            sub = lv if disp and lv == 0 else min(lv + 1, 2)
            num, den = self._mbox(node[1], sub, False), self._mbox(node[2], sub, False)
            axis = em * 0.27
            t = max(1, round(em * 0.06))
            gap = max(1.0, em * 0.12)
            w = max(num[0], den[0]) + em * 0.25
            prims = []
            prims += self._shift(num[3], (w - num[0]) / 2, -(axis + t / 2 + gap + num[2]))
            prims += self._shift(den[3], (w - den[0]) / 2, -axis + t / 2 + gap + den[1])
            prims.append(("r", em * 0.06, -axis - t / 2, w - em * 0.06, -axis + t / 2))
            asc = axis + t / 2 + gap + num[2] + num[1]
            desc = den[1] + den[2] + gap + t / 2 - axis
            return (w + em * 0.08, asc, desc, self._shift(prims, em * 0.04, 0))
        if kind == "sqrt":
            body = self._mbox(node[1], lv, disp)
            t = max(1.0, em * 0.065)
            gap = em * 0.14
            rw = em * 0.62
            total = body[1] + gap + t + body[2]
            iw = rw + body[0] + em * 0.12
            key = ("radical", int(rw), int(iw), int(total), round(t, 1))
            prims = [("img", 0, -(body[1] + gap + t), key)]
            prims += self._shift(body[3], rw, 0)
            w, lead = iw, 0.0
            if node[2] is not None:
                idx = self._mbox(node[2], 2, False)
                lead = max(0.0, idx[0] - rw * 0.5)
                prims = self._shift(prims, lead, 0)
                prims += self._shift(idx[3], lead + rw * 0.45 - idx[0], -(total * 0.45))
            return (w + lead + em * 0.05, body[1] + gap + t, body[2], prims)
        if kind == "scripts":
            return self._mscripts(node, lv, disp, em)
        if kind == "accent":
            body = self._mbox(node[1], lv, disp)
            mark = node[2]
            if mark == "bar":
                t = max(1, round(em * 0.06))
                top = -(body[1] + em * 0.08)
                prims = body[3] + [("r", 0, top - t, body[0], top)]
                return (body[0], body[1] + em * 0.08 + t, body[2], prims)
            small = mark == "→"
            font = ("Cambria Math", self._msize(lv + 1) if small else size, "")
            mw = self.f.width(font, mark)
            lift = {"→": em * 0.05, "~": -em * 0.28, "^": -em * 0.3}.get(mark, -em * 0.35)
            prims = body[3] + [("t", (body[0] - mw) / 2, -(body[1] + lift), mark, font)]
            return (max(body[0], mw), body[1] + em * 0.3, body[2], prims)
        return (0, 0, 0, [])

    def _mrow(self, nodes, lv, disp):
        x, asc, desc, prims = 0.0, 0.0, 0.0, []
        em = self.px(self._msize(lv))
        space = em * (0.22 if lv == 0 else 0.12)
        prev = None
        for node in nodes:
            box = self._mbox(node, lv, disp)
            unary = node[0] == "op" and (prev is None or prev[0] in ("op", "big") or (
                prev[0] == "sym" and prev[1] in "([{,=" ))
            pad = space if node[0] == "op" and not unary else 0.0
            if node[0] == "text" and prev is not None and node[1].isalpha():
                pad = max(pad, em * 0.12)
            if _is_function(prev) and not (node[0] == "sym" and node[1] in "([|"):
                pad = max(pad, em * 0.17)  # sin x, not sinx
            x += pad
            prims += self._shift(box[3], x, 0)
            x += box[0] + (space if node[0] == "op" and not unary else 0.0)
            asc, desc = max(asc, box[1]), max(desc, box[2])
            prev = node
        return (x, asc, desc, prims)

    def _mscripts(self, node, lv, disp, em):
        _, base, sup, sub = node
        b = self._mbox(base, lv, disp)
        sl = min(lv + 1, 2)
        up_box = self._mbox(sup, sl, False) if sup is not None else None
        dn_box = self._mbox(sub, sl, False) if sub is not None else None
        prims = list(b[3])
        if disp and (base[0] == "big" and base[1] in "∑∏∐⋃⋂"
                     or base[0] == "text" and base[1] in LIMIT_FUNCTIONS):
            # Display sums and products take their limits above and below;
            # integrals keep them to the side, as they're written.
            w = max([b[0]] + [bx[0] for bx in (up_box, dn_box) if bx])
            prims = self._shift(prims, (w - b[0]) / 2, 0)
            asc, desc = b[1], b[2]
            if up_box:
                dy = -(b[1] + em * 0.12 + up_box[2])
                prims += self._shift(up_box[3], (w - up_box[0]) / 2, dy)
                asc = -dy + up_box[1]
            if dn_box:
                dy = b[2] + em * 0.12 + dn_box[1]
                prims += self._shift(dn_box[3], (w - dn_box[0]) / 2, dy)
                desc = dy + dn_box[2]
            return (w, asc, desc, prims)

        if base[0] == "big":
            # Scripts on an integral sit at its top and bottom ends.
            up = b[1] - (up_box[1] * 0.45 if up_box else 0)
            down = b[2] + (dn_box[1] * 0.15 if dn_box else 0)
        else:
            up = max(em * 0.38, b[1] - em * 0.5)
            down = max(em * 0.2, b[2] - em * 0.05)
        if up_box and dn_box:
            gap = (down - dn_box[1] * 0.8) - (-up + up_box[2])
            if gap < em * 0.12:
                down += em * 0.12 - gap
        x = b[0] + em * 0.03
        # A slanted letter leans into its superscript, and an integral's top
        # curls right: nudge the superscript clear. Subscripts tuck in.
        lean = em * (0.07 if base[0] == "sym" and base[2] else 0.1 if base[0] == "big" else 0)
        asc, desc, w = b[1], b[2], x
        if up_box:
            prims += self._shift(up_box[3], x + lean, -up)
            asc = max(asc, up + up_box[1])
            w = max(w, x + lean + up_box[0])
        if dn_box:
            prims += self._shift(dn_box[3], x, down)
            desc = max(desc, down + dn_box[2])
            w = max(w, x + dn_box[0])
        return (w + em * 0.02, asc, desc, prims)

    @staticmethod
    def _shift(prims, dx, dy):
        out = []
        for p in prims:
            if p[0] == "t":
                out.append(("t", p[1] + dx, p[2] + dy, p[3], p[4]))
            elif p[0] == "r":
                out.append(("r", p[1] + dx, p[2] + dy, p[3] + dx, p[4] + dy))
            else:
                out.append(("img", p[1] + dx, p[2] + dy, p[3]))
        return out

    def _prims(self, prims, x, baseline):
        for p in prims:
            if p[0] == "t":
                asc = self.f.metrics(p[4])[0]
                self._text(x + p[1], baseline + p[2] - asc, p[3], p[4], INK)
            elif p[0] == "r":
                self._rect(round(x + p[1]), round(baseline + p[2]),
                           round(x + p[3]), max(round(baseline + p[4]), round(baseline + p[2]) + 1), INK)
            else:
                self._image(x + p[1], baseline + p[2], p[3])

    # -- drawing primitives --------------------------------------------------
    def _tags(self):
        return (self.tag, self._items["tag"])

    def _text(self, x, y, text, font, fill):
        return self.c.create_text(round(x), round(y), anchor="nw", text=text,
                                  font=self.f.font(font), fill=fill, tags=self._tags())

    def _rect(self, x0, y0, x1, y1, fill):
        return self.c.create_rectangle(x0, y0, x1, y1, fill=fill, outline="", tags=self._tags())

    def _hit(self, x0, y0, x1, y1, hit):
        self._items["hits"].append((x0, y0, x1, y1, hit))

    def _image(self, x, y, key):
        return self.c.create_image(round(x), round(y), anchor="nw", image=self._photo(key),
                                   tags=self._tags())

    def _round(self, x, y, w, h, fill, r):
        self._image(x, y, ("round", int(w), int(h), fill, int(r)))

    def _panel(self, x, y, w, h, fill, edge, r):
        self._image(x, y, ("panel", int(w), int(h), fill, edge, int(r)))

    def _pill(self, x, y, w, h, fill):
        self._image(x, y, ("pill", int(w), int(h), fill))

    def _chip(self, label, x, y, font, anchor="nw", fill="#ffffff"):
        """A small button (a code block's Copy and Run). Returns its size."""
        asc, desc, _ = self.f.metrics(font)
        pad_x, pad_y = self.px(9), self.px(2)
        w = self.f.width(font, label) + 2 * pad_x
        h = asc + desc + 2 * pad_y
        left = x - w if anchor == "ne" else x
        self._image(left, y, ("chip", int(w), int(h), fill))
        self._text(left + pad_x, y + pad_y, label, font, INK)
        return w, h

    def _photo(self, key):
        """The PhotoImage for a shape, rasterized once per size and shared."""
        photo = self._photos.get(key)
        if photo is not None:
            return photo
        kind = key[0]
        px = self.px
        if kind == "round":
            _, w, h, fill, r = key
            s = paint.Sheet(max(2, w), max(2, h))
            s.fill(paint.rounded_rect(s, 0, 0, w, h, r), fill)
        elif kind == "panel":
            _, w, h, fill, edge, r = key
            s = paint.Sheet(max(2, w), max(2, h))
            shape = paint.rounded_rect(s, 0.5, 0.5, w - 1, h - 1, r)
            s.fill(shape, fill)
            if edge != fill:
                s.stroke(shape, edge, px(1.2))
        elif kind == "pill":
            # The dashboard's cut-paper look, small: hard shadow, fill, outline.
            _, w, h, fill = key
            shadow = px(2)
            s = paint.Sheet(w + shadow + 2, h + shadow + 2)
            r = h / 2
            s.fill(paint.rounded_rect(s, 1 + shadow, 1 + shadow, w, h, r), INK)
            s.fill(paint.rounded_rect(s, 1, 1, w, h, r), fill)
            s.stroke(paint.rounded_rect(s, 1, 1, w, h, r), INK, px(1.4))
        elif kind == "chip":
            _, w, h, fill = key
            s = paint.Sheet(max(2, w), max(2, h))
            shape = paint.rounded_rect(s, 1, 1, w - 2, h - 2, (h - 2) / 2)
            s.fill(shape, fill)
            s.stroke(shape, INK, px(1.1), alpha=0.55)
        elif kind == "radical":
            _, rw, w, h, t = key
            s = paint.Sheet(w + 2, h + 2)
            half = t / 2 + 0.5
            pts = [(0.8, h * 0.6), (rw * 0.3, h * 0.5), (rw * 0.62, h - half),
                   (rw - 0.5, half), (w, half)]
            s.fill(paint.path(s, pts, t), INK)
        else:
            raise ValueError(key)
        # Never evicted: a canvas item showing an image goes blank the moment
        # its PhotoImage is collected, and these are small and few.
        photo = self._photos[key] = s.photo()
        return photo
