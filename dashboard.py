"""The dashboard window: a side nav, a Diagnostics page, and the agents.

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
import json
import math
import os
import re
import shutil
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

import agents
import agents.browser
import business
import config
import diagnostics
import endpoints
import icon
import memory
import paint
import prompts
import richtext
import subagents

# -- palette -----------------------------------------------------------------
PAGE = "#fff3ce"      # the window behind everything
NAV = "#ffc93c"       # the side bar, same yellow as the orb
CARD = "#ffffff"
NOTICE_BG = "#f4ecd6"  # an agent reporting back, in the chat
INK = "#101010"
MUTED = "#7d7059"
ACCENT = "#e8a317"    # gauge arcs and the sparkline, against white
LABEL = "#96660a"     # the same amber darkened until it's readable as text
TRACK = "#f0e2b6"     # the unfilled part of a gauge
GOOD = "#1f9254"
BAD = "#c0392b"
CODE = "#5a4f3c"      # a tool call's arguments: darker than MUTED, quieter than INK

RADIUS = 22           # the corner radius everything shares
SHADOW = 5            # how far the hard black shadow sits behind a panel
OUTLINE = 2.4

PAGES = [("chat", "Chat"), ("diagnostics", "Diagnostics"), ("agents", "Agents"),
         ("prompts", "Prompts")]

STATUS_COLORS = {
    "idle": "#b9ac8e", "working": ACCENT, "waiting": "#d97a0b",
    "done": GOOD, "failed": BAD, "stopped": MUTED,
}


def _ease(t):
    return 1 - (1 - t) ** 3


def text_box(px, w, h, cap_w):
    """The white text-box panel with its yellow Enter key-cap.

    Shared by the overlay's ` box and the Chat tab so the two can't drift
    apart. Returns the sheet and how far the drawing is inset to leave room
    for the shadow, which is what to subtract when placing it.
    """
    pad = px(SHADOW) + 3
    off = px(SHADOW)
    r = px(14)
    s = paint.Sheet(w + pad * 2, h + pad * 2)
    body = paint.rounded_rect(s, pad, pad, w, h, r)
    s.fill(paint.rounded_rect(s, pad + off, pad + off, w, h, r), INK)
    s.fill(body, CARD)
    m = px(7)
    cap = paint.rounded_rect(s, pad + w - m - cap_w, pad + m, cap_w, h - 2 * m, px(8))
    s.fill(cap, NAV)
    s.stroke(cap, INK, px(1.8))
    s.stroke(body, INK, px(OUTLINE))
    return s, pad


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
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<Leave>", self._on_leave)
        # On the window, not the canvas: Tk on Windows delivers the wheel to
        # whatever has focus, which on the Chat tab is its text box.
        self.win.bind("<MouseWheel>", self._on_wheel)

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
        self._focus_page()
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
        self._focus_page()

    def _focus_page(self):
        """Opening onto the Chat tab means you're about to type, so be ready."""
        if self.page == "chat":
            self.chat_page.focus()
        elif self.page == "prompts":
            self.prompts_page.focus()

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
        self._focus_page()
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
        self.f_chat = f(13)                           # message bubbles
        self.f_input = f(14, "normal", "Segoe UI Semibold")  # what you're typing
        # A tool call's line in the chat, and its arguments when opened.
        self.f_tool = tkfont.Font(family="Segoe UI", size=-self._px(12), slant="italic")
        self.f_code = f(11, "normal", "Consolas")
        self.f_tab = f(12, "bold")
        # The Prompts tab's editor, and its notes and headings.
        self.f_edit = f(13, "normal", "Consolas")
        self.f_edit_bold = f(13, "bold", "Consolas")
        self.f_edit_note = tkfont.Font(family="Consolas", size=-self._px(13), slant="italic")

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
        self.restart_bg = c.create_image(0, 0, anchor="nw")
        self.restart_text = c.create_text(0, 0, anchor="center", text="↻  Restart",
                                          font=self.f_value, fill=INK)
        self.restart_box = (0, 0, 0, 0)
        self._restarting = False
        self.status_dot = c.create_image(0, 0, anchor="nw")
        self.status_text = c.create_text(0, 0, anchor="w", text="", font=self.f_small, fill=INK)

        self.title = c.create_text(0, 0, anchor="nw", text="Diagnostics",
                                   font=self.f_title, fill=INK)
        self.subtitle = c.create_text(0, 0, anchor="nw", text="", font=self.f_label, fill=MUTED)

        self.cards = {
            "model": Card(self, "THE MODEL", rows=5, hero=True),
            "residency": Card(self, "WHERE IT IS RUNNING", rows=0, gauges=2),
            "server": Card(self, "MODEL SERVER", rows=5),
            "box": Card(self, "CONVERSATION AND MEMORY", rows=5),
            "activity": Card(self, "THIS SESSION", rows=0, spark=True, stats=3),
        }
        self.rows = [AgentRow(self, a) for a in agents.all_agents()]
        self.chat_page = ChatPage(self)
        self.prompts_page = PromptsPage(self)

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
        # Restart sits just above the hint, the width of the page buttons.
        rx, rh = m + self._px(20), self._px(38)
        rw, ry = nav_w - self._px(40), hint_y - rh - self._px(22)
        self._panel(self.restart_bg, rx, ry, rw, rh, CARD, radius=12)
        self.canvas.coords(self.restart_text, rx + rw / 2, ry + rh / 2)
        self.restart_box = (rx, ry, rw, rh)
        self.canvas.coords(self.nav_hint, m + self._px(22), hint_y)
        self.canvas.itemconfig(
            self.nav_hint,
            text=f"Press  {config.HOTKEY}  and type.\nEsc hides this window.",
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
        self.cards["box"].place(x0 + col + gap, y, col, rows[1])
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
        self.canvas.itemconfig(self.title, text=dict(PAGES)[self.page])

        for card in self.cards.values():
            card.set_visible(self.page == "diagnostics")
        for row in self.rows:
            row.set_visible(self.page == "agents")
        self.chat_page.set_visible(self.page == "chat")
        self.prompts_page.set_visible(self.page == "prompts")

        if self.page == "chat":
            self._draw_chat(now)
        elif self.page == "prompts":
            self.prompts_page.draw(now)
        elif self.page == "agents":
            self._draw_agents(now, entry)
        else:
            self._draw_diagnostics(now, entry)

    def _draw_chat(self, now):
        sent = sum(1 for m in self.app.chat if m["role"] == "you")
        self.canvas.itemconfig(
            self.subtitle,
            text=f"{sent} message{'' if sent == 1 else 's'}  ·  "
                 f"the same conversation as the  {config.HOTKEY}  box")
        self.chat_page.draw(now)

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
            (self._ease_to("ram", (machine["ram_used_percent"] or 0) / 100.0), "THIS PC'S RAM",
             f"{int(self._ease_to('ram', (machine['ram_used_percent'] or 0) / 100.0) * 100)}%",
             GOOD if (machine["ram_used_percent"] or 0) < 90 else BAD),
        ], caption=(f"{_gb(loaded['vram_gb'])} of {_gb(loaded['size_gb'])} on the DGX Spark's GPU"
                    if loaded["resident"] else "the model is not loaded right now"))

        c["server"].update(entry, 2, rows=[
            ("address", server["host"].replace("http://", "")),
            ("version", _or(server["version"], "not answering")),
            ("machine", "DGX Spark"),
            ("model loaded", "yes" if loaded["resident"] else "no"),
            ("unloads in", _unloads(loaded)),
        ])

        c["box"].update(entry, 3, rows=[
            ("open with", f"press  {app['hotkey']}"),
            ("state", "open" if app["box_open"] else "put away"),
            ("last message", _clip(app["last_message"] or "—", 24)),
            ("short-term", f"{app['short_term_tokens']:,} of {config.SHORT_TERM_TOKENS:,} tokens"
             if app["short_term_tokens"] is not None else "—"),
            ("long-term", f"{app['long_term_facts']} saved facts"
             if app["long_term_facts"] is not None else "—"),
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

    def _hit_restart(self, x, y):
        rx, ry, rw, rh = self.restart_box
        return rx <= x <= rx + rw and ry <= y <= ry + rh

    def restart(self):
        """The nav's Restart button: say so, then let the app quit and relaunch."""
        if self._restarting:
            return
        self._restarting = True
        self.canvas.itemconfig(self.restart_text, text="Restarting…")
        # A moment for the label to reach the screen before the window goes.
        self.win.after(150, self.app.restart)

    def _clickable(self, x, y):
        if self._hit(x, y) is not None or self._hit_restart(x, y):
            return True
        if self.page == "chat":
            return (self.chat_page.hit_send(x, y) or self.chat_page.hit_tool(x, y) is not None
                    or self.chat_page.hit_rich(x, y) is not None)
        if self.page == "prompts":
            return self.prompts_page.hit(x, y) is not None
        return self.page == "agents" and any(r.hit(x, y) for r in self.rows)

    def _on_wheel(self, event):
        if self.page == "chat":
            self.chat_page.wheel(event.delta)
        elif self.page == "prompts":
            self.prompts_page.wheel(event.delta)

    def _on_move(self, event):
        self._set_hover(self._hit(event.x, event.y))
        if self.page == "chat":
            self.chat_page.pointer = (event.x, event.y)
        self.canvas.configure(
            cursor="hand2" if self._clickable(event.x, event.y) else "")

    def _on_leave(self, _event=None):
        self._set_hover(None)
        self.chat_page.pointer = None

    def _on_right_click(self, event):
        if self.page == "chat":
            self.chat_page.right_click(event.x, event.y)

    def _on_click(self, event):
        hit = self._hit(event.x, event.y)
        if hit is not None:
            return self.select(PAGES[hit][0])
        if self._hit_restart(event.x, event.y):
            return self.restart()
        if self.page == "chat":
            # The key-cap sends, a tool line opens or closes its arguments, a
            # link or button in a reply does its thing, and a click anywhere
            # else on the page just puts you back in the box.
            if self.chat_page.hit_send(event.x, event.y):
                return self.chat_page.send()
            if self.chat_page.click_rich(event.x, event.y):
                return self.chat_page.focus()
            tool = self.chat_page.hit_tool(event.x, event.y)
            if tool is not None:
                self.chat_page.toggle_tool(tool)
            return self.chat_page.focus()
        if self.page == "prompts":
            return self.prompts_page.click(event.x, event.y)
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


class ChatPage:
    """The Chat tab: the conversation so far, and a box to add to it.

    It's the same conversation as the ` box — both go through App.send() — so
    a message typed in the corner shows up here too, with its reply. Newest is
    at the bottom and the wheel scrolls back. Tk's canvas can't clip, so two
    flat page-coloured masks hide bubbles as they scroll up under the title or
    down under the box.
    """

    BOX_H = 48      # the text box
    CAP = 34        # its Enter key-cap, which is also the send button
    GAP = 14        # between bubbles
    PAD_X, PAD_Y = 16, 10

    def __init__(self, dash):
        self.d = dash
        c = dash.canvas
        self._items = {}      # block key -> its canvas items, made as blocks first appear
        self._measured = {}   # canvas text item -> (text, wrap, width, height)
        self._tool_hits = []  # (x0, y0, x1, y1, part) for each tool line on screen
        self._rich = []       # the replies on screen, as RichText, for their links and buttons
        self.fonts = richtext.Fonts(dash._px)
        self._photos = {}     # button pills and code panels, shared by every reply
        self.pointer = None   # where the mouse is, for the tooltip
        self._note = None     # (text, x, y, until): something a click has to say
        self.scroll = 0.0     # pixels scrolled back from the newest message
        self._seen = 0        # entries last frame, to notice new ones arriving
        self._panel_size = None
        self._pad = 0
        self.send_box = (0, 0, 0, 0)

        self.empty = c.create_text(
            0, 0, anchor="center", justify="center", font=dash.f_label, fill=MUTED,
            text=f"Nothing said yet.\nType below, or press  {config.HOTKEY}  anywhere "
                 f"— it's the same conversation.")
        self.mask_top = c.create_rectangle(0, 0, 0, 0, fill=PAGE, outline="")
        self.mask_bottom = c.create_rectangle(0, 0, 0, 0, fill=PAGE, outline="")
        self.panel = c.create_image(0, 0, anchor="nw")
        self.cap = c.create_text(0, 0, anchor="center", text="↵", font=dash.f_value, fill=INK)

        self.var = tk.StringVar()
        self.entry = tk.Entry(
            c, textvariable=self.var, font=dash.f_input,
            bg=CARD, fg=INK, insertbackground=INK, insertwidth=dash._px(2),
            relief="flat", bd=0, highlightthickness=0,
            selectbackground=NAV, selectforeground=INK)
        self.entry.bind("<Return>", lambda _e: self.send() or "break")
        self.entry_item = c.create_window(0, 0, anchor="w", window=self.entry)

        # Placeholder laid over the empty box rather than typed into it, same
        # as the overlay's, so it can never be sent.
        self.hint = tk.Label(self.entry, text="Message BananaGent…", font=dash.f_input,
                             bg=CARD, fg=MUTED, bd=0, padx=0, pady=0)
        self.hint.bind("<Button-1>", lambda _e: self.focus())
        self.var.trace_add("write", lambda *_: self._update_hint())
        self._update_hint()

        # What a link or button does, shown while the pointer is over it.
        self.tip_bg = c.create_image(0, 0, anchor="nw", state="hidden")
        self.tip = c.create_text(0, 0, anchor="nw", font=dash.f_code, fill=CARD,
                                 state="hidden")
        self._tip_size = None

    # -- input ----------------------------------------------------------------
    def send(self):
        text = self.var.get().strip()
        if text:
            self.var.set("")
            self.d.app.send(text, "chat")
        self.focus()

    def focus(self):
        self.entry.focus_set()

    def wheel(self, delta):
        self.scroll += delta / 120 * self.d._px(60)

    def hit_send(self, px, py):
        x, y, w, h = self.send_box
        return x <= px <= x + w and y <= py <= y + h

    def hit_tool(self, px, py):
        """The tool call whose line is under the pointer, if any."""
        for x0, y0, x1, y1, part in self._tool_hits:
            if x0 <= px <= x1 and y0 <= py <= y1:
                return part
        return None

    def toggle_tool(self, part):
        part["open"] = not part.get("open")

    def hit_rich(self, px, py):
        """The link or button in a reply under the pointer: (view, action, tip, rect)."""
        for view in self._rich:
            got = view.hit(px, py)
            if got:
                return (view,) + got
        return None

    def click_rich(self, px, py):
        """Do what a click on a link or button means. False if there wasn't one."""
        got = self.hit_rich(px, py)
        if not got:
            return False
        view, action, _tip, rect = got
        try:
            said = view.act(action)
        except Exception as e:
            said = f"Couldn't do that: {e}"
        if isinstance(said, tuple) and said[0] == "send":
            self.d.app.send(said[1], "chat")
        elif said:
            self._note = (said, rect[0], rect[3], time.time() + 3.0)
        return True

    def right_click(self, px, py):
        """A file link's right-click shows it in Explorer."""
        got = self.hit_rich(px, py)
        if not got:
            return
        view, action, _tip, rect = got
        try:
            said = view.reveal(action)
        except Exception as e:
            said = f"Couldn't do that: {e}"
        if said:
            self._note = (said, rect[0], rect[3], time.time() + 3.0)

    def _update_hint(self):
        if self.var.get():
            self.hint.place_forget()
        else:
            self.hint.place(x=self.d._px(5), rely=0.5, anchor="w")  # clear of the caret

    # -- drawing --------------------------------------------------------------
    def set_visible(self, on):
        c = self.d.canvas
        state = "normal" if on else "hidden"
        for item in (self.mask_top, self.mask_bottom, self.panel, self.cap, self.entry_item):
            c.itemconfig(item, state=state)
        if not on:
            c.itemconfig(self.empty, state="hidden")
            for items in self._items.values():
                for item in items:
                    if isinstance(item, richtext.RichText):
                        item.show(False)
                    else:
                        c.itemconfig(item, state="hidden")
            for item in (self.tip, self.tip_bg):
                c.itemconfig(item, state="hidden")
            self._tool_hits, self._rich = [], []

    def _blocks(self, chat, now):
        """The conversation as things to draw, oldest first.

        A message is one bubble. A reply is its parts in the order they
        happened — a bubble per stretch of text, a grey line per tool call —
        plus dots while it's waiting on the model. Keys stay stable while a
        reply streams, so each block keeps its canvas items.
        """
        for i, msg in enumerate(chat):
            if msg["role"] == "you":
                yield ("you", i), "bubble", (msg["text"], True, NAV)
                continue
            if msg["role"] == "notice":
                # An agent reporting back — the model's reply follows it.
                yield ("notice", i), "bubble", ("↳ " + msg["text"], False, NOTICE_BG)
                continue
            if msg.get("error"):
                yield ("error", i), "bubble", (msg["text"], False, "#ffd5cf")
                continue
            parts = msg.get("parts") or []
            texts = [j for j, p in enumerate(parts) if p["kind"] == "text" and p["text"].strip()]
            for j, part in enumerate(parts):
                if part["kind"] == "tool":
                    yield ("tool", i, j), "tool", part
                elif part["text"].strip():
                    text = part["text"].strip()
                    if msg.get("cut") and j == texts[-1]:
                        text += " …"
                    # Replies are markdown, with links, buttons and math in them.
                    yield ("text", i, j), "rich", (text, False, CARD)
            idle = not parts or (parts[-1]["kind"] == "tool" and parts[-1]["status"] != "running")
            if not msg.get("done") and idle:
                yield ("dots", i), "bubble", ("•" * (1 + int(now * 3) % 3), False, CARD)
            elif msg.get("cut") and not texts:
                yield ("cut", i), "bubble", ("(cut off)", False, CARD)

    def _get(self, key, kind):
        """A block's canvas items, made the first time it's drawn."""
        if key not in self._items:
            d, c = self.d, self.d.canvas
            if kind == "bubble":
                self._items[key] = (c.create_image(0, 0, anchor="nw"),
                                    c.create_text(0, 0, anchor="nw", font=d.f_chat, fill=INK))
            elif kind == "rich":
                self._items[key] = (c.create_image(0, 0, anchor="nw"),
                                    richtext.RichText(c, self.fonts, self._photos))
            else:
                # The line, the opened box with its arguments and the run's
                # activity, and the status pill beside the line.
                self._items[key] = (c.create_text(0, 0, anchor="nw", font=d.f_tool, fill=MUTED),
                                    c.create_image(0, 0, anchor="nw"),
                                    c.create_text(0, 0, anchor="nw", font=d.f_code, fill=CODE),
                                    c.create_text(0, 0, anchor="nw", font=d.f_code, fill=INK),
                                    c.create_image(0, 0, anchor="nw"),
                                    c.create_text(0, 0, anchor="w", font=d.f_small))
        return self._items[key]

    def draw(self, now):
        d, c = self.d, self.d.canvas
        if not hasattr(d, "content"):
            return  # not laid out yet
        chat = d.app.chat
        x, y, w, h = d.content
        shadow = d._px(SHADOW)
        box_h = d._px(self.BOX_H)
        box_top = y + h - box_h - shadow
        list_top, list_bottom = y, box_top - d._px(20)

        if len(chat) != self._seen:
            self._seen = len(chat)
            self.scroll = 0.0  # something new arrived: follow it down

        # Newest first, stacking upward from just above the box.
        pad_x, pad_y = d._px(self.PAD_X), d._px(self.PAD_Y)
        wrap = int(min(w * 0.7, d._px(620))) - 2 * pad_x
        # Replies get more room: code blocks and tables need it.
        rich_wrap = int(min(w * 0.84, d._px(760))) - 2 * pad_x
        # Room under the newest bubble for its shadow, or the mask clips it.
        cursor = list_bottom - shadow - d._px(2) + self.scroll
        used, self._tool_hits, self._rich = set(), [], []
        for key, kind, data in reversed(list(self._blocks(chat, now))):
            used.add(key)
            if kind == "bubble":
                top = self._draw_bubble(key, data, x, w, cursor, wrap, list_top, list_bottom)
                cursor = top - d._px(self.GAP)
            elif kind == "rich":
                top = self._draw_rich(key, data, x, cursor, rich_wrap, list_top, list_bottom, now)
                cursor = top - d._px(self.GAP)
            else:
                top = self._draw_tool(key, data, x, cursor, wrap, list_top, list_bottom, now)
                cursor = top - d._px(self.GAP) // 2

        # Blocks that are gone — the conversation was cleared — lose their items.
        for key in [k for k in self._items if k not in used]:
            for item in self._items.pop(key):
                if isinstance(item, richtext.RichText):
                    item.destroy()
                    continue
                c.delete(item)
                self._measured.pop(item, None)

        # Keep the scroll between the newest message and the oldest one.
        total = list_bottom + self.scroll - cursor
        room = list_bottom - list_top
        self.scroll = max(0.0, min(self.scroll, total - room)) if total > room else 0.0

        c.itemconfig(self.empty, state="hidden" if chat else "normal")
        c.coords(self.empty, x + w / 2, (list_top + list_bottom) / 2)

        right, bottom = c.winfo_width(), c.winfo_height()
        c.coords(self.mask_top, x - shadow - 4, 0, right, list_top)
        c.coords(self.mask_bottom, x - shadow - 4, list_bottom, right, bottom)
        for item in (self.mask_top, self.mask_bottom, d.title, d.subtitle):
            c.tag_raise(item)
        self._draw_box(x, box_top, w - shadow, box_h)
        self._draw_tip(list_top, list_bottom)

    def _draw_rich(self, key, data, x, bottom, wrap, list_top, list_bottom, now):
        """A reply bubble drawn as rich text. Bottom at `bottom`; returns its top."""
        d = self.d
        text, _mine, colour = data
        bg, view = self._get(key, "rich")
        shadow = d._px(SHADOW)
        pad_x, pad_y = d._px(self.PAD_X), d._px(self.PAD_Y)
        tw, th = view.layout(text, wrap, now)
        bw, bh = tw + 2 * pad_x, th + 2 * pad_y
        top = bottom - bh
        size = (int(bw), int(bh), colour)
        if d._sizes.get(bg) != size:
            d._sizes[bg] = size
            d._panel(bg, x, top, bw, bh, colour, radius=16)
        d.canvas.coords(bg, x - shadow - 3, top - shadow - 3)
        view.place(x + pad_x, top + pad_y)
        on = top + bh + shadow > list_top and top < list_bottom
        d.canvas.itemconfig(bg, state="normal" if on else "hidden")
        view.show(on)
        if on:
            self._rich.append(view)
        return top

    def _draw_tip(self, list_top, list_bottom):
        """The tooltip: where a link goes, what a button runs — or, for a few
        seconds after a click, what that click had to say."""
        d, c = self.d, self.d.canvas
        text = anchor = None
        if self._note and time.time() < self._note[3]:
            text, ax, ay = self._note[0], self._note[1], self._note[2]
            anchor = (ax, ay + d._px(6))
        elif self.pointer and list_top <= self.pointer[1] <= list_bottom:
            got = self.hit_rich(*self.pointer)
            if got:
                text = got[2]
                anchor = (self.pointer[0] + d._px(12), got[3][3] + d._px(6))
        if not text:
            for item in (self.tip, self.tip_bg):
                c.itemconfig(item, state="hidden")
            return
        pad = d._px(8)
        c.itemconfig(self.tip, text=_clip(text, 600), width=d._px(420), state="normal")
        x0, y0, x1, y1 = c.bbox(self.tip)
        tw, th = x1 - x0 + 2 * pad, y1 - y0 + 2 * pad
        tx, ty = anchor
        right = c.winfo_width() - d._px(12)
        tx = min(tx, right - tw)
        if ty + th > list_bottom:  # no room below: put it above the thing instead
            ty = max(list_top, ty - th - d._px(36))
        if self._tip_size != (int(tw), int(th)):
            self._tip_size = (int(tw), int(th))
            s = paint.Sheet(int(tw), int(th))
            s.fill(paint.rounded_rect(s, 0, 0, int(tw), int(th), d._px(8)), INK, alpha=0.92)
            d._sheet(self.tip_bg, s)
        c.coords(self.tip_bg, tx, ty)
        c.coords(self.tip, tx + pad, ty + pad)
        c.itemconfig(self.tip_bg, state="normal")
        c.tag_raise(self.tip_bg)
        c.tag_raise(self.tip)

    def _draw_box(self, x, top, w, h):
        d, c = self.d, self.d.canvas
        cap = d._px(self.CAP)
        if self._panel_size != (int(w), h):
            self._panel_size = (int(w), h)
            sheet, self._pad = text_box(d._px, int(w), h, cap)
            d._sheet(self.panel, sheet)
        c.coords(self.panel, x - self._pad, top - self._pad)
        c.tag_raise(self.panel)
        c.tag_raise(self.cap)

        cy = top + h / 2
        cap_x = x + w - d._px(7) - cap
        c.coords(self.cap, cap_x + cap / 2, cy)
        inset = d._px(16)
        c.coords(self.entry_item, x + inset, cy)
        c.itemconfig(self.entry_item, width=int(w - inset - cap - d._px(22)))
        self.send_box = (cap_x, top + d._px(7), cap, h - d._px(14))

    def _draw_bubble(self, key, data, x, w, bottom, wrap, list_top, list_bottom):
        """One bubble with its bottom at `bottom`. Returns its top."""
        d, c = self.d, self.d.canvas
        text, mine, colour = data
        bg, item = self._get(key, "bubble")
        shadow = d._px(SHADOW)
        pad_x, pad_y = d._px(self.PAD_X), d._px(self.PAD_Y)
        tw, th = self._measure(item, text, wrap)
        bw, bh = tw + 2 * pad_x, th + 2 * pad_y
        top = bottom - bh
        bx = x + w - bw - shadow if mine else x
        size = (int(bw), int(bh), colour)
        if d._sizes.get(bg) != size:
            d._sizes[bg] = size
            d._panel(bg, bx, top, bw, bh, colour, radius=16)
        c.coords(bg, bx - shadow - 3, top - shadow - 3)
        c.coords(item, bx + pad_x, top + pad_y)
        on = top + bh + shadow > list_top and top < list_bottom
        for i in (bg, item):
            c.itemconfig(i, state="normal" if on else "hidden")
        return top

    def _draw_tool(self, key, part, x, bottom, wrap, list_top, list_bottom, now):
        """One tool call: a grey line, and its arguments beneath it when it's
        been clicked open. A call that set an agent off also carries a status
        pill on its line, and opened, the agent's activity streaming in live.
        Bottom at `bottom`; returns its top."""
        d, c = self.d, self.d.canvas
        label, box, code, stream, pill, pill_text = self._get(key, "tool")
        lx = x + d._px(6)
        lw, lh = self._measure(label, _tool_label(part, now), wrap)
        run = _run_of(part)
        height = lh
        opened = part.get("open")
        pad_x, pad_y = d._px(12), d._px(8)
        if opened:
            cw, ch = self._measure(code, _tool_args(part), wrap - 2 * pad_x)
            sw = sh = 0
            if run is not None:
                sw, sh = self._measure(stream, _run_lines(run, now), wrap - 2 * pad_x)
                sh += d._px(10)  # gap between the arguments and the activity
            bw, bh = max(cw, sw) + 2 * pad_x, ch + sh + 2 * pad_y
            height += d._px(6) + bh
        top = bottom - height
        on = top + height > list_top and top < list_bottom

        c.coords(label, lx, top)
        c.itemconfig(label, state="normal" if on else "hidden")
        if opened:
            size = (int(bw), int(bh))
            if d._sizes.get(box) != size:
                d._sizes[box] = size
                s = paint.Sheet(*size)
                shape = paint.rounded_rect(s, 1, 1, size[0] - 2, size[1] - 2, d._px(10))
                s.fill(shape, TRACK)
                s.stroke(shape, INK, d._px(1.2), alpha=0.25)
                d._sheet(box, s)
            box_top = top + lh + d._px(6)
            c.coords(box, lx, box_top)
            c.coords(code, lx + pad_x, box_top + pad_y)
            c.coords(stream, lx + pad_x, box_top + pad_y + ch + d._px(10))
        for item in (box, code):
            c.itemconfig(item, state="normal" if on and opened else "hidden")
        c.itemconfig(stream, state="normal" if on and opened and run is not None else "hidden")

        # The pill: green with a tick once the agent has finished the job.
        badge = _run_badge(run, now) if run is not None else None
        if badge and on:
            text, fill, ink = badge
            c.itemconfig(pill_text, text=text, fill=ink, state="normal")
            x0, y0, x1, y1 = c.bbox(pill_text)
            pw, ph = int(x1 - x0 + d._px(18)), int(lh + d._px(2))
            if d._sizes.get(pill) != (pw, ph, fill):
                d._sizes[pill] = (pw, ph, fill)
                s = paint.Sheet(pw, ph)
                shape = paint.rounded_rect(s, 1, 1, pw - 2, ph - 2, (ph - 2) / 2)
                s.fill(shape, fill)
                s.stroke(shape, INK, d._px(1.2), alpha=0.35)
                d._sheet(pill, s)
            px_ = lx + lw + d._px(10)
            c.coords(pill, px_, top - d._px(1))
            c.coords(pill_text, px_ + d._px(9), top - d._px(1) + ph / 2)
            c.itemconfig(pill, state="normal")
            c.tag_raise(pill_text, pill)
        else:
            c.itemconfig(pill, state="hidden")
            c.itemconfig(pill_text, state="hidden")
        if on:
            self._tool_hits.append((lx, top, lx + lw, top + lh, part))
        return top

    def _measure(self, item, text, wrap):
        """A text item's size, remeasured only when its text or width changes."""
        c = self.d.canvas
        cached = self._measured.get(item)
        if cached and cached[:2] == (text, wrap):
            return cached[2:]
        # A hidden item has no bounding box, so it has to be shown to be measured.
        c.itemconfig(item, text=text, width=wrap, state="normal")
        x0, y0, x1, y1 = c.bbox(item)
        self._measured[item] = (text, wrap, x1 - x0, y1 - y0)
        return x1 - x0, y1 - y0


class PromptFile:
    """One file that ends up in front of a model, as the Prompts tab edits it.

    Reading and saving go through the module that owns the file where there is
    one, so the tab gets the same caps and the same backup-to-history as an
    edit the model makes through an endpoint.
    """

    def __init__(self, key, label, path, about, read=None, save=None, limit=None):
        self.key, self.label, self.path, self.about = key, label, path, about
        self._read, self._save, self.limit = read, save, limit

    def read(self):
        if self._read:
            return self._read()
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def save(self, text):
        if self._save:
            return self._save(text)
        text = text.replace("\r\n", "\n").strip() + "\n"
        folder = os.path.join(os.path.dirname(self.path), "history")
        os.makedirs(folder, exist_ok=True)
        now = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"
        name = os.path.splitext(os.path.basename(self.path))[0]
        shutil.copyfile(self.path, os.path.join(folder, f"{name}-{stamp}.md"))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)
        return text

    def mtime(self):
        try:
            return os.path.getmtime(self.path)
        except OSError:
            return None

    def where(self):
        return os.path.relpath(self.path, os.path.dirname(os.path.abspath(__file__)))


def prompt_files():
    """Every prompt file, in the order a model meets them: the assistant's
    three parts, its long-term memory and the business brief, then each subagent and agent's own."""
    about = {
        "main": "what BananaGent is and how it does the job — opens the system prompt",
        "personality": "how it sounds",
        "texting": "how it texts when a message comes from Discord",
        "tools": "how it uses tools; {{tools}} becomes the live tool list",
    }
    files = [PromptFile(part, part.title(), prompts.path(part), about.get(part, ""),
                        read=lambda p=part: prompts.read(p),
                        save=lambda t, p=part: prompts.write(p, t),
                        limit=prompts.MAX_CHARS)
             for part in prompts.PARTS]
    files.append(PromptFile("memory", "Memory", memory.LONG_TERM,
                            "long-term memory, sent after the prompts with every message",
                            read=memory.read, save=memory.write,
                            limit=config.LONG_TERM_MAX_CHARS))
    files.append(PromptFile("business", "Business", business.BRIEF,
                            "the business brief, sent after long-term memory with every "
                            "message, with its tasks, leads, metrics and log",
                            read=business.read, save=business.write,
                            limit=config.BUSINESS_MAX_CHARS))
    for kind in subagents.KINDS.values():
        files.append(PromptFile(f"subagent:{kind.name}", f"Subagent: {kind.name}",
                                os.path.join(subagents.HERE, f"{kind.name}.md"),
                                "a subagent's whole system prompt, for one job handed off"))
    files.append(PromptFile("browser", "Browser agent", agents.browser.PROMPT,
                            "the browsing agent's prompt, sent with every step it takes"))
    files.append(PromptFile("coder", "Coding agent", agents.coder.PROMPT,
                            "the coding agent's prompt: how it runs a job and briefs "
                            "Claude Code; {{tools}} becomes its tool list"))
    return files


class PromptsPage:
    """The Prompts tab: every prompt file, editable in place.

    A row of tabs picks the file; the editor under it is a real Tk text widget
    sitting in a card, since the canvas can't do editing. Unsaved edits are kept
    per file, so hopping between tabs loses nothing, and a tab with changes
    wears a dot. Ctrl+S or the Save button writes it; the change applies from
    the next message. If a file changes on disk while it's open — the model
    edited its own memory, say — the editor follows, unless you've got edits
    of your own in it, in which case it says so and leaves them alone.
    """

    TAB_H = 36
    BUTTON_W, BUTTON_H = 104, 36

    def __init__(self, dash):
        self.d = dash
        c = dash.canvas
        self.files = prompt_files()
        self.current = self.files[0]
        self._disk = {}       # key -> (mtime, text) as last read from disk
        self._drafts = {}     # key -> text in the editor, if it differs from disk
        self._views = {}      # key -> (insert index, yview) to come back to
        self._stale = set()   # keys whose file changed on disk under a draft
        self._editor_size = None
        self._message = None  # (text, colour, until) — a save's result, for a while
        self._checked = 0.0
        self._loading = False
        self.tab_geo = []
        self.buttons = {}

        self.tabs = [(c.create_image(0, 0, anchor="nw"),
                      c.create_text(0, 0, anchor="center", text=f.label,
                                    font=dash.f_tab, fill=INK))
                     for f in self.files]
        self.card = c.create_image(0, 0, anchor="nw")
        self.status = c.create_text(0, 0, anchor="w", text="", font=dash.f_label, fill=MUTED)
        self.count = c.create_text(0, 0, anchor="w", text="", font=dash.f_small, fill=MUTED)
        self.save_bg = c.create_image(0, 0, anchor="nw")
        self.save_text = c.create_text(0, 0, anchor="center", text="Save",
                                       font=dash.f_value, fill=INK)
        self.revert_bg = c.create_image(0, 0, anchor="nw")
        self.revert_text = c.create_text(0, 0, anchor="center", text="Revert",
                                         font=dash.f_value, fill=INK)

        self.text = tk.Text(
            c, wrap="word", undo=True, autoseparators=True, maxundo=-1,
            font=dash.f_edit, bg=CARD, fg=INK, insertbackground=INK,
            insertwidth=dash._px(2), relief="flat", bd=0, highlightthickness=0,
            selectbackground=NAV, selectforeground=INK, inactiveselectbackground=TRACK,
            padx=0, pady=0, spacing3=dash._px(3), tabs=(dash.f_edit.measure("    "),))
        self.text.tag_configure("note", foreground=MUTED, font=dash.f_edit_note)
        self.text.tag_configure("heading", foreground=INK, font=dash.f_edit_bold)
        self.text.tag_configure("slot", foreground=LABEL, font=dash.f_edit_bold)
        self.text.bind("<<Modified>>", self._on_modified)
        for key in ("<Control-s>", "<Control-S>"):
            self.text.bind(key, lambda _e: self.save() or "break")
        self.text_item = c.create_window(0, 0, anchor="nw", window=self.text)

        self._load(self.current)

    # -- files ----------------------------------------------------------------
    def _read_disk(self, f):
        text = f.read()
        self._disk[f.key] = (f.mtime(), text)
        self._stale.discard(f.key)
        return text

    def _saved(self, f):
        if f.key not in self._disk:
            self._read_disk(f)
        return self._disk[f.key][1]

    def dirty(self, f):
        return f.key in self._drafts and self._drafts[f.key] != self._saved(f)

    def _load(self, f):
        """Put a file in the editor: its draft if it has one, else what's on disk."""
        try:
            text = self._drafts.get(f.key, self._saved(f))
        except Exception as e:
            text, self._message = "", (f"Couldn't read {f.where()}: {e}", BAD, time.time() + 8)
        self._fill(text)
        self.text.edit_reset()
        spot = self._views.get(f.key)
        if spot:
            self.text.mark_set("insert", spot[0])
            self.text.yview_moveto(spot[1])

    def _fill(self, text):
        """Replace the editor's contents without it counting as an edit."""
        self._loading = True
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.text.edit_modified(False)
        self._loading = False
        self._highlight()

    def select(self, f):
        if f is self.current:
            return
        self._views[self.current.key] = (self.text.index("insert"), self.text.yview()[0])
        self.current = f
        self._message = None
        self._load(f)
        self.focus()

    def save(self):
        f = self.current
        text = self.text.get("1.0", "end-1c")
        try:
            f.save(text)
        except Exception as e:
            self._message = (str(e), BAD, time.time() + 8)
            return
        self._drafts.pop(f.key, None)
        saved = self._read_disk(f)
        if saved != text:  # the save tidied trailing whitespace; show what was kept
            spot = (self.text.index("insert"), self.text.yview()[0])
            self._fill(saved)
            self.text.mark_set("insert", spot[0])
            self.text.yview_moveto(spot[1])
        self._message = ("Saved. It applies from the next message.", GOOD, time.time() + 4)

    def revert(self):
        f = self.current
        self._drafts.pop(f.key, None)
        self._fill(self._read_disk(f))
        self._message = ("Back to what's saved.", MUTED, time.time() + 3)

    def _on_modified(self, _event=None):
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        if self._loading:
            return
        self._drafts[self.current.key] = self.text.get("1.0", "end-1c")
        self._highlight()

    def _follow_disk(self, now):
        """Pick up a change made to the file behind our back, once a second."""
        if now - self._checked < 1.0:
            return
        self._checked = now
        f = self.current
        if f.key in self._disk and f.mtime() == self._disk[f.key][0]:
            return
        if self.dirty(f):
            self._stale.add(f.key)  # leave their edits be, but say so
            return
        try:
            text = self._read_disk(f)
        except Exception:
            return
        self._drafts.pop(f.key, None)
        if text != self.text.get("1.0", "end-1c"):
            spot = (self.text.index("insert"), self.text.yview()[0])
            self._fill(text)
            self.text.mark_set("insert", spot[0])
            self.text.yview_moveto(spot[1])

    def _highlight(self):
        """Notes grey and italic, since they never reach the model; headings
        bold; {{placeholders}} in amber."""
        t = self.text
        for tag in ("note", "heading", "slot"):
            t.tag_remove(tag, "1.0", "end")
        body = t.get("1.0", "end-1c")
        for tag, pattern in (("heading", r"(?m)^#{1,6}\s.*$"),
                             ("slot", r"\{\{\w+\}\}"),
                             ("note", r"(?s)<!--.*?(?:-->|\Z)")):
            for m in re.finditer(pattern, body):
                t.tag_add(tag, f"1.0+{m.start()}c", f"1.0+{m.end()}c")
        t.tag_raise("note")

    # -- input ----------------------------------------------------------------
    def focus(self):
        self.text.focus_set()

    def wheel(self, delta):
        # With the editor focused, Tk scrolls it already; this covers the rest.
        if self.d.win.focus_get() is not self.text:
            self.text.yview_scroll(int(-delta / 120 * 3), "units")

    def hit(self, px, py):
        for f, (x, y, w, h) in zip(self.files, self.tab_geo):
            if x <= px <= x + w and y <= py <= y + h:
                return f
        for name, (x, y, w, h) in self.buttons.items():
            if x <= px <= x + w and y <= py <= y + h:
                return name
        return None

    def click(self, px, py):
        what = self.hit(px, py)
        if isinstance(what, PromptFile):
            return self.select(what)
        if what == "save":
            self.save()
        elif what == "revert":
            self.revert()
        self.focus()

    # -- drawing --------------------------------------------------------------
    def set_visible(self, on):
        c = self.d.canvas
        state = "normal" if on else "hidden"
        for item in (self.card, self.status, self.count, self.text_item,
                     self.save_bg, self.save_text, self.revert_bg, self.revert_text):
            c.itemconfig(item, state=state)
        for pair in self.tabs:
            for item in pair:
                c.itemconfig(item, state=state)
        if not on:
            self.tab_geo, self.buttons = [], {}

    def draw(self, now):
        d, c = self.d, self.d.canvas
        if not hasattr(d, "content"):
            return
        self._follow_disk(now)
        f = self.current
        x, y, w, h = d.content
        shadow = d._px(SHADOW)
        w -= shadow

        c.itemconfig(d.subtitle, text=f"{f.where()}  ·  {f.about}")

        # Tabs, wrapping onto a second row if the window is too narrow for one.
        tab_h, gap, pad = d._px(self.TAB_H), d._px(10), d._px(16)
        tx, ty = x, y
        self.tab_geo = []
        for pf, (bg, label) in zip(self.files, self.tabs):
            dirty = self.dirty(pf)
            text = pf.label + ("  •" if dirty else "")
            tw = d.f_tab.measure(text) + 2 * pad
            if tx + tw > x + w and tx > x:
                tx, ty = x, ty + tab_h + gap
            chosen = pf is f
            colour = NAV if chosen else CARD
            key = (tw, tab_h, colour)
            if d._sizes.get(bg) != key:
                d._sizes[bg] = key
                d._panel(bg, tx, ty, tw, tab_h, colour, radius=12)
            c.coords(bg, tx - shadow - 3, ty - shadow - 3)
            c.coords(label, tx + tw / 2, ty + tab_h / 2)
            c.itemconfig(label, text=text, fill=INK if chosen or dirty else "#7a5f14")
            self.tab_geo.append((tx, ty, tw, tab_h))
            tx += tw + gap

        # The buttons and status along the bottom.
        bw, bh = d._px(self.BUTTON_W), d._px(self.BUTTON_H)
        by = y + h - bh - shadow
        dirty = self.dirty(f)
        sx = x + w - bw
        rx = sx - bw - gap
        self._button(self.save_bg, self.save_text, sx, by, bw, bh, NAV if dirty else PAGE, dirty)
        self._button(self.revert_bg, self.revert_text, rx, by, bw, bh, CARD if dirty else PAGE, dirty)
        self.buttons = {"save": (sx, by, bw, bh), "revert": (rx, by, bw, bh)} if dirty else {}

        if self._message and time.time() >= self._message[2]:
            self._message = None
        if self._message:
            status, colour = self._message[:2]
        elif dirty and f.key in self._stale:
            status, colour = "Changed on disk since you started. Saving overwrites that.", BAD
        elif dirty:
            status, colour = "Unsaved changes  ·  Ctrl+S saves", LABEL
        else:
            status, colour = "Notes inside <!-- --> are for you; the model never sees them.", MUTED
        c.coords(self.status, x + d._px(4), by + bh / 2 - d._px(8))
        c.itemconfig(self.status, text=_clip(status, int((rx - x) / d._px(6.6))), fill=colour)
        length = len(self.text.get("1.0", "end-1c"))
        over = f.limit and length > f.limit
        c.coords(self.count, x + d._px(4), by + bh / 2 + d._px(10))
        c.itemconfig(self.count, fill=BAD if over else MUTED,
                     text=f"{length:,} of {f.limit:,} characters" if f.limit
                     else f"{length:,} characters")

        # The editor's card fills everything between.
        top = ty + tab_h + d._px(18)
        bottom = by - d._px(18)
        ch = max(d._px(60), bottom - top)
        key = (int(w), int(ch))
        if d._sizes.get(self.card) != key:
            d._sizes[self.card] = key
            d._panel(self.card, x, top, w, ch, CARD)
        c.coords(self.card, x - shadow - 3, top - shadow - 3)
        inset = d._px(20)
        c.coords(self.text_item, x + inset, top + d._px(16))
        size = (int(w - 2 * inset), int(ch - d._px(32)))
        if self._editor_size != size:  # resizing a widget relayouts it; only when it changed
            self._editor_size = size
            c.itemconfig(self.text_item, width=size[0], height=size[1])

    def _button(self, bg, label, x, y, w, h, colour, live):
        d, c = self.d, self.d.canvas
        key = (w, h, colour)
        if d._sizes.get(bg) != key:
            d._sizes[bg] = key
            d._panel(bg, x, y, w, h, colour, radius=12)
        c.coords(bg, x - d._px(SHADOW) - 3, y - d._px(SHADOW) - 3)
        c.coords(label, x + w / 2, y + h / 2)
        c.itemconfig(label, fill=INK if live else MUTED)


class AgentRow:
    """One agent on the Agents page: a header, and its endpoints on a dropdown.

    Clicking the header rolls the list of endpoints open. When the agent is
    waiting on an approval the header grows two buttons, so the gate can be
    answered by clicking as well as through an endpoint.
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
def _tool_label(part, now):
    """A tool call's grey line: what ran, and whether it's still going."""
    arrow = "▾" if part.get("open") else "▸"
    if part["status"] == "running":
        return f"{arrow}  Running {part['name']}" + "." * (1 + int(now * 3) % 3)
    if part["status"] == "failed":
        return f"{arrow}  {part['name']} failed"
    return f"{arrow}  Ran {part['name']}"


def _tool_args(part):
    """The arguments the model passed, as shown when the line is opened."""
    args = part.get("args")
    if isinstance(args, dict):
        text = json.dumps(args, indent=2, ensure_ascii=False) if args else "no arguments"
    else:
        text = str(args)  # it wrote something that wasn't JSON; show it as written
    return text if len(text) <= 2000 else text[:2000] + "\n…"


def _run_of(part):
    """The agent run a tool call set off, as it stands now — or None if it
    didn't start one, or it's from before a restart and no longer kept."""
    if not part.get("run"):
        return None
    try:
        return agents.get(part["agent"]).run_view(part["run"])
    except Exception:
        return None


# status -> (pill text, fill, text colour)
RUN_BADGES = {
    "working": ("Working", "#ffe7a3", INK),
    "waiting": ("Waiting for you", "#ffc58a", INK),
    "done": ("✓  Completed", GOOD, "#ffffff"),
    "failed": ("✕  Failed", BAD, "#ffffff"),
    "stopped": ("■  Stopped", "#e3d9c2", INK),
}


def _run_badge(run, now):
    status = run.get("status") or "working"
    text, fill, ink = RUN_BADGES.get(status, RUN_BADGES["working"])
    if status == "working":
        text = f"●  Working · step {run.get('steps') or 0}" + "." * (1 + int(now * 3) % 3)
    return text, fill, ink


def _run_lines(run, now, keep=30):
    """The agent's activity, oldest first, as it streams in; then its answer."""
    lines = run.get("lines") or []
    shown = [_clip(line, 240) for line in lines[-keep:]]
    if len(lines) > keep:
        shown.insert(0, f"… {len(lines) - keep} earlier")
    text = "\n".join(f"›  {line}" for line in shown) or "›  starting" + "." * (1 + int(now * 3) % 3)
    result = run.get("result")
    if run.get("status") == "done" and isinstance(result, dict) and result.get("answer"):
        text += f"\n\n→  {_clip(result['answer'], 600)}"
    return text


def _unloads(loaded):
    """keep_alive -1 comes back as an expiry centuries away; that means never."""
    if not loaded["resident"]:
        return "—"
    left = loaded["unloads_in_seconds"]
    return "never" if left is not None and left > 365 * 86400 else _dur(left)


def _or(value, fallback="—"):
    return str(value) if value else fallback


def _gb(value):
    return f"{value:.1f} GB" if value else "—"


def _clip(text, n):
    text = str(text)
    return text if len(text) <= n else text[:n - 1] + "…"


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


def _dur(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
