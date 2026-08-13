"""
Anti-aliased widget rendering for the tkinter UI.

tk.Canvas has no anti-aliasing, so every oval/polygon/arc it draws has hard
jagged pixel edges — the source of the "pixelated icons and toggles" look.
Everything here is drawn with PIL at 4x resolution and downscaled with
LANCZOS, giving smooth sub-pixel edges, then handed to tkinter as a
PhotoImage. Renders are cached by their full parameter set, so after the
first call each image is a dict lookup — the UI stays instant.

All images are composited onto a solid background colour (tk PhotoImages
over Canvas don't blend alpha reliably), so callers pass the exact bg the
image will sit on.
"""

from typing import Optional

_SS = 4  # supersample factor

_cache: dict = {}
# Live-resize renders one card image per unique width; bound the cache so a
# resize marathon can't grow memory without limit (evicted images re-render
# on demand in ~1ms).
_CACHE_MAX = 512


def clear_cache() -> None:
    """Drop all cached PhotoImages. Must be called when the Tk root that owns
    them is destroyed — images bound to a dead interpreter raise TclError."""
    _cache.clear()


def _photo(key, master, draw_fn, w: int, h: int):
    """Cache wrapper: render draw_fn onto a (w*_SS, h*_SS) canvas, downscale,
    return an ImageTk.PhotoImage. Never raises — returns None on failure so
    callers can fall back to plain canvas drawing."""
    # A PhotoImage belongs to the interpreter it was created in, so the cache is
    # per-interpreter too. One root is the norm, but a second one (a test
    # harness, a rebuilt root) would otherwise be handed a foreign image and
    # every draw using it would fail.
    try:
        key = (id(master.tk),) + tuple(key)
    except Exception:
        pass
    cached = _cache.get(key)
    if cached is not None:
        return cached
    if len(_cache) > _CACHE_MAX:
        _cache.clear()
    try:
        from PIL import Image, ImageDraw, ImageTk
        img = Image.new("RGBA", (w * _SS, h * _SS), (0, 0, 0, 0))
        draw_fn(ImageDraw.Draw(img), _SS)
        img = img.resize((w, h), Image.LANCZOS)
        bg = key[-1]  # every key ends with the bg colour
        base = Image.new("RGB", (w, h), bg)
        base.paste(img, (0, 0), img)
        photo = ImageTk.PhotoImage(base, master=master)
    except Exception:
        return None
    _cache[key] = photo
    return photo


def round_rect(master, w: int, h: int, r: int, fill: str,
               outline: str = "", width: int = 1, bg: str = "#0d0d0d"):
    """Smooth rounded rectangle (card background / button base)."""
    if w <= 0 or h <= 0:
        return None
    key = ("rr", w, h, r, fill, outline, width, bg)

    def _draw(d, s):
        half = (width * s) // 2
        box = [half, half, w * s - 1 - half, h * s - 1 - half]
        kw = {"radius": r * s, "fill": fill}
        if outline and width > 0:
            kw.update(outline=outline, width=width * s)
        d.rounded_rectangle(box, **kw)

    return _photo(key, master, _draw, w, h)


