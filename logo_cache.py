"""
Shared logo image cache for FTC Whisper.

Loads logo.png from disk once (as a raw PIL image) and reuses the pixel data
across all windows. Each caller still gets its own PhotoImage bound to its own
Tk root, which is required by tkinter.
"""

import os
import threading
from typing import Optional

_pil_image = None
_lock = threading.Lock()
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_pil():
    """Load and cache the raw PIL RGBA image. Thread-safe."""
    global _pil_image
    with _lock:
        if _pil_image is None:
            try:
                from PIL import Image
                path = os.path.join(_BASE_DIR, "logo.png")
                if os.path.exists(path):
                    _pil_image = Image.open(path).convert("RGBA")
            except Exception:
                pass
    return _pil_image


def get_logo_photo(master, bg_color: str, max_w: int = 180, max_h: int = 60):
    """
    Return a PhotoImage of the logo sized to fit (max_w, max_h).
    Returns None if logo.png is missing or PIL is unavailable.

    master  — the tkinter widget that owns this PhotoImage (required for lifetime).
    bg_color — hex colour to composite against, e.g. "#4e4e4c".
    """
    img = _load_pil()
    if img is None:
        return None
    try:
        from PIL import Image, ImageTk
        thumb = img.copy()
        thumb.thumbnail((max_w, max_h), Image.LANCZOS)
        bg = Image.new("RGB", thumb.size, bg_color)
        bg.paste(thumb, mask=thumb.split()[3])
        return ImageTk.PhotoImage(bg, master=master)
    except Exception:
        return None
