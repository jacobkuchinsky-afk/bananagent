"""Antialiased shapes, rasterized with numpy and handed to Tk as PNG images.

Tk's canvas draws hard-edged polygons — every curve on it is visibly stair-
stepped — so the dashboard doesn't use it for anything shaped. Each shape here
is a signed distance field instead: negative inside, positive outside, in
pixels. Coverage is that distance clipped across a single pixel, which gives
real antialiasing, exact round corners at any radius, and outlines centred on
the edge rather than sitting inside or outside it.

Buffers go to Tk as PNGs, which 8.6 decodes natively with alpha intact — about
0.4ms for a gauge-sized image, so re-rasterizing one every frame is fine.

Colors are premultiplied while compositing, so shapes can be layered onto
transparent backgrounds without the edges picking up a dark halo.
"""

import base64
import struct
import tkinter as tk
import zlib

import numpy as np

TAU = np.pi * 2


def rgb(color):
    """'#rrggbb' to a float triple in 0..1."""
    c = color.lstrip("#")
    return np.array([int(c[i:i + 2], 16) / 255.0 for i in (0, 2, 4)], np.float32)


def _coverage(d):
    """How much of a pixel a shape covers, given its distance in pixels."""
    return np.clip(0.5 - d, 0.0, 1.0)


class Sheet:
    """An RGBA buffer you composite distance fields into."""

    def __init__(self, w, h):
        self.w, self.h = int(w), int(h)
        self.buf = np.zeros((self.h, self.w, 4), np.float32)  # premultiplied
        ys, xs = np.mgrid[0:self.h, 0:self.w]
        self.x = xs.astype(np.float32) + 0.5  # pixel centres
        self.y = ys.astype(np.float32) + 0.5

    # -- compositing ---------------------------------------------------------
    def _over(self, color, a):
        c = rgb(color) if isinstance(color, str) else color
        inv = 1.0 - a
        for k in range(3):
            self.buf[..., k] = c[k] * a + self.buf[..., k] * inv
        self.buf[..., 3] = a + self.buf[..., 3] * inv

    def fill(self, d, color, alpha=1.0):
        self._over(color, _coverage(d) * alpha)
        return self

    def stroke(self, d, color, width=2.0, alpha=1.0):
        """Paint a band centred on the shape's edge."""
        self._over(color, _coverage(np.abs(d) - width * 0.5) * alpha)
        return self

    def glow(self, d, color, spread, alpha=0.5):
        """A soft falloff outside the shape, for shadows."""
        self._over(color, np.clip(1.0 - d / spread, 0.0, 1.0) ** 2 * alpha)
        return self

    # -- output --------------------------------------------------------------
    def rgba8(self):
        a = self.buf[..., 3]
        safe = np.maximum(a, 1e-6)
        out = np.empty((self.h, self.w, 4), np.uint8)
        for k in range(3):
            out[..., k] = np.clip(self.buf[..., k] / safe, 0, 1) * 255 + 0.5
        out[..., 3] = np.clip(a, 0, 1) * 255 + 0.5
        return out

    def png(self, level=1):
        return to_png(self.rgba8(), level)

    def photo(self, level=1):
        """A Tk image. Keep a reference to it or Tk drops the pixels."""
        return photo(self.rgba8(), level)


def photo(rgba8, level=1):
    """An (h, w, 4) array as a Tk image, alpha included."""
    return tk.PhotoImage(
        data=base64.b64encode(to_png(rgba8, level)).decode("ascii"), format="png")


# -- PNG ---------------------------------------------------------------------
def _chunk(tag, data):
    body = tag + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def to_png(rgba8, level=1):
    """Encode an (h, w, 4) uint8 array. Level 1 is plenty for on-screen use."""
    h, w, _ = rgba8.shape
    # A PNG scanline is prefixed with its filter type; 0 means "store as-is",
    # which costs a few bytes and saves the whole filtering pass.
    raw = np.zeros((h, w * 4 + 1), np.uint8)
    raw[:, 1:] = rgba8.reshape(h, w * 4)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw.tobytes(), level))
            + _chunk(b"IEND", b""))


# -- distance fields ---------------------------------------------------------
def rounded_rect(s, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    dx = np.abs(s.x - (x + w / 2)) - (w / 2 - r)
    dy = np.abs(s.y - (y + h / 2)) - (h / 2 - r)
    outside = np.hypot(np.maximum(dx, 0), np.maximum(dy, 0))
    return outside + np.minimum(np.maximum(dx, dy), 0) - r


def disc(s, cx, cy, r):
    return np.hypot(s.x - cx, s.y - cy) - r


def arc(s, cx, cy, r, thickness, a0, a1):
    """A ring segment with round caps. Angles in radians, y down."""
    dx, dy = s.x - cx, s.y - cy
    half = thickness * 0.5
    band = np.abs(np.hypot(dx, dy) - r) - half

    sweep = (a1 - a0) % TAU
    if sweep >= TAU - 1e-6:
        return band
    within = (np.arctan2(dy, dx) - a0) % TAU <= sweep
    caps = np.minimum(
        np.hypot(dx - r * np.cos(a0), dy - r * np.sin(a0)),
        np.hypot(dx - r * np.cos(a1), dy - r * np.sin(a1)),
    ) - half
    return np.where(within, band, caps)


def path(s, points, thickness):
    """A polyline with round caps and joins."""
    half = thickness * 0.5
    best = None
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        vx, vy = x1 - x0, y1 - y0
        span = vx * vx + vy * vy
        px, py = s.x - x0, s.y - y0
        t = 0.0 if span == 0 else np.clip((px * vx + py * vy) / span, 0.0, 1.0)
        d = np.hypot(px - t * vx, py - t * vy)
        best = d if best is None else np.minimum(best, d)
    return (best if best is not None else np.full_like(s.x, 1e6)) - half


def tube(s, centres, radii):
    """A union of discs — a tube whose thickness varies along its length.

    Accumulated one disc at a time rather than as a pixels-by-discs array,
    which for an icon-sized render would be hundreds of megabytes.
    """
    best = None
    for (cx, cy), r in zip(centres, radii):
        d = np.hypot(s.x - cx, s.y - cy) - r
        best = d if best is None else np.minimum(best, d)
    return best if best is not None else np.full_like(s.x, 1e6)
