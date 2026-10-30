"""The always-on-top corner widget: a water orb, a text box, and the answer.

There's no panel — the window itself is fully transparent and only the orb,
the box and the text are painted, so it floats over whatever is on screen. Text
gets a dark outline instead of a backdrop, since it has to stay legible against
anything.

The orb and the box share the bottom row of the window and the answer grows
upward above them, so neither jumps around as text arrives.

Tkinter insists on owning the main thread, so everything here must be called
from it — main.py funnels worker-thread events through a queue to keep that true.
"""

import ctypes
import time
import tkinter as tk
import tkinter.font as tkfont

import config
import markup
from dashboard import CARD, INK, MUTED, NAV, SHADOW, text_box
from water import WaterOrb

# Any pixel painted this color is punched out of the window entirely. With no
# panel that's the whole background, which also makes it click-through.
_KEY = "#010203"


def _dpi_setup():
    """Go DPI-aware so we render crisp, and report the scale factor."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            return 1.0
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


def _work_area():
    """Screen rect minus the taskbar, so 'bottom-right' isn't under it."""
    try:
        from ctypes import wintypes

        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return None


class Overlay:
    IDLE = "idle"
    TYPING = "typing"
    THINKING = "thinking"
    ANSWERING = "answering"
    ERROR = "error"

    def __init__(self, on_submit=None):
        self.on_submit = on_submit  # called with the box's text on Enter
        self._prev_focus = None     # whoever had the keyboard before the box took it
        self.scale = _dpi_setup()
        self.state = self.IDLE
        self._visible = False
        self._fade = 0.0
        self._body_dirty = False
        self._text = ""

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)
        self.root.configure(bg=_KEY)
        for attr, val in (("-transparentcolor", _KEY), ("-toolwindow", True)):
            try:
                self.root.attributes(attr, val)
            except Exception:
                pass

        self.W = self._px(config.WIDTH)
        self.pad = self._px(14)
        self.orb_r = self._px(config.ORB_RADIUS)

        self.canvas = tk.Canvas(self.root, bg=_KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        self._font_body = tkfont.Font(family="Segoe UI", size=-self._px(13), weight="bold")
        self._font_box = tkfont.Font(family="Segoe UI Semibold", size=-self._px(14))
        self._font_chip = tkfont.Font(family="Segoe UI Symbol", size=-self._px(13), weight="bold")
        self._line_h = self._font_body.metrics("linespace")

        self._build()
        self._resize(self.pad * 2 + self.orb_r * 2)
        self.root.after(16, self._tick)

    # -- public API (main thread only) --------------------------------------
    def set_state(self, state):
        was_hidden = not self._visible
        was_typing = self.state == self.TYPING
        self.state = state
        if state == self.IDLE:
            self.hide()
        else:
            self.show()
            if was_hidden:
                # Bend into existence each time it wakes up.
                self.orb.restart(time.perf_counter())
        if state == self.TYPING:
            self.canvas.itemconfigure("box", state="normal")
            self.box.delete(0, "end")
            self._take_focus()
        else:
            self.canvas.itemconfigure("box", state="hidden")
            if was_typing:
                self._give_back_focus()
        if state in (self.IDLE, self.TYPING):
            self.set_status(None)  # the box takes that spot; a stale status shouldn't come back

    def set_status(self, text):
        """A grey line beside the orb, where the box sits while typing — what
        the model is doing right now, like which tool it's running. None clears it."""
        for item in self._status_items:
            self.canvas.itemconfigure(item, text=text or "",
                                      state="normal" if text else "hidden")

    def append(self, chunk):
        """Add a streamed token to the answer."""
        self._text += chunk
        self._body_dirty = True

    def set_error(self, message):
        self._text = message
        self._body_dirty = True
        self.set_state(self.ERROR)

    def clear(self):
        self._text = ""
        self._body_dirty = True

    def show(self):
        if not self._visible:
            self._visible = True
            self.root.deiconify()
            self.root.attributes("-topmost", True)

    def hide(self):
        self._visible = False

    # -- keyboard focus -----------------------------------------------------
    def _hwnd(self):
        try:
            return int(self.root.wm_frame(), 16)
        except Exception:
            return self.root.winfo_id()

    def _take_focus(self, tries=4):
        """Pull the keyboard over to the box, from whatever app had it.

        Windows won't hand the foreground to a background process just because
        it asked, which is what the hotkey makes us. Borrowing the current
        foreground thread's input state for a moment is what gets it through.
        It can still lose a race with the window being mapped, so it checks
        shortly after and tries again if the box didn't end up with the keys.
        """
        if self.state != self.TYPING:
            return
        self.root.update_idletasks()  # the Entry has to exist on screen to take focus
        user32 = ctypes.windll.user32
        fg = user32.GetForegroundWindow()
        mine = self._hwnd()
        if fg and fg != mine:
            self._prev_focus = fg
        fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        me = ctypes.windll.kernel32.GetCurrentThreadId()
        attached = bool(fg_thread and fg_thread != me
                        and user32.AttachThreadInput(me, fg_thread, True))
        try:
            user32.BringWindowToTop(mine)
            user32.SetForegroundWindow(mine)
        finally:
            if attached:
                user32.AttachThreadInput(me, fg_thread, False)
        self.box.focus_force()
        self.root.after(80, lambda: self._check_focus(tries))

    def _check_focus(self, tries):
        if self.state != self.TYPING or self._has_focus():
            return
        if tries > 1:
            self._take_focus(tries - 1)
        else:
            print("[overlay] couldn't take the keyboard; click the box to type")

    def _has_focus(self):
        return (ctypes.windll.user32.GetForegroundWindow() == self._hwnd()
                and self.root.focus_get() is self.box)

    def _give_back_focus(self):
        prev, self._prev_focus = self._prev_focus, None
        if prev and ctypes.windll.user32.IsWindow(prev):
            ctypes.windll.user32.SetForegroundWindow(prev)

    def _submit(self, _event=None):
        text = self.box.get().strip()
        if text and self.on_submit:
            self.on_submit(text)
        return "break"

    # -- construction -------------------------------------------------------
    def _px(self, n):
        return max(1, int(round(n * self.scale)))

    # Eight copies of the text in black, ringing the real one, fake a stroke.
    # With no panel behind it, white-on-white is otherwise unreadable, and a
    # single drop shadow only covers one direction.
    _OUTLINE = [(-1, -1), (0, -1), (1, -1), (-1, 0),
                (1, 0), (-1, 1), (0, 1), (1, 1)]

    def _build(self):
        wrap = self.W - 2 * self.pad
        # Outline first, so the real text draws on top of it.
        self._shadows = [
            self.canvas.create_text(0, 0, text="", anchor="sw", fill="#000000",
                                    font=self._font_body, width=wrap)
            for _ in self._OUTLINE
        ]
        self._label = self.canvas.create_text(
            0, 0, text="", anchor="sw", fill=config.FG,
            font=self._font_body, width=wrap,
        )
        self.orb = WaterOrb(self.canvas, self.orb_r, self._px)

        # The status line: grey, with the same black ring as the answer so it
        # stays readable over anything. The last item is the grey text itself.
        self._font_status = tkfont.Font(family="Segoe UI", size=-self._px(12), slant="italic")
        self._status_items = [
            self.canvas.create_text(0, 0, text="", anchor="w", font=self._font_status,
                                    fill=fill, state="hidden")
            for fill in ["#000000"] * len(self._OUTLINE) + ["#b8b8b8"]
        ]

        # The box is a dashboard card in miniature — white, a hard black
        # outline, an offset black shadow — with a borderless Entry sitting
        # inside it. Every outer edge of the panel is black, so its
        # antialiasing blends into the near-black transparency key instead of
        # leaving a light fringe. It's the one part of the window that takes clicks.
        self.box_x = self.pad * 2 + self.orb_r * 2
        self.box_w = self.W - self.box_x - self.pad - self._px(SHADOW)
        self.box_h = self._px(40)
        self._chip_w = self._px(30)
        self._panel_img = self._render_panel()
        self._panel = self.canvas.create_image(0, 0, anchor="nw", image=self._panel_img,
                                               tags="box")
        # The Enter key-cap at the right end: it says what sends the message.
        self._chip = self.canvas.create_text(0, 0, text="↵", font=self._font_chip,
                                             fill=INK, tags="box")

        self.box_var = tk.StringVar()
        self.box = tk.Entry(
            self.root, textvariable=self.box_var, font=self._font_box,
            bg=CARD, fg=INK, insertbackground=INK, insertwidth=self._px(2),
            relief="flat", bd=0, highlightthickness=0,
            selectbackground=NAV, selectforeground=INK)
        self.box.bind("<Return>", self._submit)
        self.box.bind("<Escape>", lambda _e: self.set_state(self.IDLE))
        self._inset = self._px(14)
        self._box_item = self.canvas.create_window(
            0, 0, window=self.box, anchor="w", tags="box",
            width=self.box_w - self._inset - self._chip_w - self._px(18))

        # Placeholder, laid over the empty Entry rather than typed into it, so
        # it can never be submitted or have to be selected away.
        self._hint = tk.Label(self.box, text="Ask BananaGent…", font=self._font_box,
                              bg=CARD, fg=MUTED, bd=0, padx=0, pady=0)
        self._hint.bind("<Button-1>", lambda _e: self.box.focus_set())
        self.box_var.trace_add("write", lambda *_: self._update_hint())
        self._update_hint()
        self.canvas.itemconfigure("box", state="hidden")

    def _render_panel(self):
        s, self._panel_pad = text_box(self._px, self.box_w, self.box_h, self._chip_w)
        return s.photo()

    def _update_hint(self):
        if self.box_var.get():
            self._hint.place_forget()
        else:
            self._hint.place(x=self._px(5), rely=0.5, anchor="w")  # clear of the caret

    # -- animation ----------------------------------------------------------
    def _tick(self):
        self.root.after(16, self._tick)  # ~60fps, the swirl deserves it
        now = time.perf_counter()

        # Fade the window in/out rather than popping it. Snap onto the target at
        # the end — easing alone lands at 0.99, and that last 1% is visible.
        target = 1.0 if self._visible else 0.0
        if self._fade != target:
            step = (target - self._fade) * 0.30
            self._fade = target if abs(target - self._fade) < 0.03 else self._fade + step
            self.root.attributes("-alpha", self._fade * config.OPACITY)
            if not self._visible and self._fade == 0.0:
                self.root.withdraw()

        if not self._visible:
            return

        if self._body_dirty:
            self._body_dirty = False
            self._relayout()

        churn = {self.THINKING: 1.0, self.TYPING: 0.25}.get(self.state, 0.0)
        h = int(self.canvas.cget("height") or 0)
        cy = h - self.pad - self.orb_r
        top = cy - self.box_h / 2
        self.canvas.coords(self._panel, self.box_x - self._panel_pad, top - self._panel_pad)
        self.canvas.coords(self._box_item, self.box_x + self._inset, cy)
        off = max(1, self._px(1))
        for item, (dx, dy) in zip(self._status_items, self._OUTLINE + [(0, 0)]):
            self.canvas.coords(item, self.box_x + dx * off, cy + dy * off)
        self.canvas.coords(self._chip, self.box_x + self.box_w - self._px(7) - self._chip_w / 2, cy)
        self.orb.render(now, self.pad + self.orb_r, cy, self.state, churn=churn)

    # -- sizing / placement -------------------------------------------------
    def _wrapped_lines(self, content):
        """How many display lines this text needs at the text block's width.

        Measured from font metrics rather than by asking a widget: a widget's
        answers are only meaningful once it's mapped and laid out, which isn't
        reliably true at the moment we need to size the window.
        """
        wrap = self.W - 2 * self.pad
        measure = self._font_body.measure
        total = 0
        for para in content.split("\n"):
            if not para:
                total += 1
                continue
            line = ""
            for word in para.split(" "):
                trial = f"{line} {word}" if line else word
                if not line or measure(trial) <= wrap:
                    line = trial
                else:
                    total += 1
                    line = word
                while measure(line) > wrap:
                    cut = max(1, int(len(line) * wrap / max(1, measure(line))))
                    total += 1
                    line = line[cut:]
            total += 1
        return max(1, total)

    def _fit(self, text, max_lines):
        """Drop words off the front until the tail fits, so streaming stays visible."""
        if self._wrapped_lines(text) <= max_lines:
            return text
        words = text.split(" ")
        lo, hi = 0, len(words)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._wrapped_lines("… " + " ".join(words[mid:])) <= max_lines:
                hi = mid
            else:
                lo = mid + 1
        return "… " + " ".join(words[lo:])

    def _relayout(self):
        orb_block = self.pad * 2 + self.orb_r * 2
        # Replies are markdown for the Chat tab; here they're read as plain
        # text, so the markup comes off (buttons show as their labels).
        text = (self._text if self.state == self.ERROR else markup.plain(self._text)).strip()
        if not text:
            self.canvas.itemconfig(self._label, text="")
            for s in self._shadows:
                self.canvas.itemconfig(s, text="")
            self._resize(orb_block)
            return

        line_px = self._line_h + self._px(3)
        max_text = self._px(config.MAX_HEIGHT) - orb_block - self.pad
        text = self._fit(text, max(1, max_text // line_px))

        text_h = self._wrapped_lines(text) * line_px
        height = self.pad + text_h + orb_block

        self.canvas.itemconfig(self._label, text=text)
        self._resize(height)

        baseline = height - orb_block
        off = max(1, self._px(2))
        for item, (dx, dy) in zip(self._shadows, self._OUTLINE):
            self.canvas.itemconfig(item, text=text)
            self.canvas.coords(item, self.pad + dx * off, baseline + dy * off)
        self.canvas.coords(self._label, self.pad, baseline)

    def _resize(self, height):
        height = int(height)
        self.canvas.configure(width=self.W, height=height)
        x, y = self._corner(self.W, height)
        self.root.geometry(f"{self.W}x{height}+{x}+{y}")

    def _corner(self, w, h):
        area = _work_area()
        if area:
            left, top, right, bottom = area
        else:
            left, top = 0, 0
            right = self.root.winfo_screenwidth()
            bottom = self.root.winfo_screenheight()
        m = self._px(config.MARGIN)
        c = config.CORNER
        x = left + m if "left" in c else right - w - m
        y = top + m if "top" in c else bottom - h - m
        return int(x), int(y)
