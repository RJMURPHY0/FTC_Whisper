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
        if not isinstance(bg, str):
            raise TypeError("cache key must end with the bg colour, got %r" % (bg,))
        base = Image.new("RGB", (w, h), bg)
        base.paste(img, (0, 0), img)
        photo = ImageTk.PhotoImage(base, master=master)
    except Exception:
        return None
    _cache[key] = photo
    return photo


def _photo_im(key, master, draw_fn, w: int, h: int):
    """As `_photo`, but hands draw_fn the RGBA Image rather than an ImageDraw —
    for surfaces that composite layers (blur, gradients) instead of stroking."""
    try:
        ckey = (id(master.tk),) + tuple(key)
    except Exception:
        ckey = tuple(key)
    cached = _cache.get(ckey)
    if cached is not None:
        return cached
    if len(_cache) > _CACHE_MAX:
        _cache.clear()
    try:
        from PIL import Image, ImageTk
        img = Image.new("RGBA", (w * _SS, h * _SS), (0, 0, 0, 0))
        draw_fn(img, _SS)
        img = img.resize((w, h), Image.LANCZOS)
        bg = key[-1]
        if not isinstance(bg, str):
            raise TypeError("cache key must end with the bg colour, got %r" % (bg,))
        base = Image.new("RGB", (w, h), bg)
        base.paste(img, (0, 0), img)
        photo = ImageTk.PhotoImage(base, master=master)
    except Exception:
        return None
    _cache[ckey] = photo
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


