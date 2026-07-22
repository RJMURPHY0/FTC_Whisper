"""
Target-app capture and icon extraction for the History tab.

capture_app_info(hwnd)  — resolve a window handle to {app_name, app_exe} at
                          recording time (title must be read NOW; browser tab
                          titles change constantly).
get_app_icon(exe, bg)   — extract the exe's shell icon as a tk PhotoImage
                          composited onto the given row background. Cached.
get_fallback_icon(bg)   — generic "text" tile for history rows recorded before
                          app capture existed.

All Win32 calls are best-effort: any failure returns empty info / None and the
History tab falls back to the generic tile.
"""

import ctypes
import ctypes.wintypes as wt
import os
import re
import sys

_ICON_SIZE = 36  # rendered size in the history row (fills the row height; the
                 # header's reduced pady keeps the row the same 44px tall)

# Base dir for bundled assets — sys._MEIPASS in a frozen build, else this file's
# folder (same pattern as logo_cache.py).
_BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
_BRAND_DIR = os.path.join(_BASE_DIR, "assets", "brand_icons")

# Explicit signature — ExtractIconExW has HICON* out-params that must be full
# pointer width on 64-bit Windows; without argtypes ctypes can mis-marshal the
# handles/count. Declared once at import (best-effort).
try:
    _ExtractIconExW = ctypes.windll.shell32.ExtractIconExW
    _ExtractIconExW.argtypes = [
        wt.LPCWSTR, ctypes.c_int,
        ctypes.POINTER(wt.HICON), ctypes.POINTER(wt.HICON), ctypes.c_uint,
    ]
    _ExtractIconExW.restype = ctypes.c_uint
except Exception:
    _ExtractIconExW = None

# PrivateExtractIconsW lets us request an EXACT pixel size — it picks the best
# embedded icon frame (modern exes ship 48px and 256px variants) and returns a
# handle at that size, so a 48px extraction stays crisp when downsampled to the
# 36px row icon. ExtractIconEx/SHGetFileInfo only give the 32px system size.
try:
    _PrivateExtractIconsW = ctypes.windll.user32.PrivateExtractIconsW
    _PrivateExtractIconsW.argtypes = [
        wt.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(wt.HICON), ctypes.POINTER(wt.UINT), wt.UINT, wt.DWORD,
    ]
    _PrivateExtractIconsW.restype = wt.UINT
except Exception:
    _PrivateExtractIconsW = None

# exe stem (lowercased) -> friendly display name
_KNOWN_APPS = {
    "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox",
    "brave": "Brave", "opera": "Opera", "opera_gx": "Opera GX", "arc": "Arc",
    "vivaldi": "Vivaldi", "zen": "Zen",
    "code": "VS Code", "code - insiders": "VS Code", "cursor": "Cursor",
    "devenv": "Visual Studio", "antigravity": "Antigravity",
    "antigravity ide": "Antigravity", "windsurf": "Windsurf",
    "trae": "Trae", "sublime_text": "Sublime Text", "idea64": "IntelliJ",
    "pycharm64": "PyCharm", "rider64": "Rider", "webstorm64": "WebStorm",
    "winword": "Word", "excel": "Excel", "powerpnt": "PowerPoint",
    "outlook": "Outlook", "olk": "Outlook", "onenote": "OneNote",
    "ms-teams": "Teams", "teams": "Teams",
    "slack": "Slack", "discord": "Discord", "telegram": "Telegram",
    "whatsapp": "WhatsApp", "whatsapp.root": "WhatsApp", "signal": "Signal",
    "claude": "Claude", "chatgpt": "ChatGPT",
    "notepad": "Notepad", "notepad++": "Notepad++",
    "explorer": "File Explorer",
    "windowsterminal": "Terminal", "wt": "Terminal",
    "powershell": "PowerShell", "pwsh": "PowerShell", "cmd": "Command Prompt",
    "notion": "Notion", "obsidian": "Obsidian", "figma": "Figma",
    "zoom": "Zoom", "spotify": "Spotify", "thunderbird": "Thunderbird",
    "acrobat": "Acrobat", "acrord32": "Acrobat Reader", "postman": "Postman",
    "sublime_merge": "Sublime Merge", "gitkraken": "GitKraken",
}

