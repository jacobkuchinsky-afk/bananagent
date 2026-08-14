"""The always-on-top corner widget: a water orb, and the model's answer.

There's no panel — the window itself is fully transparent and only the orb and
text are painted, so it floats over whatever is on screen. Text gets a dark
shadow instead of a backdrop, since it has to stay legible against anything.

The orb is pinned to the bottom of the window and the answer grows upward, so
the orb never jumps around as text arrives.

Tkinter insists on owning the main thread, so everything here must be called
from it — main.py funnels worker-thread events through a queue to keep that true.
"""

import ctypes
import time
import tkinter as tk
import tkinter.font as tkfont

import config
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
    LISTENING = "listening"
    THINKING = "thinking"
    ANSWERING = "answering"
    ERROR = "error"

    def __init__(self):
        self.scale = _dpi_setup()
        self.state = self.IDLE
        self.level = 0.0  # mic loudness 0..1, driven from outside
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
        self._line_h = self._font_body.metrics("linespace")

        self._build()
        self._resize(self.pad * 2 + self.orb_r * 2)
        self.root.after(16, self._tick)

    # -- public API (main thread only) --------------------------------------
    def set_state(self, state):
        was_hidden = not self._visible
        self.state = state
        if state == self.IDLE:
            self.hide()
        else:
            self.show()
            if was_hidden:
                # Bend into existence each time it wakes up.
                self.orb.restart(time.perf_counter())

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

        churn = {self.THINKING: 1.0, self.LISTENING: 0.25}.get(self.state, 0.0)
        level = self.level if self.state == self.LISTENING else 0.0
        h = int(self.canvas.cget("height") or 0)
        self.orb.render(
            now,
            self.pad + self.orb_r,
            h - self.pad - self.orb_r,
            self.state,
            level=level,
            churn=churn,
        )

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
        text = self._text.strip()
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
