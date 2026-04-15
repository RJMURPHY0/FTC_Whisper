"""
Text injector — sends transcribed text into the focused window.

Primary method: Windows SendInput API with VK_PACKET (Unicode keystrokes).
  - Bypasses the clipboard entirely — no Paste Special dialog
  - Modifier keys are explicitly released via SendInput before every injection
    so hotkey modifier bleed (Alt, Ctrl, Shift, Win) cannot affect the target app
  - Surrogate pairs handled for emoji / extended Unicode

Fallback: clipboard + raw keybd_event Ctrl+V (if SendInput reports zero events sent).
"""

import ctypes
import ctypes.wintypes
import threading
import time


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


_INPUT_KEYBOARD    = 1
_KEYEVENTF_UNICODE = 0x0004
_KEYEVENTF_KEYUP   = 0x0002

# Virtual-key codes for modifier keys
_MODIFIERS = (
    0x10,   # VK_SHIFT
    0x11,   # VK_CONTROL
    0x12,   # VK_MENU  (Alt)
    0x5B,   # VK_LWIN
    0x5C,   # VK_RWIN
    0xA0,   # VK_LSHIFT
    0xA1,   # VK_RSHIFT
    0xA2,   # VK_LCONTROL
    0xA3,   # VK_RCONTROL
    0xA4,   # VK_LMENU  (Left Alt)
    0xA5,   # VK_RMENU  (Right Alt / AltGr)
)


def _release_modifiers() -> None:
    """
    Send key-up events for any modifier keys that GetAsyncKeyState reports as held.
    Called before every injection so hotkey combos (Alt+key, Ctrl+key, etc.) cannot
    bleed into the target window and trigger shortcuts like Paste Special.
    Uses SendInput (not keybd_event) for consistency with the injection path.
    """
    u32 = ctypes.windll.user32
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
        time.sleep(0.04)   # let the key-up events settle before injecting


def _send_unicode(text: str) -> bool:
    """
    Inject every character in text via SendInput / VK_PACKET in one batched call.
    Handles surrogate pairs (emoji, extended Unicode) correctly.
    Returns True if SendInput reported all events were sent.
    """
    if not text:
        return True

    events: list[_Input] = []
    for ch in text:
        code = ord(ch)
        if code > 0xFFFF:
            # Supplementary plane character → surrogate pair
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


class Injector:
    def __init__(self, method: str = "clipboard"):
        self.method = method          # kept for config compat; SendInput is always tried first
        self._lock  = threading.Lock()

    # ── Public API ─────────────────────────────────────────────────────────────

    def inject(self, text: str) -> bool:
        """Inject text into the focused window. Thread-safe."""
        if not text or not text.strip():
            return False
        with self._lock:
            return self._inject(text)

    def inject_immediate(self, text: str) -> bool:
        """
        Same as inject() but skips the internal lock.
        Use from the streaming loop which coordinates via its own lock.
        """
        if not text or not text.strip():
            return False
        return self._inject(text)

    # ── Implementation ─────────────────────────────────────────────────────────

    def _inject(self, text: str) -> bool:
        # Always release modifier keys first — prevents hotkey bleed (Paste Special etc.)
        _release_modifiers()

        ok = _send_unicode(text)
        if ok:
            print(f"[Injector] SendInput {len(text)} chars.")
            return True

        # SendInput returned 0 events sent (blocked by UIPI or other reason)
        print("[Injector] SendInput blocked — trying clipboard fallback")
        return self._clipboard_paste(text)

    def _clipboard_paste(self, text: str) -> bool:
        """
        Clipboard + raw keybd_event Ctrl+V.
        Last resort — used only when SendInput is blocked (e.g. UIPI elevation mismatch).
        Modifiers were already released by _inject() before this is called.
        """
        try:
            import pyperclip

            try:
                original = pyperclip.paste()
            except Exception:
                original = None

            pyperclip.copy(text)
            time.sleep(0.12)

            VK_CTRL = 0x11
            VK_V    = 0x56
            KU      = 0x0002   # KEYEVENTF_KEYUP
            u32     = ctypes.windll.user32

            # Belt-and-suspenders: release Alt again in case any app re-set it
            VK_MENU = 0x12
            if u32.GetAsyncKeyState(VK_MENU) & 0x8000:
                u32.keybd_event(VK_MENU, 0, KU, 0)
                time.sleep(0.03)

            # Send Ctrl+V via raw keybd_event (not keyboard.send — avoids modifier bleed)
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