# Generic host processes that HOST another app's window — resolving their exe
# yields a useless label ("Application Frame Host"). The real app is a child
# window with a different PID (see _exe_path_for_hwnd).
_HOST_PROCESSES = {"applicationframehost"}

# Browser stems — their window title carries the site/tab name, which is a far
# better label than "Chrome" (e.g. "Claude", "Gmail").
_BROWSERS = {"chrome", "msedge", "firefox", "brave", "opera", "opera_gx",
             "arc", "vivaldi", "zen"}
# Suffixes browsers append to the page title
_BROWSER_SUFFIXES = (
    " - Google Chrome", " - Microsoft​ Edge", " - Microsoft Edge",
    " — Mozilla Firefox", " - Mozilla Firefox", " - Brave", " - Opera",
    " - Opera GX", " - Vivaldi", " - Arc", " - Zen Browser", " - Zen",
)

# Browser titles are presentation text, not identifiers. Resolve well-known
# services before shortening the remaining title so hyphen/em-dash variations
# (and titles such as "Ask Jack AI — #1 AI Auto") remain stable.
_BROWSER_SERVICES = (
    ("ask jack ai", "Ask Jack AI"), ("ask jack", "Ask Jack AI"),
    ("google chatgpt", "ChatGPT"), ("chat gpt", "ChatGPT"),
    ("chatgpt", "ChatGPT"), ("claude ai", "Claude"), ("claude", "Claude"),
    ("google gemini", "Gemini"), ("gemini", "Gemini"),
    ("microsoft copilot", "Copilot"), ("copilot", "Copilot"),
    ("perplexity ai", "Perplexity"), ("perplexity", "Perplexity"),
    ("google docs", "Google Docs"), ("google drive", "Google Drive"),
    ("google mail", "Gmail"), ("gmail", "Gmail"),
    ("outlook web", "Outlook"), ("outlook", "Outlook"),
    ("microsoft teams", "Teams"), ("teams", "Teams"),
    ("stack overflow", "Stack Overflow"), ("github", "GitHub"),
    ("gitlab", "GitLab"), ("linkedin", "LinkedIn"),
    ("whatsapp web", "WhatsApp"), ("whatsapp", "WhatsApp"),
    ("youtube", "YouTube"), ("reddit", "Reddit"),
    ("notion", "Notion"), ("figma", "Figma"), ("linear", "Linear"),
    ("slack", "Slack"), ("discord", "Discord"),
)
_GENERIC_BROWSER_TITLES = {
    "", "new tab", "new private tab", "new incognito tab", "start page",
    "about blank", "about:blank",
}
_BROWSER_PRODUCT_TITLES = {
    "google chrome", "microsoft edge", "mozilla firefox", "firefox",
    "brave", "brave browser", "opera", "opera gx", "vivaldi", "arc",
    "arc browser", "zen", "zen browser",
}
_TITLE_SEP_RE = re.compile(r"\s*(?:[-\u2010-\u2015]|[|•·])\s*")


def _pid_for_hwnd(hwnd: int) -> int:
    pid = wt.DWORD(0)
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _exe_for_pid(pid: int) -> str:
    if not pid:
        return ""
    k32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        k32.CloseHandle(h)
    return ""


def _stem_of(exe: str) -> str:
    if not exe:
        return ""
    return exe.rsplit("\\", 1)[-1].rsplit(".", 1)[0].lower()


