"""
Text injector — sends transcribed text into the focused window.

Injection strategy (chosen by foreground window class):
  Native Win32 apps (Outlook, Word, Notepad, etc.):
    → PostMessage(WM_CHAR) directly to the focused child HWND.
      WM_CHAR carries only the character code — modifier key state
      (Alt, Ctrl, Shift) is completely irrelevant. This is why streaming
      injection while Alt is held never triggers Paste Special.

  Browsers (Chrome, Edge, Firefox, Electron):
    → SendInput / KEYEVENTF_UNICODE (VK_PACKET). Browsers don't process
      WM_CHAR from external processes; VK_PACKET works correctly here.

  Fallback: clipboard + raw keybd_event Ctrl+V.

_release_modifiers() is called only for the FINAL (post-recording)
injection, never during streaming — releasing modifiers while the
hotkey is still physically held causes its own problems.
"""

import ctypes
import ctypes.wintypes
import threading
import time


# ── Win32 constants ───────────────────────────────────────────────────────────

_INPUT_KEYBOARD    = 1
_KEYEVENTF_UNICODE = 0x0004
_KEYEVENTF_KEYUP   = 0x0002
_WM_CHAR           = 0x0102

# Window classes that use Chromium / Gecko rendering — need VK_PACKET
BROWSER_CLASSES = frozenset({
    "Chrome_WidgetWin_1",   # Chrome, Edge, Brave, Electron, VS Code
    "MozillaWindowClass",   # Firefox
    "MozillaDialogClass",
})

# Modifier VK codes — released before final injection only
_MODIFIERS = (
    0x10, 0x11, 0x12,       # Shift, Ctrl, Alt
    0x5B, 0x5C,             # LWin, RWin
    0xA0, 0xA1,             # LShift, RShift
    0xA2, 0xA3,             # LCtrl, RCtrl
    0xA4, 0xA5,             # LAlt, RAlt
)


# ── Win32 SendInput structures ────────────────────────────────────────────────

class _KbdInput(ctypes.Structure):
    _fields_ = [
        ("wVk",         ctypes.c_ushort),
        ("wScan",       ctypes.c_ushort),
        ("dwFlags",     ctypes.c_ulong),
        ("time",        ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_uint64),
    ]


class _Input(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KbdInput), ("_pad", ctypes.c_byte * 28)]
    _anonymous_ = ("_u",)
    _fields_    = [("type", ctypes.c_ulong), ("_u", _U)]


# ── Window detection ──────────────────────────────────────────────────────────

def _get_fg_class() -> str:
    """Return the Win32 class name of the current foreground window."""
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(128)
    ctypes.windll.user32.GetClassNameW(hwnd, buf, 128)
    return buf.value


def _get_focused_child(fg_hwnd: int) -> int:
    """
    Return the focused child HWND inside the foreground window.
    Uses AttachThreadInput so GetFocus() returns the correct control
    (e.g. the text editor inside an Outlook compose window).
    """
    u32     = ctypes.windll.user32
    k32     = ctypes.windll.kernel32
    fg_tid  = u32.GetWindowThreadProcessId(fg_hwnd, None)
    our_tid = k32.GetCurrentThreadId()
    u32.AttachThreadInput(our_tid, fg_tid, True)
    focused = u32.GetFocus()
    u32.AttachThreadInput(our_tid, fg_tid, False)
    return focused or fg_hwnd


# ── Injection methods ─────────────────────────────────────────────────────────

def _post_wm_char(text: str) -> bool:
    """
    Inject text by posting WM_CHAR messages directly to the focused child HWND.

    This is the safest method for native Windows apps (Outlook, Word, etc.):
    - WM_CHAR carries only the character code — modifier state is irrelevant
    - No clipboard involvement — no Paste Special dialog
    - Works while the hotkey modifiers (Alt, Ctrl) are still physically held
    - Handles surrogate pairs for emoji / extended Unicode
    """
    u32 = ctypes.windll.user32
    fg  = u32.GetForegroundWindow()
    if not fg:
        return False
    target = _get_focused_child(fg)
    for ch in text:
        code = ord(ch)
        if code > 0xFFFF:
            code -= 0x10000
            u32.PostMessageW(target, _WM_CHAR, 0xD800 | (code >> 10), 1)
            u32.PostMessageW(target, _WM_CHAR, 0xDC00 | (code & 0x3FF), 1)
        else:
            u32.PostMessageW(target, _WM_CHAR, code, 1)
    return True


