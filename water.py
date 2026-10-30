"""The yellow water orb.

On wake it bends into being: a sheared, swirling teardrop with droplets pulled
in from outside, unwinding into a still sphere over 0.75s. After that it keeps
breathing slightly so it reads as liquid rather than a drawn circle.

The shape is one closed polygon of N points. Each point's radius is a sum of
harmonics, and its *angle* is sheared by how far out it sits — that radius-
dependent twist is what makes it look bent rather than merely wobbly.
"""

import math

TAU = math.pi * 2
N = 96  # polygon resolution; smooth=True splines between these

FORM_SECONDS = 0.75

# Shear and lobe must satisfy shear * lobe < 1, or the angles stop increasing
# monotonically and the polygon folds through itself.
MAX_SHEAR = 1.5
MAX_LOBE = 0.55
SPIN_TURNS = 1.35  # how far it rotates while forming

PALETTES = {
    # state: (body, core, highlight)
    "typing": ("#e8a317", "#ffc93c", "#fff3c4"),
    "thinking": ("#e0980f", "#ffc226", "#fff0b8"),
    "answering": ("#d9930f", "#ffd76a", "#fff6d5"),
    "error": ("#c0392b", "#ff6b5b", "#ffd5cf"),
}


def _ease_out(t):
    return 1 - (1 - t) ** 3


class WaterOrb:
    def __init__(self, canvas, radius, px):
        self.canvas = canvas
        self.radius = radius
        self.px = px
        self._t0 = None

        blank = (0, 0, 0, 0, 0, 0)
        self._body = canvas.create_polygon(blank, smooth=True, fill=PALETTES["typing"][0], outline="")
        self._core = canvas.create_polygon(blank, smooth=True, fill=PALETTES["typing"][1], outline="")
        self._spec = canvas.create_oval(0, 0, 0, 0, fill=PALETTES["typing"][2], outline="")
        self._drops = [canvas.create_oval(0, 0, 0, 0, fill=PALETTES["typing"][1], outline="")
                       for _ in range(6)]

    def restart(self, now):
        """Play the formation animation from the top."""
        self._t0 = now

    def forming(self, now):
        return self._t0 is not None and (now - self._t0) < FORM_SECONDS

    # -- shape ---------------------------------------------------------------
    def _ring(self, cx, cy, scale, spin, shear, lobe, ripple, phase, swirl2):
        pts = []
        for i in range(N):
            a = TAU * i / N
            r = (1.0
                 + lobe * math.cos(a)
                 + swirl2 * math.sin(2 * a + spin * 1.3)
                 + ripple * math.sin(3 * a + phase * 2.0))
            # Twist proportional to how far this point sits from the mean
            # radius — the outside laps the inside, so the blob curls.
            ang = a + spin + shear * (r - 1.0)
            rr = scale * r
            pts.append(cx + rr * math.cos(ang))
            pts.append(cy + rr * math.sin(ang))
        return pts

    def render(self, now, cx, cy, state, level=0.0, churn=0.0):
        t = 1.0 if self._t0 is None else min(1.0, max(0.0, (now - self._t0) / FORM_SECONDS))
        e = _ease_out(t)
        f = 1.0 - e  # 1 while bending into shape, 0 once settled

        body, core, spec = PALETTES.get(state, PALETTES["typing"])
        phase = now * 6.0

        # Grows in fast, overshoots a little, then tightens to its resting size.
        grow = _ease_out(min(1.0, t / 0.4))
        scale = self.radius * (0.30 + 0.70 * grow) * (1.0 + 0.15 * f) * (1.0 + 0.22 * level)

        spin = SPIN_TURNS * TAU * e + phase * (0.08 + 0.22 * churn)
        shear = MAX_SHEAR * f + 0.10 * churn
        lobe = MAX_LOBE * f + (0.035 + 0.05 * churn) * math.sin(phase * 0.7)
        swirl2 = 0.18 * f + 0.02 * math.sin(phase * 1.1)
        ripple = 0.26 * f + 0.028 + 0.05 * churn + 0.06 * level

        self.canvas.coords(self._body, *self._ring(cx, cy, scale, spin, shear, lobe, ripple, phase, swirl2))
        self.canvas.itemconfig(self._body, fill=body)

        # Inner highlight. It shares the body's twist so it reads as light
        # sitting inside the same volume of liquid — give it its own rotation
        # and it separates into a second blob.
        inner = scale * 0.55
        ox, oy = -scale * 0.10, -scale * 0.12
        self.canvas.coords(self._core, *self._ring(
            cx + ox, cy + oy, inner, spin * 0.9, shear * 0.8, lobe * 0.8,
            ripple * 0.7, phase * 1.15, swirl2 * 0.7))
        self.canvas.itemconfig(self._core, fill=core)

        sr = scale * 0.20
        sx, sy = cx - scale * 0.34, cy - scale * 0.36
        self.canvas.coords(self._spec, sx - sr, sy - sr * 0.8, sx + sr, sy + sr * 0.8)
        self.canvas.itemconfig(self._spec, fill=spec, state="normal" if t > 0.35 else "hidden")

        self._render_drops(t, cx, cy, scale, spin, core)

    def _render_drops(self, t, cx, cy, scale, spin, color):
        """Droplets dragged in along a spiral while the orb forms."""
        n = len(self._drops)
        for k, drop in enumerate(self._drops):
            p = (t - k * 0.045) / 0.62
            if p <= 0.0 or p >= 1.0:
                self.canvas.itemconfig(drop, state="hidden")
                continue
            ease = _ease_out(p)
            ang = TAU * k / n + spin * 0.5 + (1.0 - ease) * 2.8
            dist = scale * (1.0 + 2.4 * (1.0 - ease))
            r = max(1.0, self.px(4) * (1.0 - p))
            x, y = cx + dist * math.cos(ang), cy + dist * math.sin(ang)
            self.canvas.coords(drop, x - r, y - r, x + r, y + r)
            self.canvas.itemconfig(drop, state="normal", fill=color)