# Callback type for EnumChildWindows
_ENUMCHILDPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def _resolve_host_child_pid(hwnd: int, host_pid: int) -> int:
    """UWP/Store windows are hosted by ApplicationFrameHost.exe; the real app runs
    in a child window with a DIFFERENT pid. Return that child's pid (0 if none)."""
    found = {"pid": 0}

    def _cb(child, _lparam):
        cpid = _pid_for_hwnd(child)
        if cpid and cpid != host_pid:
            found["pid"] = cpid
            return False  # stop enumeration
        return True

    try:
        ctypes.windll.user32.EnumChildWindows(hwnd, _ENUMCHILDPROC(_cb), 0)
    except Exception:
        pass
    return found["pid"]


def _exe_path_for_hwnd(hwnd: int) -> str:
    try:
        pid = _pid_for_hwnd(hwnd)
        if not pid:
            return ""
        exe = _exe_for_pid(pid)
        # UWP/Store apps: the foreground window belongs to the generic host — dig
        # into the child window that belongs to the real app.
        if _stem_of(exe) in _HOST_PROCESSES:
            child_pid = _resolve_host_child_pid(hwnd, pid)
            if child_pid:
                child_exe = _exe_for_pid(child_pid)
                if child_exe:
                    return child_exe
        return exe
    except Exception:
        pass
    return ""


def _window_title(hwnd: int) -> str:
    try:
        u32 = ctypes.windll.user32
        n = u32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 2)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value or ""
    except Exception:
        return ""


def _browser_service_label(title: str) -> str:
    """Resolve a browser title to a stable service/display label."""
    t = re.sub(r"[\u200b-\u200d\ufeff]", "", title or "").strip()
    for suf in _BROWSER_SUFFIXES:
        if t.casefold().endswith(suf.casefold()):
            t = t[: -len(suf)]
            break
    t = t.strip()
    segments = [part.strip() for part in _TITLE_SEP_RE.split(t) if part.strip()]

    def _words(value: str) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))

    # Product suffixes are not consistent about which dash they use. Removing
    # the final browser-name segment after tokenisation handles ASCII hyphens,
    # en/em dashes and vertical bars without maintaining every combination.
    if segments and _words(segments[-1]) in _BROWSER_PRODUCT_TITLES:
        segments.pop()
        if not segments:
            return ""

    # Search right-to-left because browsers conventionally place the service
    # name at the end, while still supporting "ChatGPT — release notes".
    for segment in reversed(segments or [t]):
        words = _words(segment)
        for alias, label in _BROWSER_SERVICES:
            if (words == alias or words.startswith(alias + " ")
                    or words.endswith(" " + alias)):
                return label

    candidate = (segments[-1] if segments else t).strip()
    if _words(candidate) in _GENERIC_BROWSER_TITLES:
        return ""
    return candidate[:24]


def capture_app_info(hwnd: int) -> dict:
    """Resolve hwnd -> {'app_name': str, 'app_exe': str}. Never raises."""
    info = {"app_name": "", "app_exe": ""}
    if not hwnd:
        return info
    try:
        exe = _exe_path_for_hwnd(hwnd)
        info["app_exe"] = exe
        stem = _stem_of(exe)
        name = _KNOWN_APPS.get(stem, "")
        if stem in _BROWSERS:
            label = _browser_service_label(_window_title(hwnd))
            if label:
                name = label
        if not name and stem:
            name = stem.replace("_", " ").replace("-", " ").title()
        info["app_name"] = name
    except Exception as e:
        print(f"[AppInfo] capture failed: {e}")
    return info


# ── Icon extraction (HICON -> PIL -> tk PhotoImage) ──────────────────────────

_raw_icon_cache: dict = {}  # executable signature -> successful RGBA PIL image
_icon_cache: dict = {}      # (executable signature, bg) -> successful PhotoImage
_fallback_cache: dict = {}  # bg -> PhotoImage


class _SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ("hIcon", wt.HICON),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", wt.DWORD),
        ("szDisplayName", ctypes.c_wchar * 260),
        ("szTypeName", ctypes.c_wchar * 80),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