def _send_unicode(text: str) -> bool:
    """
    Inject via SendInput / KEYEVENTF_UNICODE (VK_PACKET) in one batched call.
    Used for browsers which don't process external WM_CHAR.
    Returns True if SendInput reported all events were sent.
    """
    if not text:
        return True

    events: list[_Input] = []
    for ch in text:
        code = ord(ch)
        if code > 0xFFFF:
            code -= 0x10000
            high = 0xD800 | (code >> 10)
            low  = 0xDC00 | (code & 0x3FF)
            for sc in (high, low):
                events.append(_Input(type=_INPUT_KEYBOARD,
                                     ki=_KbdInput(wScan=sc, dwFlags=_KEYEVENTF_UNICODE)))
                events.append(_Input(type=_INPUT_KEYBOARD,
                                     ki=_KbdInput(wScan=sc, dwFlags=_KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP)))
        else:
            events.append(_Input(type=_INPUT_KEYBOARD,
                                 ki=_KbdInput(wScan=code, dwFlags=_KEYEVENTF_UNICODE)))
            events.append(_Input(type=_INPUT_KEYBOARD,
                                 ki=_KbdInput(wScan=code, dwFlags=_KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP)))

    arr  = (_Input * len(events))(*events)
    sent = ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(_Input))
    return sent == len(events)


def _release_modifiers() -> None:
    """
    Send key-up events for any modifier keys currently held.
    Only called before the FINAL injection (after recording stops and
    hotkey keys are fully released). Never called during streaming.
    """
    u32    = ctypes.windll.user32
    events: list[_Input] = []
    for vk in _MODIFIERS:
        if u32.GetAsyncKeyState(vk) & 0x8000:
            inp = _Input(type=_INPUT_KEYBOARD)
            inp.ki.wVk     = vk
            inp.ki.dwFlags = _KEYEVENTF_KEYUP
            events.append(inp)
    if events:
        arr = (_Input * len(events))(*events)
        u32.SendInput(len(events), arr, ctypes.sizeof(_Input))
        time.sleep(0.10)   # settle before injecting


# ── Injector class ────────────────────────────────────────────────────────────

class Injector:
    def __init__(self, method: str = "clipboard"):
        self.method = method   # kept for config compat
        self._lock  = threading.Lock()

    # ── Public API ─────────────────────────────────────────────────────────────

    def inject(self, text: str, release_mods: bool = True) -> bool:
        """
        Inject text into the focused window. Thread-safe.
        release_mods=True: release hotkey modifiers first (use for final injection).
        release_mods=False: skip modifier release (use during streaming while key held).
        """
        if not text or not text.strip():
            return False
        with self._lock:
            return self._inject(text, release_mods=release_mods)

    def inject_immediate(self, text: str) -> bool:
        """
        Skip the internal lock — called from streaming loop which has its own lock.
        Never releases modifiers (hotkey is still physically held during streaming).
        """
        if not text or not text.strip():
            return False
        return self._inject(text, release_mods=False)

    # ── Implementation ─────────────────────────────────────────────────────────

    def _inject(self, text: str, release_mods: bool = True) -> bool:
        if release_mods:
            _release_modifiers()

        cls = _get_fg_class()

        if cls in BROWSER_CLASSES:
            # Browsers: VK_PACKET via SendInput
            ok = _send_unicode(text)
            method = "SendInput/VK_PACKET"
        else:
            # Native apps (Outlook, Word, etc.): WM_CHAR — modifier-state agnostic
            ok = _post_wm_char(text)
            method = "WM_CHAR"
            if not ok:
                # WM_CHAR failed (no foreground window?) — try VK_PACKET
                ok = _send_unicode(text)
                method = "SendInput/VK_PACKET (fallback)"

        if ok:
            print(f"[Injector] {len(text)} chars via {method} (class={cls!r})")
            return True

        print("[Injector] All direct methods failed — trying clipboard")
        return self._clipboard_paste(text)

    def _clipboard_paste(self, text: str) -> bool:
        """
        Last resort: clipboard + raw keybd_event Ctrl+V.
        Only reached when SendInput is blocked (UIPI elevation mismatch).
        """
        try:
            import pyperclip

            try:
                original = pyperclip.paste()
            except Exception:
                original = None

            pyperclip.copy(text)
            time.sleep(0.15)

            VK_CTRL = 0x11
            VK_V    = 0x56
            VK_MENU = 0x12
            KU      = 0x0002
            u32     = ctypes.windll.user32

            # Release Alt in case it's still registered
            if u32.GetAsyncKeyState(VK_MENU) & 0x8000:
                u32.keybd_event(VK_MENU, 0, KU, 0)
                time.sleep(0.05)

            u32.keybd_event(VK_CTRL, 0, 0,  0)
            u32.keybd_event(VK_V,    0, 0,  0)
            u32.keybd_event(VK_V,    0, KU, 0)
            u32.keybd_event(VK_CTRL, 0, KU, 0)
            time.sleep(0.18)

            if original is not None:
                def _restore():
                    time.sleep(0.4)
                    try:
                        pyperclip.copy(original)
                    except Exception:
                        pass
                threading.Thread(target=_restore, daemon=True).start()

            print(f"[Injector] Clipboard paste {len(text)} chars.")
            return True

        except Exception as e:
            print(f"[Injector] Clipboard paste failed: {e}")
            return False