def toggle_pill(master, on: bool, w: int = 48, h: int = 28,
                accent: str = "#f39200", off_track: str = "#2d2d2d",
                dot: str = "#ffffff", bg: str = "#1a1a1a"):
    """Capsule toggle in either state. Two cached images per bg — flipping
    the toggle is just an itemconfigure image swap."""
    key = ("tg", on, w, h, accent, off_track, dot, bg)

    def _draw(d, s):
        track = accent if on else off_track
        d.rounded_rectangle([0, 0, w * s - 1, h * s - 1],
                            radius=(h * s) // 2, fill=track)
        m = 3 * s
        dia = h * s - 2 * m
        x = (w * s - m - dia) if on else m
        d.ellipse([x, m, x + dia, m + dia], fill=dot)

    return _photo(key, master, _draw, w, h)


# The impact-card glyphs are authored against a 26px design box; `u` turns
# `size` into a true scale factor. (They used to multiply design coords by the
# supersample factor alone, so a bigger `size` grew the canvas and left the
# glyph the same size inside it.)
_IMPACT_GRID = 26.0


def icon_clock(master, size: int = 30, color: str = "#f39200",
               bg: str = "#1a1a1a"):
    key = ("clock", size, color, bg)

    def _draw(d, s):
        u = size * s / _IMPACT_GRID
        c = size * s / 2
        r = 11 * u
        lw = max(1, int(round(2 * u)))
        d.ellipse([c - r, c - r, c + r, c + r], outline=color, width=lw)
        d.line([c, c, c, c - 6 * u], fill=color, width=lw)
        d.line([c, c, c + 5 * u, c + 2.5 * u], fill=color, width=lw)
        # round the hand joints like capstyle="round"
        for x, y in ((c, c), (c, c - 6 * u), (c + 5 * u, c + 2.5 * u)):
            d.ellipse([x - lw / 2, y - lw / 2, x + lw / 2, y + lw / 2],
                      fill=color)

    return _photo(key, master, _draw, size, size)


def icon_bolt(master, size: int = 30, color: str = "#4ade80",
              bg: str = "#1a1a1a"):
    key = ("bolt", size, color, bg)

    def _draw(d, s):
        u = size * s / _IMPACT_GRID
        c = size * s / 2
        pts = [(2.5, -12), (-7, 1.5), (-1, 1.5), (-2.5, 12), (7, -1.5), (1, -1.5)]
        d.polygon([(c + dx * u, c + dy * u) for dx, dy in pts], fill=color)

    return _photo(key, master, _draw, size, size)


def icon_flame(master, size: int = 30, color: str = "#f39200",
               cutout: str = "#1a1a1a", bg: str = "#1a1a1a"):
    key = ("flame", size, color, cutout, bg)

    def _draw(d, s):
        u = size * s / _IMPACT_GRID
        c = size * s / 2
        outer = [(0.5, -12), (4.5, -6.5), (7, -1), (7.5, 4), (4.5, 9.5),
                 (0, 11.5), (-4.5, 9.5), (-7.5, 4.5), (-7, -0.5), (-4, -4),
                 (-2.5, -1), (-1.5, -5.5)]
        d.polygon([(c + dx * u, c + dy * u) for dx, dy in outer], fill=color)
        inner = [(0.5, 1), (3.5, 4.5), (2.5, 8.5), (-0.5, 10),
                 (-3.5, 8), (-3, 4)]
        d.polygon([(c + dx * u, c + dy * u) for dx, dy in inner], fill=cutout)

    return _photo(key, master, _draw, size, size)


def icon_glyph(master, name: str, size: int = 20, color: str = "#f39200",
               bg: str = "#1a1a1a"):
    """Small settings glyphs, drawn in a 24x24 design grid. Returns None for
    unknown names so callers can simply skip the icon."""
    key = ("glyph", name, size, color, bg)

    def _draw(d, s):
        u = size * s / 24.0          # design-grid unit
        lw = max(1, int(1.7 * u))

        def L(*pts):
            d.line([(x * u, y * u) for x, y in pts], fill=color, width=lw,
                   joint="curve")

        if name == "mic":
            d.rounded_rectangle([9.5 * u, 3.5 * u, 14.5 * u, 13 * u],
                                radius=2.5 * u, outline=color, width=lw)
            d.arc([6.5 * u, 5 * u, 17.5 * u, 16.5 * u], start=0, end=180,
                  fill=color, width=lw)
            L((12, 16.5), (12, 19.5))
            L((8.5, 19.5), (15.5, 19.5))
        elif name == "update":
            L((12, 4), (12, 13))
            L((8.2, 9.8), (12, 13.6))
            L((15.8, 9.8), (12, 13.6))
            L((5, 15), (5, 19), (19, 19), (19, 15))
        elif name == "book":
            L((12, 7), (12, 18.5))
            L((12, 7), (9.5, 5.8), (5, 5.8), (5, 17), (9.5, 17), (12, 18.5))
            L((12, 7), (14.5, 5.8), (19, 5.8), (19, 17), (14.5, 17), (12, 18.5))
        elif name == "punct":
            # "A." — auto punctuation
            L((5.5, 17), (9.5, 6), (13.5, 17))
            L((7, 13), (12, 13))
            r = 1.6 * u
            d.ellipse([16.5 * u - r, 17 * u - r, 16.5 * u + r, 17 * u + r],
                      fill=color)
        elif name == "space":
            L((5.5, 10), (5.5, 15.5), (18.5, 15.5), (18.5, 10))
        elif name == "enter":
            L((18, 6), (18, 12.5), (8, 12.5))
            L((11.2, 9.3), (8, 12.5), (11.2, 15.7))
        elif name == "keyboard":
            d.rounded_rectangle([3 * u, 6.5 * u, 21 * u, 17.5 * u],
                                radius=2 * u, outline=color, width=lw)
            for x in (6.5, 10, 13.5, 17):
                r = 0.9 * u
                d.ellipse([x * u - r, 10 * u - r, x * u + r, 10 * u + r],
                          fill=color)
            L((8.5, 14.3), (15.5, 14.3))
        elif name == "captions":
            d.rounded_rectangle([3.5 * u, 6 * u, 20.5 * u, 18 * u],
                                radius=2.5 * u, outline=color, width=lw)
            L((6.5, 11.5), (13, 11.5))
            L((15, 11.5), (17.5, 11.5))
            L((6.5, 14.8), (9, 14.8))
            L((11, 14.8), (17.5, 14.8))
        elif name == "speaker":
            d.polygon([(5 * u, 10 * u), (8.8 * u, 10 * u), (12.8 * u, 6.4 * u),
                       (12.8 * u, 17.6 * u), (8.8 * u, 14 * u), (5 * u, 14 * u)],
                      fill=color)
            d.arc([11.5 * u, 8 * u, 19 * u, 16 * u], start=-55, end=55,
                  fill=color, width=lw)
            d.arc([12 * u, 5 * u, 23 * u, 19 * u], start=-50, end=50,
                  fill=color, width=lw)
        elif name == "person":
            r = 3.4 * u
            d.ellipse([12 * u - r, 8 * u - r, 12 * u + r, 8 * u + r],
                      outline=color, width=lw)
            d.arc([5 * u, 13.5 * u, 19 * u, 26 * u], start=180, end=360,
                  fill=color, width=lw)
        elif name == "zap":
            c = 12 * u
            pts = [(2, -8), (-4.7, 1), (-0.7, 1), (-1.7, 8), (4.7, -1), (0.7, -1)]
            d.polygon([(c + dx * u, c + dy * u) for dx, dy in pts], fill=color)
        elif name == "wand":
            # magic wand + sparkles — AI refine
            L((5.5, 18.5), (14, 10))
            for cx, cy, r in ((16.5, 7, 2.6), (10.5, 5, 1.5), (19, 12.5, 1.5)):
                L((cx - r, cy), (cx + r, cy))
                L((cx, cy - r), (cx, cy + r))
        elif name == "check":
            # copied-to-clipboard tick
            L((4.5, 12.5), (10, 18), (19.5, 6.5))
        elif name == "retry":
            # circular arrow — retry transcription
            d.arc([5 * u, 5 * u, 19 * u, 19 * u], start=-40, end=230,
                  fill=color, width=lw)
            d.polygon([(17.2 * u, 3.2 * u), (21.2 * u, 8.4 * u),
                       (14.8 * u, 8.8 * u)], fill=color)
        elif name == "download":
            L((12, 4), (12, 14))
            L((7.8, 10.2), (12, 14.4))
            L((16.2, 10.2), (12, 14.4))
            L((5, 16.5), (5, 19.5), (19, 19.5), (19, 16.5))
        elif name == "clipboard":
            # clipboard body + top clip + two text lines — copy to clipboard
            d.rounded_rectangle([6 * u, 5.5 * u, 18 * u, 20 * u],
                                radius=2 * u, outline=color, width=lw)
            d.rounded_rectangle([9.5 * u, 3.6 * u, 14.5 * u, 7 * u],
                                radius=1.2 * u, outline=color, width=lw)
            L((9, 12), (15, 12))
            L((9, 15.5), (15, 15.5))
        elif name == "clock":
            # clock face + two hands — time spent using the app
            d.ellipse([4.5 * u, 4.5 * u, 19.5 * u, 19.5 * u],
                      outline=color, width=lw)
            L((12, 12), (12, 7.5))               # hour hand (up)
            L((12, 12), (15.5, 13.5))            # minute hand
        elif name == "pen":
            # pencil at 45° — handwriting
            L((16, 6), (6.5, 15.5))              # upper long edge
            L((18, 8), (8.5, 17.5))              # lower long edge
            L((16, 6), (18, 8))                  # cap (top-right)
            d.polygon([(6.5 * u, 15.5 * u), (8.5 * u, 17.5 * u),
                       (4.5 * u, 19.5 * u)], fill=color)   # nib
        elif name == "camera":
            # camera body + viewfinder bump + lens — screenshots
            d.rounded_rectangle([4 * u, 8 * u, 20 * u, 18.5 * u],
                                radius=2 * u, outline=color, width=lw)
            L((9, 8), (10, 5.5), (14, 5.5), (15, 8))
            r = 3.0 * u
            d.ellipse([12 * u - r, 13.2 * u - r, 12 * u + r, 13.2 * u + r],
                      outline=color, width=lw)
        else:
            raise ValueError(f"unknown glyph {name!r}")

    try:
        return _photo(key, master, _draw, size, size)
    except Exception:
        return None


def icon_media(master, kind: str, size: int = 30, accent: str = "#f39200",
               fg: str = "#0d0d0d", bg: str = "#1a1a1a"):
    """History player button: accent-filled circle holding a play triangle or
    a stop square. Cached per bg — play/stop toggling is an image swap."""
    key = ("media", kind, size, accent, fg, bg)

    def _draw(d, s):
        u = size * s / 30.0
        d.ellipse([1 * u, 1 * u, 29 * u, 29 * u], fill=accent)
        if kind == "play":
            d.polygon([(12 * u, 9.3 * u), (12 * u, 20.7 * u),
                       (21.5 * u, 15 * u)], fill=fg)
        else:
            d.rounded_rectangle([10.5 * u, 10.5 * u, 19.5 * u, 19.5 * u],
                                radius=1.5 * u, fill=fg)

    return _photo(key, master, _draw, size, size)


def icon_doc(master, size: int = 20, color: str = "#777777",
             bg: str = "#1a1a1a"):
    """Small document/list glyph for the Today bar. Design box is 18px."""
    key = ("doc", size, color, bg)

    def _draw(d, s):
        u = size * s / 18.0
        lw = max(1, int(round(1.4 * u)))
        d.rounded_rectangle([3 * u, 1 * u, 15 * u, 17 * u], radius=3 * u,
                            outline=color, width=lw)
        d.line([6 * u, 7 * u, 12 * u, 7 * u], fill=color, width=lw)
        d.line([6 * u, 11 * u, 12 * u, 11 * u], fill=color, width=lw)

    return _photo(key, master, _draw, size, size)