def _hicon_to_pil(hicon, size: int = 32):
    """Draw an HICON into a 32-bit DIB and return an RGBA PIL image."""
    from PIL import Image
    u32 = ctypes.windll.user32
    gdi = ctypes.windll.gdi32
    hdc_screen = u32.GetDC(0)
    hdc = gdi.CreateCompatibleDC(hdc_screen)
    bmi = _BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.biWidth = size
    bmi.biHeight = -size  # top-down
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0
    bits = ctypes.c_void_p()
    hbmp = gdi.CreateDIBSection(hdc, ctypes.byref(bmi), 0,
                                ctypes.byref(bits), None, 0)
    img = None
    try:
        if hbmp and bits:
            old = gdi.SelectObject(hdc, hbmp)
            DI_NORMAL = 3
            u32.DrawIconEx(hdc, 0, 0, hicon, size, size, 0, 0, DI_NORMAL)
            gdi.SelectObject(hdc, old)
            raw = ctypes.string_at(bits, size * size * 4)
            img = Image.frombuffer("RGBA", (size, size), raw,
                                   "raw", "BGRA", 0, 1)
            # Icons without an alpha channel come back fully transparent —
            # treat any non-black pixel as opaque in that case.
            if img.getextrema()[3] == (0, 0):
                r, g, b, _ = img.split()
                mask = Image.eval(
                    Image.merge("RGB", (r, g, b)).convert("L"),
                    lambda v: 255 if v else 0)
                img.putalpha(mask)
    finally:
        if hbmp:
            gdi.DeleteObject(hbmp)
        gdi.DeleteDC(hdc)
        u32.ReleaseDC(0, hdc_screen)
    return img


def _extract_via_extracticon(exe_path: str):
    """exe path -> RGBA PIL image via ExtractIconExW (reads the exe's own icon
    resource directly). More reliable than the shell icon for Electron/Chromium
    exes (Antigravity, Cursor, Discord, VS Code). Returns None on failure."""
    if _ExtractIconExW is None:
        return None
    try:
        u32 = ctypes.windll.user32
        large = wt.HICON()
        small = wt.HICON()
        # ExtractIconExW(file, index, *large, *small, nIcons) -> icons extracted
        n = _ExtractIconExW(
            exe_path, 0, ctypes.byref(large), ctypes.byref(small), 1)
        hicon = large.value or small.value
        if not n or not hicon:
            return None
        try:
            return _hicon_to_pil(hicon, 32)
        finally:
            if large.value:
                u32.DestroyIcon(large)
            if small.value:
                u32.DestroyIcon(small)
    except Exception:
        return None


def _extract_via_private(exe_path: str, size: int = 48):
    """exe path -> RGBA PIL image via PrivateExtractIconsW at an exact size.
    Best crispness source for native exes. Returns None on failure."""
    if _PrivateExtractIconsW is None:
        return None
    try:
        hicon = wt.HICON()
        iconid = wt.UINT()
        n = _PrivateExtractIconsW(
            exe_path, 0, size, size,
            ctypes.byref(hicon), ctypes.byref(iconid), 1, 0)
        if not n or not hicon.value:
            return None
        try:
            return _hicon_to_pil(hicon, size)
        finally:
            ctypes.windll.user32.DestroyIcon(hicon)
    except Exception:
        return None


def _extract_exe_icon(exe_path: str):
    """exe path -> RGBA PIL image, or None."""
    # Prefer an exact 48px extraction (crispest when downscaled to the row size).
    img = _extract_via_private(exe_path, 48)
    if img is not None:
        return img
    try:
        SHGFI_ICON = 0x100
        SHGFI_LARGEICON = 0x0
        info = _SHFILEINFOW()
        res = ctypes.windll.shell32.SHGetFileInfoW(
            exe_path, 0, ctypes.byref(info), ctypes.sizeof(info),
            SHGFI_ICON | SHGFI_LARGEICON)
        if res and info.hIcon:
            try:
                img = _hicon_to_pil(info.hIcon, 32)
            finally:
                ctypes.windll.user32.DestroyIcon(info.hIcon)
            if img is not None:
                return img
        # Shell lookup gave nothing usable — pull the icon straight from the exe.
        return _extract_via_extracticon(exe_path)
    except Exception:
        # Last resort even if SHGetFileInfoW raised.
        return _extract_via_extracticon(exe_path)


