"""The dashboard window: a side nav, and a Diagnostics page about the model.

It exists from the moment the app starts but sits minimized, so logging in
doesn't throw a window in your face — click its taskbar button (the banana) to
look at it, and closing it minimizes rather than quits. The assistant keeps
running either way.

Everything with a curve or a corner is rasterized in paint.py and placed as an
image; only text uses the canvas directly, since Tk draws that well and draws
everything else with stair-stepped edges. Panels are white on yellow with hard
black outlines and an offset black shadow, so the whole thing reads as cut
paper — which is also why the corner radii are large and consistent.

Numbers arrive from a polling thread and are eased toward on the way in, so a
value never snaps: gauges sweep, counters roll. Nothing repaints at all while
the window is minimized.
"""

import ctypes
import math
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

import agents
import config
import diagnostics
import endpoints
import icon
import paint

# -- palette -----------------------------------------------------------------
PAGE = "#fff3ce"      # the window behind everything
NAV = "#ffc93c"       # the side bar, same yellow as the orb
CARD = "#ffffff"
INK = "#101010"
MUTED = "#7d7059"
ACCENT = "#e8a317"    # gauge arcs and the sparkline, against white
LABEL = "#96660a"     # the same amber darkened until it's readable as text
TRACK = "#f0e2b6"     # the unfilled part of a gauge
GOOD = "#1f9254"
BAD = "#c0392b"

RADIUS = 22           # the corner radius everything shares
SHADOW = 5            # how far the hard black shadow sits behind a panel
OUTLINE = 2.4

PAGES = [("diagnostics", "Diagnostics"), ("agents", "Agents")]

STATUS_COLORS = {
    "idle": "#b9ac8e", "working": ACCENT, "waiting": "#d97a0b",
    "done": GOOD, "failed": BAD, "stopped": MUTED,
}


def _ease(t):
    return 1 - (1 - t) ** 3


