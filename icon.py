"""The banana — drawn as math, at whatever size is asked for.

One routine serves both the Windows icon and the logo in the dashboard's nav,
so they can never drift apart. The shape is a circular arc for the spine and a
thickness that swells in the middle and tapers to the tips; the body is the
union of discs along it, which gives a crescent with genuinely round ends
rather than a chopped-off tube.

`ensure_ico()` writes banana.ico next to the app the first time it's needed.
The file is generated rather than committed because it is entirely derived
from this code — change a number here and the icon follows.
"""

import math
import os
import struct

import numpy as np

import paint

PEEL = "#ffc93c"
PEEL_LIT = "#ffeaa0"
PEEL_DEEP = "#e8a317"
TIP = "#6b4a2b"
INK = "#101010"

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _shape(samples=200):
    """The banana in its own units: spine points, body radius at each, stem.

    The spine is the lower arc of a circle centred above the shape, so it
    smiles; the whole thing is tilted the way a banana lies. Thickness peaks at
    the middle and never quite reaches zero, so the tips round off instead of
    coming to a needle point. The stem carries on past the upper tip along the
    curve's own tangent, so it looks grown rather than stuck on.
    """
    radius, fat = 1.0, 0.30
    a0, a1 = math.radians(30), math.radians(150)
    tilt = math.radians(-20)
    cos_t, sin_t = math.cos(tilt), math.sin(tilt)

    def turn(x, y):
        return x * cos_t - y * sin_t, x * sin_t + y * cos_t

    centres, radii = [], []
    for i in range(samples):
        s = i / (samples - 1)
        a = a0 + (a1 - a0) * s
        centres.append(turn(radius * math.cos(a), radius * math.sin(a)))
        radii.append(fat * (0.06 + 0.94 * math.sin(math.pi * s) ** 0.40))

    # Continue past the first tip, tapering, for the stem.
    stem_c, stem_r = [], []
    for i in range(24):
        s = i / 23
        a = a0 - math.radians(17) * s
        grow = 1.0 + 0.16 * s
        stem_c.append(turn(radius * grow * math.cos(a), radius * grow * math.sin(a)))
        stem_r.append(fat * (0.20 - 0.11 * s))
    return centres, radii, stem_c, stem_r


def _fit(size, groups):
    """Scale and centre the shape so it fills the frame with a hairline margin."""
    pts = [(x, y, r) for centres, radii in groups for (x, y), r in zip(centres, radii)]
    lo_x = min(x - r for x, _, r in pts)
    hi_x = max(x + r for x, _, r in pts)
    lo_y = min(y - r for _, y, r in pts)
    hi_y = max(y + r for _, y, r in pts)

    margin = size * 0.055
    span = max(hi_x - lo_x, hi_y - lo_y)
    k = (size - 2 * margin) / span
    ox = (size - (hi_x - lo_x) * k) / 2 - lo_x * k
    oy = (size - (hi_y - lo_y) * k) / 2 - lo_y * k

    def place(centres, radii):
        return ([(x * k + ox, y * k + oy) for x, y in centres], [r * k for r in radii])
    return place, k


def draw(size):
    """The banana as an (h, w, 4) uint8 array."""
    size = int(size)
    s = paint.Sheet(size, size)
    spine, thick, stem_c, stem_r = _shape()
    place, _ = _fit(size, [(spine, thick), (stem_c, stem_r)])
    spine, thick = place(spine, thick)
    stem_c, stem_r = place(stem_c, stem_r)

    body = paint.tube(s, spine, thick)
    stem = paint.tube(s, stem_c, stem_r)

    # The lit edge is the same tube, thinner and pushed toward the upper left,
    # then intersected with the body so it can't spill past the outline.
    lit = paint.tube(s, [(x - size * 0.035, y - size * 0.055) for x, y in spine],
                     [r * 0.50 for r in thick])
    # A dark blossom end on the tip opposite the stem.
    cap = paint.disc(s, *spine[-1], thick[-1] * 2.0)

    outline = max(1.0, size * 0.052)
    s.fill(stem, TIP)
    s.stroke(stem, INK, outline)
    s.fill(body, PEEL_DEEP)
    s.fill(body + outline * 0.60, PEEL)
    s.fill(np.maximum(lit, body), PEEL_LIT, alpha=0.85)
    s.fill(np.maximum(cap, body), TIP)
    s.stroke(body, INK, outline)
    return s.rgba8()


# -- Windows .ico ------------------------------------------------------------
def _ico(images):
    """Pack RGBA arrays into an .ico.

    Written as classic DIB entries rather than embedded PNGs: Tk parses .ico
    files itself and only understands the DIB form.
    """
    entries, blobs, offset = [], [], 6 + 16 * len(images)
    for im in images:
        h, w, _ = im.shape
        # Height is doubled in the header to account for the AND mask that
        # follows, even though 32-bit icons carry their own alpha.
        header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, w * h * 4, 0, 0, 0, 0)
        bgra = np.ascontiguousarray(im[::-1, :, [2, 1, 0, 3]]).tobytes()
        mask = b"\x00" * (((w + 31) // 32) * 4 * h)
        blob = header + bgra + mask
        entries.append(struct.pack("<BBBBHHII", w % 256, h % 256, 0, 0, 1, 32,
                                   len(blob), offset))
        blobs.append(blob)
        offset += len(blob)
    return struct.pack("<HHH", 0, 1, len(images)) + b"".join(entries) + b"".join(blobs)


def path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "banana.ico")


def ensure_ico(force=False):
    """Write banana.ico if it isn't there yet, and return its path."""
    target = path()
    if force or not os.path.exists(target):
        with open(target, "wb") as f:
            f.write(_ico([draw(n) for n in ICO_SIZES]))
    return target


if __name__ == "__main__":
    print("wrote", ensure_ico(force=True))