def _exe_icon_cache_key(exe_path: str) -> tuple:
    """Path plus file signature, so an in-place app update gets a fresh icon."""
    path = os.path.normcase(os.path.normpath(exe_path or ""))
    try:
        stat = os.stat(exe_path)
        return path, stat.st_mtime_ns, stat.st_size
    except Exception:
        return path, None, None


def _get_raw_exe_icon(exe_path: str):
    """Extract once per executable version; failed attempts are never cached."""
    key = _exe_icon_cache_key(exe_path)
    cached = _raw_icon_cache.get(key)
    if cached is not None:
        return cached
    src = _extract_exe_icon(exe_path)
    if src is not None:
        _raw_icon_cache[key] = src
    return src


def get_app_icon(exe_path: str, bg: str):
    """tk PhotoImage of the exe's icon composited onto bg. None on failure.
    Caller must keep a reference (tk requirement); the cache does that."""
    if not exe_path:
        return None
    exe_key = _exe_icon_cache_key(exe_path)
    key = (exe_key, bg)
    if key in _icon_cache:
        return _icon_cache[key]
    photo = None
    try:
        from PIL import Image, ImageTk
        src = _get_raw_exe_icon(exe_path)
        if src is not None:
            src = src.resize((_ICON_SIZE, _ICON_SIZE), Image.LANCZOS)
            base = Image.new("RGBA", src.size, bg)
            base.alpha_composite(src)
            photo = ImageTk.PhotoImage(base.convert("RGB"))
    except Exception:
        photo = None
    # Do not permanently poison this path/background after a transient shell,
    # file-system or Tk initialisation failure. A later render gets to retry.
    if photo is not None:
        _icon_cache[key] = photo
    return photo


# ── Bundled brand icons (real app/service logos) ─────────────────────────────
# Highest-fidelity source: a curated pack of real favicons/logos keyed by the
# captured app_name. This is the ONLY way to show the correct icon for a
# browser-hosted web app (Claude, ChatGPT, Gemini, Gmail…) — there the captured
# exe is the browser, so exe-icon extraction can only ever yield the browser's
# icon. For native apps not in this pack, exe extraction still applies.
_brand_icon_cache: dict = {}  # (slug, bg) -> successful PhotoImage

# Rendering tunables for brand icons (see get_brand_icon):
_BRAND_TILE_OPAQUE = 0.72   # ≥ this opaque fraction ⇒ treat as full-bleed tile
_BRAND_RADIUS = 0.225       # corner radius as a fraction of the tile size
_BRAND_GLYPH_SCALE = 0.82   # floating glyphs occupy this fraction of the cell

# Textual app_name (lowercased) -> icon slug (a file assets/brand_icons/<slug>.png).
# Keys cover the names produced by _KNOWN_APPS values AND browser tab labels.
_BRAND_ALIASES = {
    "claude": "claude", "claude ai": "claude",
    "chatgpt": "chatgpt", "chat gpt": "chatgpt", "openai": "openai",
    "gemini": "gemini", "google gemini": "gemini", "bard": "gemini",
    "copilot": "copilot", "microsoft copilot": "copilot",
    "perplexity": "perplexity", "perplexity ai": "perplexity",
    "antigravity": "antigravity", "google antigravity": "antigravity",
    "cursor": "cursor",
    "notion": "notion",
    "slack": "slack",
    "discord": "discord",
    "telegram": "telegram",
    "whatsapp": "whatsapp", "whatsapp web": "whatsapp",
    "gmail": "gmail", "google mail": "gmail",
    "outlook": "outlook", "outlook web": "outlook",
    "teams": "teams", "microsoft teams": "teams",
    "github": "github",
    "gitlab": "gitlab",
    "linear": "linear",
    "figma": "figma",
    "spotify": "spotify",
    "youtube": "youtube",
    "reddit": "reddit",
    "linkedin": "linkedin",
    "stack overflow": "stackoverflow", "stackoverflow": "stackoverflow",
    "zoom": "zoom",
    "trello": "trello",
    "asana": "asana",
    "atlassian": "atlassian", "jira": "atlassian", "confluence": "atlassian",
    "google docs": "docs", "docs": "docs",
    "google drive": "drive", "drive": "drive",
}