class Dashboard:
    def __init__(self, root, app):
        self.app = app
        self.scale = getattr(app.ui, "scale", 1.0)
        self.page = PAGES[0][0]
        self.data = None
        self._images = {}     # canvas item -> PhotoImage, so Tk keeps the pixels
        self._sizes = {}      # canvas item -> the size it was last drawn at
        self._eased = {}      # animated values, keyed by name
        self._shown_at = 0.0
        self._hover = None
        self._layout_key = None
        self._has_icon = False

        self.win = tk.Toplevel(root)
        # Built out of sight: the window is assembled withdrawn and only ever
        # revealed as an icon, so logging in never flashes a panel on screen.
        self.win.withdraw()
        self.win.title("BananaGent")
        self.win.configure(bg=PAGE)
        self.win.protocol("WM_DELETE_WINDOW", self.hide)
        self.win.bind("<Configure>", lambda _e: self._relayout())
        self.win.bind("<Escape>", lambda _e: self.hide())
        # Restoring from the taskbar goes through Windows, not through show(),
        # so the arriving-on-screen work hangs off the map event instead.
        self.win.bind("<Map>", self._on_map)

        w, h = self._px(1020), self._px(710)
        self.win.minsize(self._px(880), self._px(640))
        self.win.geometry(f"{w}x{h}+{self._px(90)}+{self._px(70)}")

        self.canvas = tk.Canvas(self.win, bg=PAGE, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Motion>", self._on_move)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Leave>", lambda _e: self._set_hover(None))

        self._fonts()
        self._build()
        self._decorate()

        threading.Thread(target=self._poll, daemon=True).start()
        self.win.after(33, self._tick)
        self._park()

    def _park(self):
        """Leave it minimized on the taskbar, without ever showing it.

        A withdrawn window has no taskbar button at all, so it has to be mapped
        before it can be minimized. Both calls happen in one turn of the event
        loop with no redraw in between, so nothing reaches the screen.
        """
        self.win.deiconify()
        self.win.iconify()

    # -- window state --------------------------------------------------------
    def visible(self):
        return self.win.state() in ("normal", "zoomed")

    def show(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        return True

    def _on_map(self, _event=None):
        # The startup map is on its way to being minimized and lands here too,
        # by which point the window is already iconic — there is nothing to
        # dress up and nothing to animate.
        if not self.visible():
            return
        self._decorate()
        self._relayout(force=True)
        self._shown_at = time.perf_counter()  # replay the entrance

    def hide(self):
        self.win.iconify()
        return True

    def toggle(self):
        return self.hide() if self.visible() else self.show()

    def select(self, page):
        names = [p for p, _ in PAGES]
        if page not in names:
            raise ValueError(f"No page called '{page}'. Pages: {', '.join(names)}")
        self.page = page
        self._shown_at = time.perf_counter()
        return page

    def refresh(self):
        """Take a reading now instead of waiting for the next poll."""
        self.data = diagnostics.snapshot(self.app)
        return self.data

    # -- setup ---------------------------------------------------------------
    def _px(self, n):
        return max(1, int(round(n * self.scale)))

    def _fonts(self):
        def f(size, weight="normal", family="Segoe UI"):
            return tkfont.Font(family=family, size=-self._px(size), weight=weight)

        self.f_title = f(25, "bold")
        self.f_logo = f(17, "bold")
        self.f_nav = f(13, "bold")
        self.f_card = f(10, "bold")
        self.f_big = f(23, "bold")
        self.f_value = f(13, "bold")
        self.f_label = f(11)
        self.f_small = f(9, "bold")
        # Digits that change every frame need to be fixed-width or the text
        # jitters as it counts.
        self.f_num = f(15, "bold", "Consolas")
        self.f_gauge = f(19, "bold", "Consolas")
        self.f_mono = f(11, "bold", "Consolas")  # endpoint signatures

    def _decorate(self):
        """Paint the title bar to match, and give the window the banana.

        Re-applied every time the window is shown: Tk rebuilds the frame's
        window handle when it moves between withdrawn, iconic and normal, and
        the new one comes back with stock Windows colors.
        """
        try:
            if not self._has_icon:
                self.win.iconbitmap(icon.ensure_ico())
                self._has_icon = True
        except Exception:
            pass
        try:
            hwnd = ctypes.windll.user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()
            dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute

            def bgr(color):
                c = color.lstrip("#")
                return ctypes.c_int(int(c[4:6] + c[2:4] + c[0:2], 16))

            for attr, color in ((35, NAV), (36, INK), (34, INK)):  # caption, text, border
                dwm(hwnd, attr, ctypes.byref(bgr(color)), 4)
        except Exception:
            pass

    def _build(self):
        c = self.canvas
        self.nav_bg = c.create_image(0, 0, anchor="nw")
        self.logo = c.create_image(0, 0, anchor="nw")
        self.logo_text = c.create_text(0, 0, anchor="w", text="BananaGent",
                                       font=self.f_logo, fill=INK)
        self.nav_rule = c.create_image(0, 0, anchor="nw")

        self.nav_pill = c.create_image(0, 0, anchor="nw")
        self.nav_items = []
        for _, label in PAGES:
            self.nav_items.append(c.create_text(0, 0, anchor="w", text=label,
                                                font=self.f_nav, fill=INK))

        self.nav_hint = c.create_text(0, 0, anchor="nw", text="", font=self.f_label,
                                      fill=INK, justify="left")
        self.status_dot = c.create_image(0, 0, anchor="nw")
        self.status_text = c.create_text(0, 0, anchor="w", text="", font=self.f_small, fill=INK)

        self.title = c.create_text(0, 0, anchor="nw", text="Diagnostics",
                                   font=self.f_title, fill=INK)
        self.subtitle = c.create_text(0, 0, anchor="nw", text="", font=self.f_label, fill=MUTED)

        self.cards = {
            "model": Card(self, "THE MODEL", rows=5, hero=True),
            "residency": Card(self, "WHERE IT IS RUNNING", rows=0, gauges=2),
            "server": Card(self, "MODEL SERVER", rows=5),
            "audio": Card(self, "VOICE IN AND OUT", rows=5),
            "activity": Card(self, "THIS SESSION", rows=0, spark=True, stats=3),
        }
        self.rows = [AgentRow(self, a) for a in agents.all_agents()]

    # -- layout --------------------------------------------------------------
    def _relayout(self, force=False):
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if w < 50 or h < 50:
            return
        if not force and self._layout_key == (w, h):
            return
        self._layout_key = (w, h)

        m = self._px(18)
        nav_w = self._px(218)
        gap = self._px(16)

        self._panel(self.nav_bg, m, m, nav_w, h - 2 * m, NAV)

        logo_px = self._px(46)
        self._image(self.logo, icon.draw(logo_px))
        self.logo_x, self.logo_y = m + self._px(22), m + self._px(24)
        self.canvas.coords(self.logo_text, self.logo_x + logo_px + self._px(12),
                           self.logo_y + logo_px / 2)

        rule_y = m + self._px(96)
        self._rule(self.nav_rule, m + self._px(20), rule_y, nav_w - self._px(40))

        self.nav_geo = []
        item_h = self._px(46)
        for i, item in enumerate(self.nav_items):
            y = rule_y + self._px(22) + i * (item_h + self._px(8))
            x = m + self._px(14)
            self.nav_geo.append((x, y, nav_w - self._px(28), item_h))
            self.canvas.coords(item, x + self._px(44), y + item_h / 2)

        hint_y = h - m - self._px(96)
        self.canvas.coords(self.nav_hint, m + self._px(22), hint_y)
        self.canvas.itemconfig(
            self.nav_hint,
            text=f"Hold  {config.HOTKEY}  and talk.\nEsc hides this window.",
            width=nav_w - self._px(44))
        self.canvas.coords(self.status_dot, m + self._px(22), h - m - self._px(34))
        self.canvas.coords(self.status_text, m + self._px(44), h - m - self._px(26))

        x0 = m + nav_w + gap
        cw = w - x0 - m
        self.canvas.coords(self.title, x0 + self._px(4), m + self._px(2))
        self.canvas.coords(self.subtitle, x0 + self._px(6), m + self._px(40))

        top = m + self._px(72)
        col = (cw - gap) / 2
        rows = [self._px(206), self._px(186), self._px(156)]
        self.cards["model"].place(x0, top, col, rows[0])
        self.cards["residency"].place(x0 + col + gap, top, col, rows[0])
        y = top + rows[0] + gap
        self.cards["server"].place(x0, y, col, rows[1])
        self.cards["audio"].place(x0 + col + gap, y, col, rows[1])
        self.cards["activity"].place(x0, y + rows[1] + gap, cw, rows[2])

        self.content = (x0, top, cw, h - top - m)

    # -- image helpers -------------------------------------------------------
    def _image(self, item, rgba8):
        self._put(item, paint.photo(rgba8))

    def _sheet(self, item, sheet):
        self._put(item, sheet.photo())

    def _put(self, item, photo):
        self.canvas.itemconfig(item, image=photo)
        self._images[item] = photo  # Tk drops the pixels if nothing holds it

    def _panel(self, item, x, y, w, h, color, radius=None):
        """A rounded card: hard black shadow, fill, black outline."""
        w, h = int(w), int(h)
        if w < 2 or h < 2:
            return
        pad = self._px(SHADOW) + 3
        s = paint.Sheet(w + pad * 2, h + pad * 2)
        r = self._px(RADIUS if radius is None else radius)
        off = self._px(SHADOW)
        s.fill(paint.rounded_rect(s, pad + off, pad + off, w, h, r), INK)
        s.fill(paint.rounded_rect(s, pad, pad, w, h, r), color)
        s.stroke(paint.rounded_rect(s, pad, pad, w, h, r), INK, self._px(OUTLINE))
        self._sheet(item, s)
        self.canvas.coords(item, x - pad, y - pad)

    def _rule(self, item, x, y, w):
        h = self._px(3)
        s = paint.Sheet(int(w), h + 2)
        s.fill(paint.rounded_rect(s, 0, 1, int(w), h, h / 2), INK, alpha=0.30)
        self._sheet(item, s)
        self.canvas.coords(item, x, y)

    # -- animation -----------------------------------------------------------
    def _ease_to(self, key, target, rate=0.16):
        if target is None:
            return self._eased.get(key, 0.0)
        cur = self._eased.get(key)
        cur = target if cur is None else cur + (target - cur) * rate
        if abs(target - cur) < 1e-3:
            cur = target
        self._eased[key] = cur
        return cur

    def _tick(self):
        self.win.after(33, self._tick)
        if not self.visible():
            return
        now = time.perf_counter()
        self._relayout()
        self._draw_nav(now)
        self._draw_page(now)

    def _draw_nav(self, now):
        if not getattr(self, "nav_geo", None):
            return
        # The logo drifts as if it were floating in water.
        bob = math.sin(now * 1.7) * self._px(3)
        self.canvas.coords(self.logo, self.logo_x, self.logo_y + bob)

        # The pill slides to whichever page is selected rather than cutting.
        chosen = [p for p, _ in PAGES].index(self.page)
        slot = self._ease_to("pill_y", float(chosen), 0.22)
        x, y, w, h = self.nav_geo[0]
        pitch = (self.nav_geo[1][1] - y) if len(self.nav_geo) > 1 else 0
        y += slot * pitch

        lift = self._ease_to("pill", 1.0 if self._hover is not None else 0.0, 0.22)
        grow = self._px(3) * lift
        key = (int(w + 2 * grow), int(h))
        if self._sizes.get(self.nav_pill) != key:
            self._sizes[self.nav_pill] = key
            self._panel(self.nav_pill, x - grow, y, w + 2 * grow, h, CARD, radius=15)
        self.canvas.coords(self.nav_pill, x - grow - self._px(SHADOW) - 3,
                           y - self._px(SHADOW) - 3)
        self.canvas.tag_raise(self.nav_pill)
        for i, item in enumerate(self.nav_items):
            self.canvas.tag_raise(item)
            self.canvas.itemconfig(item, fill=INK if i == chosen else "#7a5f14")

        up = bool(self.data and self.data["server"]["up"])
        # A slow pulse while the server answers; a still dot when it doesn't.
        pulse = 0.5 + 0.5 * math.sin(now * 2.6) if up else 1.0
        r = self._px(6) + self._px(2) * pulse
        size = self._px(20)
        s = paint.Sheet(size, size)
        s.fill(paint.disc(s, size / 2, size / 2, r), GOOD if up else BAD)
        s.stroke(paint.disc(s, size / 2, size / 2, r), INK, self._px(1.8))
        self._sheet(self.status_dot, s)
        version = (self.data or {}).get("server", {}).get("version")
        self.canvas.itemconfig(
            self.status_text,
            text=f"ollama {version}" if up else "server not answering")

    def _draw_page(self, now):
        entry = _ease(min(1.0, (now - self._shown_at) / 0.55)) if self._shown_at else 1.0
        showing_agents = self.page == "agents"
        self.canvas.itemconfig(self.title, text="Agents" if showing_agents else "Diagnostics")

        for card in self.cards.values():
            card.set_visible(not showing_agents)
        for row in self.rows:
            row.set_visible(showing_agents)

        if showing_agents:
            self._draw_agents(now, entry)
        else:
            self._draw_diagnostics(now, entry)

    def _draw_agents(self, now, entry):
        busy = [a for a in agents.all_agents() if a.status in agents.LIVE]
        waiting = [a for a in agents.all_agents() if a.status == "waiting"]
        self.canvas.itemconfig(
            self.subtitle,
            text=(f"{len(waiting)} waiting for you" if waiting else
                  f"{len(busy)} working" if busy else
                  f"{len(self.rows)} agent{'' if len(self.rows) == 1 else 's'}, all idle"))

        x, y, w, _h = self.content
        gap = self._px(14)
        for i, row in enumerate(self.rows):
            y += row.draw(now, entry, i, x, y, w) + gap

    def _draw_diagnostics(self, now, entry):
        d = self.data
        if not d:
            self.canvas.itemconfig(self.subtitle, text="reading…")
            return

        model, loaded, server = d["model"], d["loaded"], d["server"]
        machine, app = d["machine"], d["app"]
        self.canvas.itemconfig(
            self.subtitle,
            text=f"{model['name']}  ·  {_dur(app['uptime_seconds'])} up  ·  "
                 f"{app['questions_answered']} answered")

        c = self.cards
        c["model"].update(entry, 0, hero=model["name"], rows=[
            ("parameters", _or(model["parameters"])),
            ("quantization", _or(model["quantization"])),
            ("context window", f"{model['context_tokens']:,} tokens"
             if model["context_tokens"] else "—"),
            ("size on disk", _gb(model["disk_gb"])),
            ("family", _or(model["family"])),
        ])

        on_gpu = loaded["on_gpu_percent"]
        c["residency"].update(entry, 1, gauges=[
            (self._ease_to("gpu", (on_gpu or 0) / 100.0), "ON GPU",
             f"{int(self._ease_to('gpu', (on_gpu or 0) / 100.0) * 100)}%" if loaded["resident"] else "—",
             ACCENT),
            (self._ease_to("ram", (machine["ram_used_percent"] or 0) / 100.0), "SYSTEM RAM",
             f"{int(self._ease_to('ram', (machine['ram_used_percent'] or 0) / 100.0) * 100)}%",
             GOOD if (machine["ram_used_percent"] or 0) < 90 else BAD),
        ], caption=(f"{_gb(loaded['vram_gb'])} of {_gb(loaded['size_gb'])} on "
                    f"{_card_name(machine['gpu'])}"
                    if loaded["resident"] else "the model is not loaded right now"))

        c["server"].update(entry, 2, rows=[
            ("address", server["host"].replace("http://", "")),
            ("version", _or(server["version"], "not answering")),
            ("which one", "pinned copy, 0.20.7" if server["pinned"] else "system install"),
            ("model loaded", "yes" if loaded["resident"] else "no"),
            ("unloads in", _dur(loaded["unloads_in_seconds"]) if loaded["resident"] else "—"),
        ])

        speech = d["speech"]
        c["audio"].update(entry, 3, rows=[
            ("microphone", _clip(_mic_name(app["microphone"]), 24)),
            ("push to talk", f"hold  {app['hotkey']}"),
            ("speaks as", speech["voice"] if speech["ready"] else "—"),
            ("speaking rate", f"{speech['speed']}x" if speech["ready"] else "—"),
            ("state", _voice_state(app, speech)),
        ])

        replies = app["recent_reply_seconds"]
        c["activity"].update(entry, 4, spark=replies, stats=[
            ("answered", str(app["questions_answered"])),
            ("last", f"{app['last_reply_seconds']:.1f}s" if app["last_reply_seconds"] else "—"),
            ("average", f"{sum(replies) / len(replies):.1f}s" if replies else "—"),
        ])

    # -- input ---------------------------------------------------------------
    def _hit(self, x, y):
        for i, (ix, iy, iw, ih) in enumerate(getattr(self, "nav_geo", [])):
            if ix <= x <= ix + iw and iy <= y <= iy + ih:
                return i
        return None

    def _set_hover(self, index):
        if index != self._hover:
            self._hover = index

    def _clickable(self, x, y):
        if self._hit(x, y) is not None:
            return True
        return self.page == "agents" and any(r.hit(x, y) for r in self.rows)

    def _on_move(self, event):
        self._set_hover(self._hit(event.x, event.y))
        self.canvas.configure(
            cursor="hand2" if self._clickable(event.x, event.y) else "")

    def _on_click(self, event):
        hit = self._hit(event.x, event.y)
        if hit is not None:
            return self.select(PAGES[hit][0])
        if self.page != "agents":
            return
        for row in self.rows:
            what = row.hit(event.x, event.y)
            if what == "header":
                return row.toggle()
            if what in ("approve", "deny"):
                # Answering the gate by hand, rather than by saying so.
                try:
                    return row.agent.answer(what == "approve")
                except Exception as e:
                    print(f"[dashboard] {e}")
                    return None

    # -- data ----------------------------------------------------------------
    def _poll(self):
        while True:
            try:
                self.data = diagnostics.snapshot(self.app)
            except Exception:
                pass
            # Nobody is looking at a minimized window, so back right off.
            time.sleep(1.5 if self.visible() else 10.0)


class AgentRow:
    """One agent on the Agents page: a header, and its endpoints on a dropdown.

    Clicking the header rolls the list of endpoints open. When the agent is
    waiting on an approval the header grows two buttons, so the gate can be
    answered by clicking as well as by voice.
    """

    HEADER = 96
    LINE = 25

    def __init__(self, dash, agent):
        self.d = dash
        self.agent = agent
        self.open = False
        self.box = (0, 0, 0, 0)
        self.header_box = (0, 0, 0, 0)
        self.buttons = {}          # name -> hit box, only while waiting
        self._drawn = None

        c = dash.canvas
        self.bg = c.create_image(0, 0, anchor="nw")
        self.dot = c.create_image(0, 0, anchor="nw")
        self.chevron = c.create_text(0, 0, anchor="center", text="▾",
                                     font=dash.f_value, fill=LABEL)
        self.title = c.create_text(0, 0, anchor="nw", text=agent.title,
                                   font=dash.f_value, fill=INK)
        self.status = c.create_text(0, 0, anchor="nw", text="", font=dash.f_small, fill=LABEL)
        self.task = c.create_text(0, 0, anchor="nw", text="", font=dash.f_label, fill=MUTED)

        self.eps = endpoints.by_agent(agent.name)
        self.lines = [(c.create_text(0, 0, anchor="nw", text="", font=dash.f_mono, fill=INK),
                       c.create_text(0, 0, anchor="nw", text="", font=dash.f_small, fill=LABEL),
                       c.create_text(0, 0, anchor="nw", text="", font=dash.f_label, fill=MUTED))
                      for _ in self.eps]
        self.approve = c.create_image(0, 0, anchor="nw")
        self.deny = c.create_image(0, 0, anchor="nw")
        self.approve_text = c.create_text(0, 0, anchor="center", text="Approve",
                                          font=dash.f_small, fill=INK)
        self.deny_text = c.create_text(0, 0, anchor="center", text="Deny",
                                       font=dash.f_small, fill=INK)

    def _items(self):
        out = [self.dot, self.chevron, self.title, self.status, self.task]
        out += [i for trio in self.lines for i in trio]
        return out

    def set_visible(self, on):
        state = "normal" if on else "hidden"
        self.d.canvas.itemconfig(self.bg, state=state)
        for item in self._items():
            self.d.canvas.itemconfig(item, state=state)
        if not on:
            for item in (self.approve, self.deny, self.approve_text, self.deny_text):
                self.d.canvas.itemconfig(item, state="hidden")
            self.buttons = {}

    def toggle(self):
        self.open = not self.open
        return self.open

    def height(self):
        d = self.d
        body = len(self.eps) * d._px(self.LINE) + d._px(22)
        return d._px(self.HEADER) + d._ease_to(f"open:{self.agent.name}",
                                               1.0 if self.open else 0.0, 0.20) * body

    def draw(self, now, entry, index, x, y, w):
        d, c = self.d, self.d.canvas
        state = self.agent.snapshot()
        h = self.height()

        lift = (1.0 - _ease(min(1.0, max(0.0, (entry - index * 0.08) / 0.7)))) * d._px(26)
        y += lift
        self.box = (x, y, w, h)
        self.header_box = (x, y, w, d._px(self.HEADER))

        # The panel is re-rasterized only when its size actually changes, which
        # during the roll-open is every few frames rather than every one.
        key = (int(w), int(h / 3) * 3)
        if self._drawn != key:
            self._drawn = key
            d._panel(self.bg, x, y, w, h, CARD)
        c.coords(self.bg, x - d._px(SHADOW) - 3, y - d._px(SHADOW) - 3)

        pad = d._px(20)
        colour = STATUS_COLORS.get(state["status"], MUTED)
        size = d._px(20)
        sheet = paint.Sheet(size, size)
        # A working agent's dot breathes; a stopped one sits still.
        r = d._px(6) + (d._px(2) * (0.5 + 0.5 * math.sin(now * 3.0))
                        if state["status"] in agents.LIVE else 0)
        sheet.fill(paint.disc(sheet, size / 2, size / 2, r), colour)
        sheet.stroke(paint.disc(sheet, size / 2, size / 2, r), INK, d._px(1.8))
        d._sheet(self.dot, sheet)
        c.coords(self.dot, x + pad, y + d._px(20))

        c.coords(self.title, x + pad + d._px(28), y + d._px(17))
        c.coords(self.status, x + pad + d._px(28), y + d._px(40))
        c.itemconfig(self.status, text=_status_line(state), fill=colour)
        c.coords(self.task, x + pad, y + d._px(62))
        c.itemconfig(self.task, text=_clip(_task_line(state), int(w / d._px(7))))

        c.coords(self.chevron, x + w - pad, y + d._px(28))
        c.itemconfig(self.chevron, text="▾" if self.open else "▸")

        self._draw_buttons(state, x, y, w)
        self._draw_endpoints(x, y, w)
        return h

    def _draw_buttons(self, state, x, y, w):
        d, c = self.d, self.d.canvas
        self.buttons = {}
        waiting = state.get("waiting_for")
        for item in (self.approve, self.deny, self.approve_text, self.deny_text):
            c.itemconfig(item, state="normal" if waiting else "hidden")
        if not waiting:
            return
        bw, bh = d._px(92), d._px(30)
        by = y + d._px(16)
        ax = x + w - d._px(20) - d._px(24) - bw * 2 - d._px(8)
        dx = ax + bw + d._px(8)
        for item, bx, colour, label in ((self.approve, ax, GOOD, self.approve_text),
                                        (self.deny, dx, "#f0b8b0", self.deny_text)):
            if self.d._sizes.get(item) != (bw, bh, colour):
                self.d._sizes[item] = (bw, bh, colour)
                d._panel(item, 0, 0, bw, bh, colour if colour != GOOD else "#8fe0b0",
                         radius=10)
            c.coords(item, bx - d._px(SHADOW) - 3, by - d._px(SHADOW) - 3)
            c.coords(label, bx + bw / 2, by + bh / 2)
            c.tag_raise(item)
            c.tag_raise(label)
        self.buttons = {"approve": (ax, by, bw, bh), "deny": (dx, by, bw, bh)}

    def _draw_endpoints(self, x, y, w):
        d, c = self.d, self.d.canvas
        shown = d._eased.get(f"open:{self.agent.name}", 0.0) > 0.02
        pad = d._px(20)
        top = y + d._px(self.HEADER) - d._px(6)
        for i, (ep, (name, tier, desc)) in enumerate(zip(self.eps, self.lines)):
            for item in (name, tier, desc):
                c.itemconfig(item, state="normal" if shown else "hidden")
            if not shown:
                continue
            ly = top + i * d._px(self.LINE)
            c.coords(name, x + pad, ly)
            c.itemconfig(name, text=ep.signature())
            c.coords(tier, x + pad + d._px(200), ly + d._px(2))
            c.itemconfig(tier, text="MAJOR" if ep.tier == "major" else "minor",
                         fill=BAD if ep.tier == "major" else LABEL)
            c.coords(desc, x + pad + d._px(258), ly + d._px(1))
            c.itemconfig(desc, text=_clip(_first_sentence(ep.description),
                                          max(10, int((w - d._px(300)) / d._px(6.2)))))

    def hit(self, px, py):
        """What was clicked: an approve/deny button, the header, or nothing."""
        for name, (bx, by, bw, bh) in self.buttons.items():
            if bx <= px <= bx + bw and by <= py <= by + bh:
                return name
        hx, hy, hw, hh = self.header_box
        if hx <= px <= hx + hw and hy <= py <= hy + hh:
            return "header"
        return None


class Card:
    """One white panel: a title, optional hero line, and rows of label/value."""

    def __init__(self, dash, title, rows=0, hero=False, gauges=0, spark=False, stats=0):
        self.d = dash
        c = dash.canvas
        self.bg = c.create_image(0, 0, anchor="nw")
        self.title = c.create_text(0, 0, anchor="nw", text=title,
                                   font=dash.f_card, fill=LABEL)
        self.hero = c.create_text(0, 0, anchor="nw", text="", font=dash.f_big, fill=INK) if hero else None
        self.rows = [(c.create_text(0, 0, anchor="nw", text="", font=dash.f_label, fill=MUTED),
                      c.create_text(0, 0, anchor="ne", text="", font=dash.f_value, fill=INK))
                     for _ in range(rows)]
        self.gauges = [(c.create_image(0, 0, anchor="nw"),
                        c.create_text(0, 0, anchor="center", text="", font=dash.f_gauge, fill=INK),
                        c.create_text(0, 0, anchor="center", text="", font=dash.f_small, fill=MUTED))
                       for _ in range(gauges)]
        self.caption = c.create_text(0, 0, anchor="center", text="",
                                     font=dash.f_label, fill=MUTED) if gauges else None
        self.spark = c.create_image(0, 0, anchor="nw") if spark else None
        self.stats = [(c.create_text(0, 0, anchor="nw", text="", font=dash.f_num, fill=INK),
                       c.create_text(0, 0, anchor="nw", text="", font=dash.f_small, fill=MUTED))
                      for _ in range(stats)]
        self.box = (0, 0, 0, 0)
        self._drawn = None

    def set_visible(self, on):
        self.d.canvas.itemconfig(self.bg, state="normal" if on else "hidden")
        if not on:
            for item in self._items():
                self.d.canvas.itemconfig(item, state="hidden")

    def place(self, x, y, w, h):
        self.box = (x, y, w, h)
        if self._drawn != (int(w), int(h)):
            self._drawn = (int(w), int(h))
            self.d._panel(self.bg, x, y, w, h, CARD)
        self._arrange(0.0)

    def _arrange(self, lift):
        d, c = self.d, self.d.canvas
        x, y, w, h = self.box
        y += lift
        pad = d._px(20)
        c.coords(self.bg, x - d._px(SHADOW) - 3, y - d._px(SHADOW) - 3)
        c.coords(self.title, x + pad, y + d._px(16))
        top = y + d._px(40)

        if self.hero is not None:
            c.coords(self.hero, x + pad, top)
            top += d._px(38)

        step = d._px(24)
        for i, (label, value) in enumerate(self.rows):
            c.coords(label, x + pad, top + i * step)
            c.coords(value, x + w - pad, top + i * step)

        if self.gauges:
            size = d._px(108)
            span = w / len(self.gauges)
            for i, (img, big, small) in enumerate(self.gauges):
                cx = x + span * (i + 0.5)
                c.coords(img, cx - size / 2, y + d._px(44))
                # The percentage sits in the hole; the name goes underneath,
                # clear of the ring rather than across it.
                c.coords(big, cx, y + d._px(44) + size / 2)
                c.coords(small, cx, y + d._px(44) + size + d._px(11))
            c.coords(self.caption, x + w / 2, y + h - d._px(22))

        if self.spark is not None:
            stat_w = d._px(104)
            block = x + w - pad - stat_w * len(self.stats)
            c.coords(self.spark, x + pad, y + d._px(50))
            for i, (num, label) in enumerate(self.stats):
                sx = block + i * stat_w
                c.coords(num, sx, y + d._px(56))
                c.coords(label, sx, y + d._px(82))

    def update(self, entry, index, hero=None, rows=(), gauges=(), caption=None,
               spark=None, stats=()):
        d, c = self.d, self.d.canvas
        # Cards arrive one after another rather than all at once.
        local = min(1.0, max(0.0, (entry - index * 0.07) / 0.72))
        self._arrange((1.0 - _ease(local)) * d._px(26))
        state = "normal" if local > 0.01 else "hidden"
        for item in self._items():
            c.itemconfig(item, state=state)

        if hero is not None:
            c.itemconfig(self.hero, text=hero)
        for (label_item, value_item), (label, value) in zip(self.rows, rows):
            c.itemconfig(label_item, text=label)
            c.itemconfig(value_item, text=value)
        for (img, big, small), (frac, name, text, color) in zip(self.gauges, gauges):
            self._gauge(img, frac, color)
            c.itemconfig(big, text=text)
            c.itemconfig(small, text=name)
        if caption is not None:
            c.itemconfig(self.caption, text=caption)
        if spark is not None:
            self._spark(spark)
        for (num, label), (name, value) in zip(self.stats, stats):
            c.itemconfig(num, text=value)
            c.itemconfig(label, text=name)

    def _items(self):
        out = [self.title]
        if self.hero is not None:
            out.append(self.hero)
        out += [i for pair in self.rows for i in pair]
        out += [i for trio in self.gauges for i in trio]
        out += [i for pair in self.stats for i in pair]
        if self.caption is not None:
            out.append(self.caption)
        if self.spark is not None:
            out.append(self.spark)
        return out

    def _gauge(self, item, frac, color):
        d = self.d
        size = d._px(112)
        s = paint.Sheet(size, size)
        c, r = size / 2, size * 0.40
        th = size * 0.15
        a0, sweep = math.radians(135), math.radians(270)
        s.fill(paint.arc(s, c, c, r, th, a0, a0 + sweep), TRACK)
        s.stroke(paint.arc(s, c, c, r, th, a0, a0 + sweep), INK, d._px(1.6), alpha=0.35)
        if frac > 0.004:
            lit = paint.arc(s, c, c, r, th, a0, a0 + sweep * min(1.0, frac))
            s.fill(lit, color)
            s.stroke(lit, INK, d._px(1.8))
        d._sheet(item, s)

    def _spark(self, values):
        """Recent reply times, most recent on the right."""
        d = self.d
        w = int(self.box[2] - d._px(104) * len(self.stats) - d._px(56))
        h = d._px(72)
        if w < d._px(60):
            w = d._px(60)

        key = (w, tuple(values[-40:]))
        if self.d._sizes.get(self.spark) == key:
            return
        self.d._sizes[self.spark] = key

        s = paint.Sheet(w, h)
        s.fill(paint.rounded_rect(s, 0, 0, w, h, d._px(12)), PAGE)
        s.stroke(paint.rounded_rect(s, 0, 0, w, h, d._px(12)), INK, d._px(1.6), alpha=0.35)
        pts = values[-40:]
        if len(pts) >= 2:
            top = max(pts) * 1.15 or 1.0
            pad = d._px(10)
            step = (w - 2 * pad) / (len(pts) - 1)
            line = [(pad + i * step, h - pad - (v / top) * (h - 2 * pad))
                    for i, v in enumerate(pts)]
            s.fill(paint.path(s, line, d._px(3.6)), ACCENT)
            s.fill(paint.disc(s, *line[-1], d._px(4.5)), INK)
        else:
            s.fill(paint.rounded_rect(s, w / 2 - d._px(50), h / 2 - d._px(1),
                                      d._px(100), d._px(2), 1), INK, alpha=0.20)
        d._sheet(self.spark, s)


# -- formatting --------------------------------------------------------------
def _or(value, fallback="—"):
    return str(value) if value else fallback


def _gb(value):
    return f"{value:.1f} GB" if value else "—"


def _clip(text, n):
    text = str(text)
    return text if len(text) <= n else text[:n - 1] + "…"


def _card_name(gpu):
    """'NVIDIA GeForce RTX 4070' is mostly branding; the model is the useful part."""
    if not gpu:
        return "the CPU"
    for noise in ("NVIDIA ", "GeForce ", "AMD ", "Radeon "):
        gpu = gpu.replace(noise, "")
    return _clip(gpu, 22)


def _first_sentence(text):
    head = str(text).split(". ")[0].rstrip(".")
    return head


def _status_line(state):
    status = state["status"]
    if status == "waiting":
        return "WAITING FOR YOU"
    if status == "working":
        running = state.get("running_for_seconds")
        return f"WORKING · STEP {state['steps']} · {_dur(running)}"
    if status == "failed":
        return "FAILED"
    if status == "done":
        return f"DONE · {state['steps']} STEPS"
    if status == "stopped":
        return "STOPPED"
    return "IDLE"


def _task_line(state):
    """The most useful single line about what this agent is up to."""
    waiting = state.get("waiting_for")
    if waiting:
        return waiting["question"]
    if state["status"] == "failed" and state.get("error"):
        return state["error"]
    if state["status"] == "done" and isinstance(state.get("result"), dict):
        return str(state["result"].get("answer") or "finished")
    if state.get("detail") and state["status"] != "idle":
        return state["detail"]
    return state.get("task") or state.get("url") or "nothing on the go"


def _voice_state(app, speech):
    if app["listening"]:
        return "listening"
    if speech.get("speaking"):
        return "speaking"
    if not speech.get("ready"):
        return "silent" if not speech.get("enabled") else "voice loading"
    return "idle" if speech["enabled"] else "muted"


def _mic_name(name):
    """Windows wraps device names in 'Microphone (…)'; the inside is the name."""
    if not name:
        return "system default"
    inner = re.match(r"^\s*(?:Microphone|Headset|Line In)\s*\((.+?)\)?\s*$", str(name))
    return inner.group(1) if inner else str(name)


def _dur(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