def keycap(master, w: int, h: int, r: int = 6, face: str = "#242424",
           border: str = "#2d2d2d", bg: str = "#1a1a1a", pressed: bool = False):
    """A physical-looking keyboard key: gradient face, hairline border, an
    inset highlight along the top and a darker lip along the bottom so the cap
    reads as a key rather than as a chip. Same visual family as the glass
    buttons — one raised surface language across the tab."""
    if w <= 0 or h <= 0:
        return None
    key = ("keycap", w, h, r, face, border, pressed, bg)

    def _draw(img, s):
        from PIL import Image, ImageDraw, ImageFilter

        rad = r * s
        pad = 2 * s
        x0, y0, x1, y1 = pad, pad, w * s - pad, h * s - pad - (0 if pressed else s)
        cw, ch = int(x1 - x0), int(y1 - y0)
        if cw < 2 or ch < 2:
            return

        # Shadow under the cap — a key sits proud of the surface.
        if not pressed:
            sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(sh).rounded_rectangle(
                [x0, y0 + 2 * s, x1, y1 + 2 * s], radius=rad,
                fill=(0, 0, 0, 120))
            img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(1.6 * s)))

        top = _mix(face, "#ffffff", 0.04 if pressed else 0.12)
        bot = _mix(face, "#000000", 0.10 if pressed else 0.0)
        t_rgb, b_rgb = _rgb(top), _rgb(bot)
        col = Image.new("RGB", (1, ch))
        px = col.load()
        for y in range(ch):
            k = y / max(1, ch - 1)
            px[0, y] = tuple(int(round(t_rgb[i] + (b_rgb[i] - t_rgb[i]) * k))
                             for i in range(3))
        mask = Image.new("L", (cw, ch), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, cw - 1, ch - 1],
                                               radius=rad, fill=255)
        cap = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        cap.paste(col.resize((cw, ch), Image.NEAREST), (0, 0), mask)
        cd = ImageDraw.Draw(cap)
        if not pressed:
            cd.line([(rad // 2, s), (cw - rad // 2, s)],
                    fill=(255, 255, 255, 30), width=max(1, s))
            cd.line([(rad // 2, ch - 1 - s), (cw - rad // 2, ch - 1 - s)],
                    fill=(0, 0, 0, 110), width=max(1, s))
        cap.putalpha(Image.composite(cap.getchannel("A"),
                                     Image.new("L", (cw, ch), 0), mask))
        img.alpha_composite(cap, (int(x0), int(y0)))
        ImageDraw.Draw(img).rounded_rectangle(
            [x0, y0, x1 - 1, y1 - 1], radius=rad,
            outline=_rgb(_mix(border, "#ffffff", 0.12)) + (255,),
            width=max(1, s))

    return _photo_im(key, master, _draw, w, h)


def help_dot(master, size: int = 15, color: str = "#777777",
             bg: str = "#1a1a1a", filled: bool = False):
    """The small "?" bubble beside a label. Outline at rest, filled on hover —
    a colour change alone is too small a signal at 15px."""
    if size <= 0:
        return None
    key = ("helpdot", size, color, filled, bg)

    def _draw(d, s):
        box = [s, s, size * s - 1 - s, size * s - 1 - s]
        lw = max(1, int(size * s / 14.0))
        if filled:
            d.ellipse(box, fill=color)
        else:
            d.ellipse(box, outline=color, width=lw)
        ink = bg if filled else color
        u = size * s / 15.0
        # "?" drawn as strokes: a PIL-rendered glyph at 15px is mush, and the
        # question mark has to stay legible at exactly this size.
        d.arc([4.6 * u, 3.4 * u, 10.4 * u, 9.0 * u], start=170, end=20,
              fill=ink, width=lw)
        d.line([(8.4 * u, 7.6 * u), (7.5 * u, 9.9 * u)], fill=ink, width=lw)
        r = max(1, int(0.85 * u))
        d.ellipse([7.5 * u - r, 11.6 * u - r, 7.5 * u + r, 11.6 * u + r],
                  fill=ink)

    return _photo(key, master, _draw, size, size)


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


class _Offset:
    """ImageDraw proxy that shifts every coordinate by (ox, oy).

    Lets the glyph painters below be reused inside a larger canvas (the icon
    badge) without threading an offset through every drawing call.
    """

    _SHIFT = ("line", "rectangle", "rounded_rectangle", "ellipse", "arc",
              "polygon", "pieslice", "chord")

    def __init__(self, d, ox: float, oy: float):
        self._d, self._ox, self._oy = d, ox, oy

    def __getattr__(self, name):
        fn = getattr(self._d, name)
        if name not in self._SHIFT:
            return fn
        ox, oy = self._ox, self._oy

        def _wrapped(xy, *a, **kw):
            if xy and isinstance(xy[0], (int, float)):
                xy = [v + (ox if k % 2 == 0 else oy) for k, v in enumerate(xy)]
            else:
                xy = [(x + ox, y + oy) for x, y in xy]
            return fn(xy, *a, **kw)

        return _wrapped


def _glyph_paint(d, s, name: str, size: int, color: str) -> None:
    """Paint one 24x24-grid glyph. Raises ValueError for an unknown name."""
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
    elif name == "home":
        # house outline with a doorway — the Home tab
        L((3.5, 11.5), (12, 4.5), (20.5, 11.5))
        L((5.8, 10.2), (5.8, 19.5), (18.2, 19.5), (18.2, 10.2))
        L((9.8, 19.5), (9.8, 14), (14.2, 14), (14.2, 19.5))
    elif name == "history":
        # Clock with a counter-clockwise rewind head — the History tab.
        # Nearly the full ring (one notch at 10 o'clock for the head to sit
        # in) because a big gap reads as a clipped icon, not as an arrow.
        d.arc([5 * u, 5 * u, 19 * u, 19 * u], start=250, end=205 + 360,
              fill=color, width=lw)
        # Head at the notch, pointing anticlockwise (down and to the left).
        hx, hy, sz = 9.6, 5.6, 2.9
        d.polygon([(hx * u, (hy - sz * 0.75) * u),
                   ((hx - sz) * u, (hy + sz * 0.55) * u),
                   ((hx + sz * 0.55) * u, (hy + sz * 0.9) * u)], fill=color)
        L((12, 12), (12, 8.3))               # hour hand
        L((12, 12), (15, 13.5))              # minute hand
    elif name == "save":
        # floppy: body, shutter slot, label panel — the Save button
        d.rounded_rectangle([4 * u, 4.5 * u, 20 * u, 19.5 * u],
                            radius=2 * u, outline=color, width=lw)
        L((17.5, 4.5), (20, 7))              # clipped corner
        d.rounded_rectangle([8 * u, 4.5 * u, 16 * u, 10 * u],
                            radius=0.8 * u, outline=color, width=lw)
        L((13.5, 6.4), (13.5, 8.2))          # shutter
        d.rounded_rectangle([7 * u, 13 * u, 17 * u, 19.5 * u],
                            radius=0.8 * u, outline=color, width=lw)
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



def icon_glyph(master, name: str, size: int = 20, color: str = "#f39200",
               bg: str = "#1a1a1a"):
    """Small settings glyphs, drawn in a 24x24 design grid. Returns None for
    unknown names so callers can simply skip the icon."""
    key = ("glyph", name, size, color, bg)

    def _draw(d, s):
        _glyph_paint(d, s, name, size, color)

    try:
        return _photo(key, master, _draw, size, size)
    except Exception:
        return None


def icon_badge(master, name: str, glyph: int = 20, box: int = 36,
               radius: int = 11, color: str = "#f39200",
               badge: str = "#3d2600", bg: str = "#1a1a1a",
               circle: bool = True, ring: float = 0.30):
    """A glyph sitting in a tinted badge — the card-header treatment. A disc by
    default, with a faint ring of the accent around it so the badge reads as a
    deliberate token rather than a coloured blob. Returns None for an unknown
    glyph so callers can skip it."""
    # NB bg stays LAST: _photo composites onto key[-1].
    key = ("iconbadge", name, glyph, box, radius, color, badge,
           circle, round(ring, 2), bg)

    def _draw(d, s):
        box_px = [0, 0, box * s - 1, box * s - 1]
        if circle:
            d.ellipse(box_px, fill=badge)
            if ring > 0:
                d.ellipse(box_px, outline=_mix(badge, color, ring),
                          width=max(1, s))
        else:
            d.rounded_rectangle(box_px, radius=radius * s, fill=badge)
        off = (box - glyph) / 2.0 * s
        _glyph_paint(_Offset(d, off, off), s, name, glyph, color)

    try:
        return _photo(key, master, _draw, box, box)
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


# ─────────────────────────────────────────────────────────────────────────────
# Glass button — a port of Brightlink's `.glass-button` (CRM src/index.css).
#
# The CSS is the reference and every value here traces back to it: a raised
# card-gradient face, a hairline border a shade lighter than the card, a soft
# drop shadow, an inset top highlight, and on hover a 1.5px lift with a
# brighter face, a lighter border and a deeper shadow. Press drops it back
# flush and swaps the drop shadow for an inset one. `sheen` is the specular
# band that sweeps across on hover (CSS: 105deg, translateX -130% → 130%).
#
# The face never fills the whole canvas: PAD_T leaves room for the hover lift
# and PAD_B/PAD_X for the shadow spread, so a state change is a pure image
# swap with no widget resize (a resize mid-hover reflows the row).
# ─────────────────────────────────────────────────────────────────────────────

GLASS_PAD_X = 4
GLASS_PAD_T = 4
GLASS_PAD_B = 8
_GLASS_LIFT = 2          # CSS translateY(-1.5px), rounded to whole pixels


def _rgb(c: str):
    c = c.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def _mix(a: str, b: str, t: float) -> str:
    """Blend hex `a` toward hex `b` by t (0..1) — CSS color-mix, in sRGB."""
    ar, ag, ab = _rgb(a)
    br, bg_, bb = _rgb(b)
    return "#%02x%02x%02x" % (
        int(round(ar + (br - ar) * t)),
        int(round(ag + (bg_ - ag) * t)),
        int(round(ab + (bb - ab) * t)),
    )


#: Public alias — call sites tint surfaces toward the accent (icon badges).
mix = _mix


def glass_button(master, w: int, h: int, state: str = "rest",
                 r: int = 8, card: str = "#1a1a1a", border: str = "#2d2d2d",
                 accent: str = "", sheen: float = -1.0, bg: str = "#0d0d0d",
                 glyph: str = "", glyph_size: int = 14,
                 glyph_color: str = "#ffffff", glyph_x: int = 0):
    """One glass-button surface image.

    state: 'rest' | 'hover' | 'press'. `accent` set gives the CSS
    `[data-active="true"]` look (pressed in, tinted toward the accent).
    `sheen` is the sweep phase 0..1; anything < 0 draws no band.

    A leading `glyph` is painted INTO the face rather than laid over it as a
    second canvas image: ui_render composites every glyph onto one flat colour,
    so an icon dropped on top of the gradient would carry a visible patch of
    the wrong tone. It rides the hover lift with the rest of the face.
    """
    if w <= 0 or h <= 0:
        return None
    ph = round(max(0.0, min(1.0, sheen)), 3) if sheen >= 0 else -1.0
    # NB bg stays LAST: _photo_im composites onto key[-1].
    key = ("glass", w, h, state, r, card, border, accent, ph,
           glyph, glyph_size, glyph_color, glyph_x, bg)

    def _draw(img, s):
        from PIL import Image, ImageDraw, ImageFilter

        active = bool(accent)
        hover = state == "hover"
        press = state == "press"

        fx0, fx1 = GLASS_PAD_X * s, (w - GLASS_PAD_X) * s
        lift = _GLASS_LIFT if (hover and not press and not active) else 0
        fy0 = (GLASS_PAD_T - lift) * s
        fy1 = (h - GLASS_PAD_B - lift) * s
        fw, fh = int(fx1 - fx0), int(fy1 - fy0)
        if fw < 2 or fh < 2:
            return
        rad = r * s

        # ── Drop shadow ──────────────────────────────────────────────────────
        # Pressed states (click, and the active/selected look) carry no drop
        # shadow in the CSS: they sit IN the surface, not above it.
        if not press and not active:
            blur = (7 if hover else 3) * s
            drop = (6 if hover else 2) * s
            alpha = 128 if hover else 112
            sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(sh).rounded_rectangle(
                [fx0, fy0 + drop, fx1, fy1 + drop], radius=rad,
                fill=(0, 0, 0, alpha))
            img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(blur / 2.0)))

        # ── Face: vertical gradient, clipped to the rounded rect ─────────────
        if active:
            top, bot = _mix(card, accent, 0.16), _mix(bg, accent, 0.09)
        elif hover:
            top, bot = _mix(card, "#ffffff", 0.15), _mix(card, "#ffffff", 0.03)
        else:
            top, bot = _mix(card, "#ffffff", 0.08), card
        t_rgb, b_rgb = _rgb(top), _rgb(bot)
        col = Image.new("RGB", (1, fh))
        px = col.load()
        for y in range(fh):
            k = y / max(1, fh - 1)
            px[0, y] = tuple(int(round(t_rgb[i] + (b_rgb[i] - t_rgb[i]) * k))
                             for i in range(3))
        mask = Image.new("L", (fw, fh), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, fw - 1, fh - 1],
                                               radius=rad, fill=255)
        face = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
        face.paste(col.resize((fw, fh), Image.NEAREST), (0, 0), mask)
        fd = ImageDraw.Draw(face)
        inset = max(1, s)

        # Inset top highlight — CSS `inset 0 1px 0 rgba(255,255,255,.06/.12)`.
        if not active:
            fd.line([(rad // 2, inset), (fw - rad // 2, inset)],
                    fill=(255, 255, 255, 34 if hover else 16), width=inset)

        # Pressed / active inset shadow, faded over a few rows.
        if press or active:
            depth = int((5 if press else 4) * s)
            for i in range(depth):
                fd.line([(rad // 3, i), (fw - rad // 3, i)],
                        fill=(0, 0, 0, int(115 * (1 - i / max(1.0, depth)))),
                        width=1)

        # ── Specular sheen: 105deg band sweeping left → right ────────────────
        # CSS: linear-gradient(105deg, transparent 35%, rgba(255,255,255,.14)
        # 48%, rgba(255,255,255,.03) 56%, transparent 70%), translateX
        # -130% → 130%. Built as a real alpha ramp and sheared row by row —
        # a few flat polygons instead read as hard stripes.
        if ph >= 0.0:
            stops = ((0.35, 0), (0.48, 36), (0.56, 8), (0.70, 0))
            strip = Image.new("L", (fw, 1), 0)
            sp = strip.load()
            for x in range(fw):
                t = x / float(fw)
                a = 0
                for (t0, a0), (t1, a1) in zip(stops, stops[1:]):
                    if t0 <= t <= t1:
                        k = (t - t0) / max(1e-6, t1 - t0)
                        a = int(round(a0 + (a1 - a0) * k))
                        break
                sp[x, 0] = a
            shift = (-1.3 + 2.6 * ph) * fw
            skew = fh * 0.27                     # the 105deg lean
            layer = Image.new("L", (fw, fh), 0)
            for y in range(fh):
                dx = int(round(shift + (0.5 - y / float(fh)) * 2 * skew))
                layer.paste(strip, (dx, y))
            band = Image.new("RGBA", (fw, fh), (255, 255, 255, 0))
            band.putalpha(layer)
            face.alpha_composite(band)
            # The band spills past the rounded corners — clip it back.
            face.putalpha(Image.composite(face.getchannel("A"),
                                          Image.new("L", (fw, fh), 0), mask))

        img.alpha_composite(face, (int(fx0), int(fy0)))

        # ── Leading glyph, painted on the face so it rides the lift ──────────
        if glyph:
            gx = glyph_x * s
            gy = (fy0 + fy1) / 2.0 - glyph_size * s / 2.0
            try:
                _glyph_paint(_Offset(ImageDraw.Draw(img), gx, gy), s,
                             glyph, glyph_size, glyph_color)
            except ValueError:
                pass

        # ── Border ───────────────────────────────────────────────────────────
        if active:
            bcol = _rgb(_mix(card, accent, 0.55)) + (255,)
        elif hover:
            bcol = _rgb(_mix(border, "#ffffff", 0.10)) + (255,)
        else:
            bcol = _rgb(_mix(border, "#ffffff", 0.04)) + (255,)
        ImageDraw.Draw(img).rounded_rectangle(
            [fx0, fy0, fx1 - 1, fy1 - 1], radius=rad, outline=bcol,
            width=max(1, s))

    return _photo_im(key, master, _draw, w, h)