# Slugs we actually shipped a PNG for (guards a name mapping to a missing file).
try:
    _BRAND_SLUGS = {f[:-4] for f in os.listdir(_BRAND_DIR) if f.endswith(".png")}
except Exception:
    _BRAND_SLUGS = set()


def _brand_slug(app_name: str) -> str:
    """Map a captured app_name to a bundled icon slug, or '' if none."""
    n = (app_name or "").strip().lower()
    if not n:
        return ""
    slug = _BRAND_ALIASES.get(n, "")
    if slug and slug in _BRAND_SLUGS:
        return slug
    # Fall back to a compacted form matching a shipped file directly
    # (e.g. "Cursor" -> "cursor", "GitLab" -> "gitlab").
    compact = re.sub(r"[^a-z0-9]", "", n)
    if compact in _BRAND_SLUGS:
        return compact
    return ""


def get_brand_icon(app_name: str, bg: str):
    """Real bundled logo for a known app/service, composited onto bg. None if the
    app isn't in the curated pack. Caller keeps the reference; the cache does."""
    slug = _brand_slug(app_name)
    if not slug:
        return None
    key = (slug, bg)
    if key in _brand_icon_cache:
        return _brand_icon_cache[key]
    photo = None
    try:
        from PIL import Image, ImageDraw, ImageChops, ImageTk
        big = _ICON_SIZE * 4  # render 4x, downsample for smooth corners/edges
        src = Image.open(os.path.join(_BRAND_DIR, slug + ".png")).convert("RGBA")
        # Two visually distinct favicon shapes need different handling so every
        # row reads at a CONSISTENT optical size:
        #   • full-bleed colour tile (Claude, Outlook, ChatGPT) → fill the cell
        #     and round the corners (iOS/app-icon look).
        #   • floating glyph on transparency (Antigravity, Gemini, Slack) →
        #     trim to the mark, then scale it to a fixed fraction of the cell so
        #     a tall mark (Antigravity's "A") no longer towers over the tiles.
        alpha = src.split()[3]
        opaque_frac = alpha.histogram()[255] / float(src.size[0] * src.size[1])
        canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        if opaque_frac >= _BRAND_TILE_OPAQUE:
            tile = src.resize((big, big), Image.LANCZOS)
            mask = Image.new("L", (big, big), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                [0, 0, big - 1, big - 1], radius=int(big * _BRAND_RADIUS), fill=255)
            r, g, b, a = tile.split()
            tile.putalpha(ImageChops.multiply(a, mask))
            canvas.alpha_composite(tile)
        else:
            bb = src.getbbox()
            if bb:
                src = src.crop(bb)
            w, h = src.size
            scale = (_BRAND_GLYPH_SCALE * big) / max(w, h)
            nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
            glyph = src.resize((nw, nh), Image.LANCZOS)
            canvas.alpha_composite(glyph, ((big - nw) // 2, (big - nh) // 2))
        icon = canvas.resize((_ICON_SIZE, _ICON_SIZE), Image.LANCZOS)
        base = Image.new("RGBA", icon.size, bg)
        base.alpha_composite(icon)
        photo = ImageTk.PhotoImage(base.convert("RGB"))
    except Exception:
        photo = None
    if photo is not None:
        _brand_icon_cache[key] = photo
    return photo


_monogram_cache: dict = {}  # (normalised full app name, bg) -> PhotoImage | None

# Stable, pleasant tile colours picked by hashing the app name — so "Claude"
# is always the same colour, "Outlook" another, etc.
_MONOGRAM_COLORS = [
    "#f39200",  # FTC orange
    "#4a7edb", "#3fae6f", "#c85c9e", "#d9534f", "#6f6fd6",
    "#2fa8a8", "#e0a52e", "#8a6fd6", "#5c8a3f", "#d67f4a",
]


def _monogram_cache_key(app_name: str, bg: str) -> tuple[str, str]:
    return (app_name or "").strip().casefold(), bg


def get_monogram_icon(app_name: str, bg: str):
    """Coloured rounded tile with the app's initial(s) — used when the real exe
    icon can't be extracted but we DO know the app name (e.g. 'Claude'). Keeps
    every history row visually identifiable instead of showing a generic tile.
    None on failure. Caller keeps the reference; the cache does that."""
    name = (app_name or "").strip()
    if not name:
        return None
    letter = name[0].upper()
    key = _monogram_cache_key(name, bg)
    if key in _monogram_cache:
        return _monogram_cache[key]
    photo = None
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageTk
        # Deterministic colour from the name (no randomness — same app, same hue).
        color = _MONOGRAM_COLORS[sum(ord(c) for c in name) % len(_MONOGRAM_COLORS)]
        s = _ICON_SIZE * 4  # draw 4x, downsample for smooth corners/text
        img = Image.new("RGBA", (s, s), bg)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, s - 1, s - 1], radius=s // 4, fill=color)
        font = None
        for _fname in ("segoeui.ttf", "arialbd.ttf", "arial.ttf"):
            try:
                font = ImageFont.truetype(_fname, int(s * 0.55))
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()
        try:
            bbox = d.textbbox((0, 0), letter, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            tx = (s - tw) / 2 - bbox[0]
            ty = (s - th) / 2 - bbox[1]
        except Exception:
            tw, th = d.textsize(letter, font=font)  # Pillow <8 fallback
            tx, ty = (s - tw) / 2, (s - th) / 2
        d.text((tx, ty), letter, fill="#ffffff", font=font)
        img = img.resize((_ICON_SIZE, _ICON_SIZE), Image.LANCZOS)
        photo = ImageTk.PhotoImage(img.convert("RGB"))
    except Exception:
        photo = None
    if photo is not None:
        _monogram_cache[key] = photo
    return photo


def get_fallback_icon(bg: str):
    """Generic rounded 'text lines' tile for rows with no captured app."""
    if bg in _fallback_cache:
        return _fallback_cache[bg]
    photo = None
    try:
        from PIL import Image, ImageDraw, ImageTk
        s = _ICON_SIZE * 4  # draw 4x, downsample for smooth corners
        img = Image.new("RGBA", (s, s), bg)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, s - 1, s - 1], radius=s // 4,
                            fill="#262626")
        lw = s // 10
        x0 = s // 4
        d.rounded_rectangle([x0, int(s * 0.30), int(s * 0.75), int(s * 0.30) + lw],
                            radius=lw // 2, fill="#f39200")
        d.rounded_rectangle([x0, int(s * 0.48), int(s * 0.66), int(s * 0.48) + lw],
                            radius=lw // 2, fill="#8a8a8a")
        d.rounded_rectangle([x0, int(s * 0.66), int(s * 0.71), int(s * 0.66) + lw],
                            radius=lw // 2, fill="#8a8a8a")
        img = img.resize((_ICON_SIZE, _ICON_SIZE), Image.LANCZOS)
        photo = ImageTk.PhotoImage(img.convert("RGB"))
    except Exception:
        photo = None
    if photo is not None:
        _fallback_cache[bg] = photo
    return photo
