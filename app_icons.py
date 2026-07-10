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

_ICON_SIZE = 26  # rendered size in the history row

# exe stem (lowercased) -> friendly display name
_KNOWN_APPS = {
    "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox",
    "brave": "Brave", "opera": "Opera", "opera_gx": "Opera GX", "arc": "Arc",
    "vivaldi": "Vivaldi",
    "code": "VS Code", "cursor": "Cursor", "devenv": "Visual Studio",
    "winword": "Word", "excel": "Excel", "powerpnt": "PowerPoint",
    "outlook": "Outlook", "olk": "Outlook", "onenote": "OneNote",
    "ms-teams": "Teams", "teams": "Teams",
    "slack": "Slack", "discord": "Discord", "telegram": "Telegram",
    "whatsapp": "WhatsApp", "signal": "Signal",
    "claude": "Claude", "chatgpt": "ChatGPT",
    "notepad": "Notepad", "notepad++": "Notepad++",
    "explorer": "File Explorer",
    "windowsterminal": "Terminal", "wt": "Terminal",
    "powershell": "PowerShell", "pwsh": "PowerShell", "cmd": "Command Prompt",
    "notion": "Notion", "obsidian": "Obsidian", "figma": "Figma",
    "zoom": "Zoom",
}

# Browser stems — their window title carries the site/tab name, which is a far
# better label than "Chrome" (e.g. "Claude", "Gmail").
_BROWSERS = {"chrome", "msedge", "firefox", "brave", "opera", "opera_gx",
             "arc", "vivaldi"}
# Suffixes browsers append to the page title
_BROWSER_SUFFIXES = (
    " - Google Chrome", " - Microsoft​ Edge", " - Microsoft Edge",
    " — Mozilla Firefox", " - Mozilla Firefox", " - Brave", " - Opera",
    " - Vivaldi", " - Arc",
)


def _exe_path_for_hwnd(hwnd: int) -> str:
    try:
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        pid = wt.DWORD(0)
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value
        finally:
            k32.CloseHandle(h)
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
    """'New chat - Claude - Google Chrome' -> 'Claude'.
    Sites put their name LAST in the title, so after stripping the browser
    suffix take the final ' - ' segment."""
    t = title
    for suf in _BROWSER_SUFFIXES:
        if t.endswith(suf):
            t = t[: -len(suf)]
            break
    t = t.strip()
    if " - " in t:
        t = t.rsplit(" - ", 1)[-1].strip()
    return t[:24]


def capture_app_info(hwnd: int) -> dict:
    """Resolve hwnd -> {'app_name': str, 'app_exe': str}. Never raises."""
    info = {"app_name": "", "app_exe": ""}
    if not hwnd:
        return info
    try:
        exe = _exe_path_for_hwnd(hwnd)
        info["app_exe"] = exe
        stem = ""
        if exe:
            stem = exe.rsplit("\\", 1)[-1].rsplit(".", 1)[0].lower()
        name = _KNOWN_APPS.get(stem, "")
        if stem in _BROWSERS:
            label = _browser_service_label(_window_title(hwnd))
            if label:
                name = label
        if not name and stem:
            name = stem.replace("_", " ").replace("-", " ").title()
        info["app_name"] = name
    except Exception:
        pass
    return info


# ── Icon extraction (HICON -> PIL -> tk PhotoImage) ──────────────────────────

_icon_cache: dict = {}   # (exe_lower, bg) -> PhotoImage | None
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


def _extract_exe_icon(exe_path: str):
    """exe path -> RGBA PIL image, or None."""
    try:
        SHGFI_ICON = 0x100
        info = _SHFILEINFOW()
        res = ctypes.windll.shell32.SHGetFileInfoW(
            exe_path, 0, ctypes.byref(info), ctypes.sizeof(info), SHGFI_ICON)
        if not res or not info.hIcon:
            return None
        try:
            return _hicon_to_pil(info.hIcon, 32)
        finally:
            ctypes.windll.user32.DestroyIcon(info.hIcon)
    except Exception:
        return None


def get_app_icon(exe_path: str, bg: str):
    """tk PhotoImage of the exe's icon composited onto bg. None on failure.
    Caller must keep a reference (tk requirement); the cache does that."""
    if not exe_path:
        return None
    key = (exe_path.lower(), bg)
    if key in _icon_cache:
        return _icon_cache[key]
    photo = None
    try:
        from PIL import Image, ImageTk
        src = _extract_exe_icon(exe_path)
        if src is not None:
            src = src.resize((_ICON_SIZE, _ICON_SIZE), Image.LANCZOS)
            base = Image.new("RGBA", src.size, bg)
            base.alpha_composite(src)
            photo = ImageTk.PhotoImage(base.convert("RGB"))
    except Exception:
        photo = None
    _icon_cache[key] = photo
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
    _fallback_cache[bg] = photo
    return photo
