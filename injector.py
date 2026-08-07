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
import os
import threading
import time


_u32 = ctypes.WinDLL("user32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

_HGLOBAL = ctypes.c_void_p
_LPVOID = ctypes.c_void_p

_u32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
_u32.OpenClipboard.restype = ctypes.wintypes.BOOL
_u32.CloseClipboard.argtypes = []
_u32.CloseClipboard.restype = ctypes.wintypes.BOOL
_u32.EmptyClipboard.argtypes = []
_u32.EmptyClipboard.restype = ctypes.wintypes.BOOL
_u32.GetClipboardData.argtypes = [ctypes.c_uint]
_u32.GetClipboardData.restype = _HGLOBAL
_u32.SetClipboardData.argtypes = [ctypes.c_uint, _HGLOBAL]
_u32.SetClipboardData.restype = _HGLOBAL

_k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
_k32.GlobalAlloc.restype = _HGLOBAL
_k32.GlobalLock.argtypes = [_HGLOBAL]
_k32.GlobalLock.restype = _LPVOID
_k32.GlobalUnlock.argtypes = [_HGLOBAL]
_k32.GlobalUnlock.restype = ctypes.wintypes.BOOL
_k32.GlobalFree.argtypes = [_HGLOBAL]
_k32.GlobalFree.restype = _HGLOBAL

# Declare return/arg types so we can actually trust PostMessageW / SendInput
# results — without these ctypes assumes int and silent failures look like wins.
_u32.PostMessageW.argtypes = [
    ctypes.wintypes.HWND, ctypes.c_uint, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM
]
_u32.PostMessageW.restype = ctypes.wintypes.BOOL
_u32.SendInput.restype = ctypes.c_uint

# SendMessageTimeoutW carries a pointer in lParam and returns the message result
# through an out-pointer — without explicit types both get truncated to c_int on
# 64-bit and the read-back verification would compare garbage.
_u32.SendMessageTimeoutW.argtypes = [
    ctypes.wintypes.HWND, ctypes.c_uint, ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM, ctypes.c_uint, ctypes.c_uint,
    ctypes.POINTER(ctypes.c_size_t),
]
_u32.SendMessageTimeoutW.restype = ctypes.c_size_t

# ── Clipboard-restore race guard ──────────────────────────────────────────────
# Every clipboard write bumps a generation counter. A delayed restore only fires
# if no newer write happened in the meantime, so a stale restore thread can never
# clobber a newer paste's clipboard contents.
_clip_lock = threading.Lock()
_clip_gen = 0
# The USER'S clipboard content while one or more restores are outstanding:
# (text, timestamp). When pastes overlap inside the restore delay, the second
# paste would otherwise capture OUR OWN injected text as "previous" and the
# user's real clipboard would be lost. Carried forward instead.
_orig_pending = None


# ── Win32 constants ───────────────────────────────────────────────────────────

_INPUT_KEYBOARD = 1
_KEYEVENTF_UNICODE = 0x0004
_KEYEVENTF_KEYUP = 0x0002
_WM_CHAR = 0x0102
_VK_RETURN = 0x0D

# Window classes that use Chromium / Gecko rendering — prefer VK_PACKET
BROWSER_CLASSES = frozenset(
    {
        "Chrome_WidgetWin_1",  # Chrome, Edge, Brave, Electron, VS Code
        "MozillaWindowClass",  # Firefox
        "MozillaDialogClass",
        "Chrome_RenderWidgetHostHWND",
    }
)
BROWSER_CLASS_PREFIXES = (
    "Chrome_WidgetWin_",
    "Mozilla",
    "CEF-",
)

# Window classes for game engines / fullscreen overlays — skip direct injection,
# go straight to clipboard paste (they don't process WM_CHAR / VK_PACKET reliably)
GAME_CLASSES = frozenset(
    {
        "DXGI",
        "UnrealWindow",
        "SDL_app",
        "GLFW30",
        "D3DWindow",
    }
)
GAME_CLASS_PREFIXES = (
    "DXGI",
    "UnrealWindow",
    "SDL_",
    "GLFW",
    "D3DWindow",
)

# Terminal emulator windows — use Ctrl+Shift+V instead of Ctrl+V.
# Ctrl+V in a terminal session sends a raw ^V control sequence to the shell,
# not a paste event. The terminal app itself intercepts Ctrl+Shift+V as paste.
TERMINAL_CLASSES = frozenset({
    "CASCADIA_HOSTING_WINDOW_CLASS",  # Windows Terminal (wt.exe)
})
TERMINAL_CLASS_PREFIXES = ("CASCADIA_",)

# Modifier VK codes — released before final injection only
_MODIFIERS = (
    0x10,
    0x11,
    0x12,  # Shift, Ctrl, Alt
    0x5B,
    0x5C,  # LWin, RWin
    0xA0,
    0xA1,  # LShift, RShift
    0xA2,
    0xA3,  # LCtrl, RCtrl
    0xA4,
    0xA5,  # LAlt, RAlt
)


# ── Win32 SendInput structures ────────────────────────────────────────────────


class _KbdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_uint64),
    ]


class _Input(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KbdInput), ("_pad", ctypes.c_byte * 28)]

    _anonymous_ = ("_u",)
    _fields_ = [("type", ctypes.c_ulong), ("_u", _U)]


# ── Window detection ──────────────────────────────────────────────────────────


def _get_fg_class() -> str:
    """Return the Win32 class name of the current foreground window."""
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(128)
    ctypes.windll.user32.GetClassNameW(hwnd, buf, 128)
    return buf.value


def _is_browser_class(class_name: str) -> bool:
    if not class_name:
        return False
    if class_name in BROWSER_CLASSES:
        return True
    return class_name.startswith(BROWSER_CLASS_PREFIXES)


def _is_game_class(class_name: str) -> bool:
    if not class_name:
        return False
    if class_name in GAME_CLASSES:
        return True
    return class_name.startswith(GAME_CLASS_PREFIXES)


def _is_terminal_class(class_name: str) -> bool:
    if not class_name:
        return False
    if class_name in TERMINAL_CLASSES:
        return True
    return class_name.startswith(TERMINAL_CLASS_PREFIXES)


def _get_focused_child(fg_hwnd: int) -> int:
    """
    Return the focused child HWND inside the foreground window.
    Uses AttachThreadInput so GetFocus() returns the correct control
    (e.g. the text editor inside an Outlook compose window).

    AttachThreadInput can legitimately fail (different desktop, elevated target,
    timing). If it does, fall back to the foreground HWND rather than posting
    WM_CHAR to whatever GetFocus() returns for our own thread (which would type
    into the wrong window or nowhere).
    """
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    fg_tid = u32.GetWindowThreadProcessId(fg_hwnd, None)
    our_tid = k32.GetCurrentThreadId()
    if fg_tid == our_tid:
        return u32.GetFocus() or fg_hwnd
    attached = bool(u32.AttachThreadInput(our_tid, fg_tid, True))
    if not attached:
        return fg_hwnd  # couldn't attach — safest target is the fg window itself
    try:
        focused = u32.GetFocus()
    finally:
        u32.AttachThreadInput(our_tid, fg_tid, False)
    return focused or fg_hwnd


def foreground_is_elevated_blocked() -> bool:
    """
    True if the foreground window belongs to an elevated (higher integrity)
    process while we are NOT elevated. In that case Windows UIPI silently drops
    all our injected input — clipboard paste, SendInput, and WM_CHAR all fail
    with no error. The caller uses this to warn the user instead of failing
    silently ('Run FTC Whisper as administrator to type into elevated apps').
    """
    try:
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        adv = ctypes.windll.advapi32

        # Are we elevated? If so, nothing is blocked.
        if ctypes.windll.shell32.IsUserAnAdmin():
            return False

        hwnd = u32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = ctypes.wintypes.DWORD(0)
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return False

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            # Access denied opening the process handle is itself a strong signal
            # the target is higher-integrity than us.
            return True
        try:
            TOKEN_QUERY = 0x0008
            token = ctypes.wintypes.HANDLE()
            # OpenProcessToken lives in advapi32 — calling it on kernel32 raised
            # AttributeError into the blanket except, so this check always
            # returned False and elevated windows failed silently.
            if not adv.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(token)):
                return False
            try:
                TokenElevation = 20
                elevation = ctypes.wintypes.DWORD(0)
                ret_len = ctypes.wintypes.DWORD(0)
                if adv.GetTokenInformation(
                    token, TokenElevation,
                    ctypes.byref(elevation), ctypes.sizeof(elevation),
                    ctypes.byref(ret_len),
                ):
                    return bool(elevation.value)
            finally:
                k32.CloseHandle(token)
        finally:
            k32.CloseHandle(h)
    except Exception:
        pass
    return False


def _get_fg_exe() -> str:
    """Best-effort exe name of the foreground window's process ('' unknown)."""
    try:
        hwnd = _u32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = ctypes.wintypes.DWORD(0)
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = ctypes.wintypes.DWORD(260)
            if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            _k32.CloseHandle(h)
    except Exception:
        pass
    return ""


def _build_failure_detail(fg_exe: str, fg_class: str, method: str, chars: int,
                          partial: bool, elevated: bool,
                          target_hwnd: int = 0, fg_hwnd: int = 0,
                          exception: str = "") -> dict:
    """Structured record of WHY a final injection failed — the same facts as
    the local inject-failures.log line, but machine-readable so the remote
    error log can be filtered by cause (elevated target, moved foreground,
    partial WM_CHAR, clipboard refusal)."""
    detail = {
        "fg_exe": fg_exe or "?",
        "fg_class": fg_class or "",
        "method": method,
        "chars": int(chars),
        "partial_direct": bool(partial),
        "elevated_target": bool(elevated),
    }
    if target_hwnd and fg_hwnd and target_hwnd != fg_hwnd:
        # The user clicked elsewhere mid-dictation — the paste gate refused
        # rather than typing into the wrong window.
        detail["foreground_moved"] = True
    if exception:
        detail["exception"] = exception[:300]
    return detail


# ── Post-injection read-back verification (telemetry only) ────────────────────

_WM_GETTEXT = 0x000D
_WM_GETTEXTLENGTH = 0x000E
_SMTO_ABORTIFHUNG = 0x0002
_VERIFY_MAX_CONTROL_CHARS = 200_000


def _send_message_timeout(hwnd: int, msg: int, wparam: int = 0,
                          lparam: int = 0, timeout_ms: int = 500):
    """SendMessageTimeoutW wrapper → message result, or None when the target
    is hung / the call failed. Never blocks longer than timeout_ms."""
    try:
        res = ctypes.c_size_t(0)
        ok = _u32.SendMessageTimeoutW(
            hwnd, msg, wparam, lparam, _SMTO_ABORTIFHUNG, timeout_ms,
            ctypes.byref(res))
        return int(res.value) if ok else None
    except Exception:
        return None


def _control_class(hwnd: int) -> str:
    try:
        buf = ctypes.create_unicode_buffer(64)
        _u32.GetClassNameW(hwnd, buf, 64)
        return buf.value or ""
    except Exception:
        return ""


def _control_style(hwnd: int) -> int:
    try:
        GWL_STYLE = -16
        return int(_u32.GetWindowLongW(hwnd, GWL_STYLE))
    except Exception:
        return 0


def _read_control_text(hwnd: int, length: int):
    """Text of a classic Edit/RichEdit control via WM_GETTEXT (None on failure)."""
    try:
        buf = ctypes.create_unicode_buffer(length + 1)
        copied = _send_message_timeout(
            hwnd, _WM_GETTEXT, length + 1, ctypes.addressof(buf))
        if copied is None:
            return None
        return buf.value
    except Exception:
        return None


def verify_text_landed(child_hwnd: int, text: str):
    """Best-effort post-injection check: did *text* actually appear in the
    target control? Returns True/False only for classic Edit/RichEdit controls
    (the only ones that honestly answer WM_GETTEXT); None = unverifiable —
    browsers, Word, and custom editors don't expose content this way, and a
    None must never be treated as a failure.

    Telemetry only: the caller must NEVER re-inject on False — if the check is
    wrong (an autocorrect rewrote the text), a re-send would duplicate it.
    """
    try:
        if not child_hwnd or not text or not text.strip():
            return None
        if not _u32.IsWindow(child_hwnd):
            return None
        cls = _control_class(child_hwnd).lower()
        if "edit" not in cls:  # Edit, RichEdit20W, RICHEDIT50W, …
            return None
        ES_PASSWORD = 0x0020
        if _control_style(child_hwnd) & ES_PASSWORD:
            return None  # masked content — unreadable by design
        length = _send_message_timeout(child_hwnd, _WM_GETTEXTLENGTH)
        if length is None or length > _VERIFY_MAX_CONTROL_CHARS:
            return None  # hung target or huge document — don't stall the app
        expected = " ".join(text.split())
        if not expected:
            return None
        if length <= 0:
            # Ambiguous, NOT a failure: chat/command inputs clear themselves
            # when the user presses Enter within the settle window — treating
            # that as a false success would flood the log with phantoms.
            return None
        content = _read_control_text(child_hwnd, length)
        if content is None:
            return None
        # Compare whitespace-normalised (WM_CHAR turned \n into \r, the control
        # renders \r\n) and only the tail — the control may hold prior content.
        tail = expected[-60:]
        return tail in " ".join(content.split())
    except Exception:
        return None


def _log_inject_failure(detail: str) -> None:
    """Append one line to a small rolling failure log. The frozen exe has no
    console, so this file is the only way a 'text never landed' report from
    another machine can say WHICH app, WHICH strategy, and whether the target
    was elevated. %LOCALAPPDATA%\\FTC Whisper\\inject-failures.log"""
    try:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        folder = os.path.join(base, "FTC Whisper")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "inject-failures.log")
        lines: list[str] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            pass
        lines.append(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + detail + "\n")
        if len(lines) > 200:
            lines = lines[-150:]
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception:
        pass


# ── Injection methods ─────────────────────────────────────────────────────────


def _post_wm_char(text: str, target_child: int = 0) -> tuple[int, int]:
    """
    Inject text by posting WM_CHAR messages directly to the focused child HWND.

    This is the safest method for native Windows apps (Outlook, Word, etc.):
    - WM_CHAR carries only the character code — modifier state is irrelevant
    - No clipboard involvement — no Paste Special dialog
    - Works while the hotkey modifiers (Alt, Ctrl) are still physically held
    - Handles surrogate pairs for emoji / extended Unicode

    target_child: when a valid HWND, post straight to it. This is the control
    that was focused when recording STARTED — using it means text lands in the
    box the user originally clicked even if the foreground moved (a mid-speech
    click elsewhere). PostMessage needs no foreground, so this is the reliable
    recovery path. When 0/invalid, derive the child from the live foreground.

    Returns (posted, failed) character counts. A partial post (some landed,
    some failed) must NOT be retried with another method — the characters that
    DID land would be duplicated.
    """
    u32 = _u32
    if target_child and u32.IsWindow(target_child):
        target = target_child
    else:
        fg = u32.GetForegroundWindow()
        if not fg:
            return 0, len(text)
        target = _get_focused_child(fg)
    posted = 0
    failed = 0
    for ch in text:
        # Edit controls expect a carriage return (\r) for a line break; a raw
        # \n is a control char that most native controls ignore or render wrong.
        if ch == "\n":
            ch = "\r"
        code = ord(ch)
        if code > 0xFFFF:
            code -= 0x10000
            ok1 = u32.PostMessageW(target, _WM_CHAR, 0xD800 | (code >> 10), 1)
            ok2 = u32.PostMessageW(target, _WM_CHAR, 0xDC00 | (code & 0x3FF), 1)
            ok = bool(ok1) and bool(ok2)
        else:
            ok = bool(u32.PostMessageW(target, _WM_CHAR, code, 1))
        if ok:
            posted += 1
        else:
            # Stop at the FIRST failed post: continuing would leave holes in the
            # middle of the landed text ("I like ice cr" + later chars landing
            # after the gap). Stopping keeps what landed a clean prefix of the
            # chunk, which the live-inject reconcile can converge safely.
            failed = len(text) - posted
            break
    if failed:
        print(f"[Injector] WM_CHAR posted={posted} failed={failed}")
    return posted, failed


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
        if ch == "\n":
            # Inject a real Enter keypress rather than a literal LF — web inputs
            # and editors treat VK_RETURN as a newline but ignore a unicode \n.
            events.append(_Input(type=_INPUT_KEYBOARD, ki=_KbdInput(wVk=_VK_RETURN)))
            events.append(_Input(
                type=_INPUT_KEYBOARD,
                ki=_KbdInput(wVk=_VK_RETURN, dwFlags=_KEYEVENTF_KEYUP),
            ))
            continue
        code = ord(ch)
        if code > 0xFFFF:
            code -= 0x10000
            high = 0xD800 | (code >> 10)
            low = 0xDC00 | (code & 0x3FF)
            for sc in (high, low):
                events.append(
                    _Input(
                        type=_INPUT_KEYBOARD,
                        ki=_KbdInput(wScan=sc, dwFlags=_KEYEVENTF_UNICODE),
                    )
                )
                events.append(
                    _Input(
                        type=_INPUT_KEYBOARD,
                        ki=_KbdInput(
                            wScan=sc, dwFlags=_KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP
                        ),
                    )
                )
        else:
            events.append(
                _Input(
                    type=_INPUT_KEYBOARD,
                    ki=_KbdInput(wScan=code, dwFlags=_KEYEVENTF_UNICODE),
                )
            )
            events.append(
                _Input(
                    type=_INPUT_KEYBOARD,
                    ki=_KbdInput(
                        wScan=code, dwFlags=_KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP
                    ),
                )
            )

    arr = (_Input * len(events))(*events)
    sent = ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(_Input))
    return sent == len(events)


def _release_modifiers() -> None:
    """
    Send key-up events for any modifier keys currently held.
    Only called before the FINAL injection (after recording stops and
    hotkey keys are fully released). Never called during streaming.
    """
    u32 = ctypes.windll.user32
    events: list[_Input] = []
    alt_released = False
    for vk in _MODIFIERS:
        if u32.GetAsyncKeyState(vk) & 0x8000:
            if vk in (0x12, 0xA4, 0xA5):
                alt_released = True
            inp = _Input(type=_INPUT_KEYBOARD)
            inp.ki.wVk = vk
            inp.ki.dwFlags = _KEYEVENTF_KEYUP
            events.append(inp)
    if events:
        if alt_released:
            # A bare Alt-down/Alt-up (the base key was swallowed by
            # RegisterHotKey) activates the menu bar in browsers/Office. A
            # harmless dummy key BEFORE the Alt key-up cancels menu activation —
            # 0xFF (VK__none_) is the documented no-op key for this.
            dummy_dn = _Input(type=_INPUT_KEYBOARD, ki=_KbdInput(wVk=0xFF))
            dummy_up = _Input(type=_INPUT_KEYBOARD, ki=_KbdInput(wVk=0xFF, dwFlags=_KEYEVENTF_KEYUP))
            events = [dummy_dn, dummy_up] + events
        arr = (_Input * len(events))(*events)
        u32.SendInput(len(events), arr, ctypes.sizeof(_Input))
        time.sleep(0.05)  # settle before injecting


# ── Injector class ────────────────────────────────────────────────────────────


class Injector:
    # Mirror of config.copy_to_clipboard, kept on the class because the
    # clipboard helpers are static. False (the default) means the clipboard is
    # left as we found it: a clipboard-paste injection with nothing to restore
    # empties it again instead of leaving the dictation behind. app.py sets it
    # at startup and on every settings change.
    keep_clipboard = False

    def __init__(self, method: str = "clipboard"):
        m = (method or "").strip().lower()
        self.method = m if m in {"clipboard", "keystrokes", "auto"} else "clipboard"
        self._lock = threading.Lock()
        self._partial_direct = False  # set when a direct inject partially landed
        # Diagnostics for the fleet error log: why the last final inject()
        # failed (structured dict, None while it hasn't), and which transport
        # actually landed the last successful injection.
        self.last_failure: dict | None = None
        self.last_method: str = ""
        # Capture target for the CURRENT final injection. Set by inject() and
        # read by _clipboard_paste (foreground gate) and _direct_inject (stored
        # child WM_CHAR). 0 outside a final inject — the streaming path never
        # sets these, so its focus contract is untouched.
        self._target_hwnd = 0
        self._target_child = 0

    # ── Public API ─────────────────────────────────────────────────────────────

    def inject(self, text: str, release_mods: bool = True,
               target_hwnd: int = 0, target_child: int = 0) -> bool:
        """
        Inject text into the focused window. Thread-safe.
        release_mods=True: release hotkey modifiers first (use for final injection).
        release_mods=False: skip modifier release (use during streaming while key held).
        target_hwnd/target_child: the top-level window and focused control captured
        when recording STARTED. When given, the clipboard paste refuses to fire
        into any other foreground (so a mid-speech click elsewhere can't paste into
        the wrong window), and native WM_CHAR posts straight to the stored control
        so the text still lands where the user originally clicked.
        Automatically retries once on failure after a short settle delay —
        UNLESS the failure was a partial direct injection (some characters
        landed), where a full re-send would duplicate them.
        """
        if not text or not text.strip():
            return False
        with self._lock:
            self._partial_direct = False
            self.last_failure = None
            self.last_method = ""
            self._target_hwnd = int(target_hwnd or 0)
            self._target_child = int(target_child or 0)
            try:
                result = self._inject(text, release_mods=release_mods)
                if not result and not self._partial_direct:
                    # First attempt failed — give Windows time to settle focus,
                    # then try once more. Modifiers are already released.
                    print("[Injector] First attempt failed — retrying in 200 ms…")
                    time.sleep(0.20)
                    result = self._inject(text, release_mods=False)
                    if result:
                        print("[Injector] Retry succeeded.")
                    else:
                        print("[Injector] Retry also failed.")
                if not result:
                    # Hard failure: capture WHY in the rolling field log (and a
                    # structured copy for the remote error log) so a "text never
                    # landed" report from any device is diagnosable.
                    elevated = foreground_is_elevated_blocked()
                    fg_now = 0
                    try:
                        fg_now = _u32.GetForegroundWindow()
                    except Exception:
                        pass
                    self.last_failure = _build_failure_detail(
                        _get_fg_exe(), _get_fg_class(), self.method, len(text),
                        self._partial_direct, elevated,
                        target_hwnd=self._target_hwnd, fg_hwnd=fg_now)
                    detail = (
                        f"target={_get_fg_exe() or '?'} class={_get_fg_class()!r} "
                        f"method={self.method} chars={len(text)}"
                        + (" partial-direct (no paste retry)"
                           if self._partial_direct else "")
                        + (" ELEVATED-TARGET (run FTC Whisper as admin)"
                           if elevated else "")
                    )
                    print(f"[Injector] FAILED: {detail}")
                    _log_inject_failure(detail)
                return result
            finally:
                self._target_hwnd = 0
                self._target_child = 0

    def inject_immediate(self, text: str) -> bool:
        """
        Skip the internal lock — called from streaming loop which has its own lock.
        Never releases modifiers (hotkey is still physically held during streaming).
        """
        if not text or not text.strip():
            return False
        return self._inject(text, release_mods=False)

    def inject_stream(self, text: str) -> bool:
        """
        Append-only live-injection of a small chunk while the user is still speaking.

        Goes STRAIGHT to the direct path (WM_CHAR for native apps, VK_PACKET for
        browsers) and NEVER through the clipboard: a per-word Ctrl+V + clipboard
        backup/restore would thrash the clipboard, release modifiers, and race the
        restore thread on every word. WM_CHAR/VK_PACKET carry no modifier state, so
        this is safe while the hotkey (e.g. Alt+V) is still physically held.

        No internal lock — the single StreamingSession worker thread is the only
        caller, and it serialises its own emits. Never releases modifiers.

        Returns True only if the whole chunk landed. The caller MUST advance its
        char-count bookkeeping only when this returns True (a partial/failed emit
        that still counted would desync the finalize reconcile).
        """
        if not text:
            return False
        return self._direct_inject(text)

    def delete_stream(self, n: int) -> bool:
        """
        Delete *n* characters via the SAME transport inject_stream uses for the
        current window, so deletions and re-typed text can never interleave out
        of order:
          - Native apps: WM_CHAR 0x08 posted to the focused child — the same
            thread message queue the streamed text went through.
          - Browsers: SendInput VK_BACK — same system input queue as VK_PACKET.
        Mixing transports (SendInput backspaces + PostMessage text) lets trailing
        backspaces arrive AFTER the corrected tail and eat it — the exact
        truncation bug the live-inject e2e test caught.
        """
        if n <= 0:
            return True
        cls = _get_fg_class()
        if _is_browser_class(cls) and not _is_game_class(cls):
            return self.send_backspaces(n)
        posted, failed = _post_wm_char("\x08" * n)
        if failed:
            print(f"[Injector] delete_stream posted={posted} failed={failed}")
        return failed == 0

    def send_backspaces(self, n: int) -> bool:
        """
        Send *n* Backspace keypresses in one batched SendInput call. Correct only
        when the text being deleted was ALSO sent through SendInput (browser
        path) or when no further text will be typed through another transport —
        otherwise use delete_stream() for queue-ordering safety.
        """
        if n <= 0:
            return True
        VK_BACK = 0x08
        events: list[_Input] = []
        for _ in range(n):
            events.append(_Input(type=_INPUT_KEYBOARD, ki=_KbdInput(wVk=VK_BACK)))
            events.append(_Input(
                type=_INPUT_KEYBOARD,
                ki=_KbdInput(wVk=VK_BACK, dwFlags=_KEYEVENTF_KEYUP),
            ))
        arr = (_Input * len(events))(*events)
        sent = ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(_Input))
        ok = sent == len(events)
        if not ok:
            print(f"[Injector] send_backspaces sent={sent}/{len(events)}")
        return ok

    # ── Implementation ─────────────────────────────────────────────────────────

    def _inject(self, text: str, release_mods: bool = True) -> bool:
        if release_mods:
            _release_modifiers()

        cls = _get_fg_class()

        # Windows Terminal: Ctrl+Shift+V is the paste shortcut. Ctrl+V passes
        # a raw ^V control-sequence to the running shell process instead.
        if _is_terminal_class(cls):
            return self._terminal_paste(text)

        if _is_browser_class(cls) and not _is_game_class(cls):
            # Clipboard paste (Ctrl+V) is the most reliable method for ALL browser
            # inputs: search bars, React/Vue inputs, contenteditable, rich editors,
            # and Electron apps. SendInput/VK_PACKET returns "success" even when
            # Chrome discards the events due to uncertain DOM focus state — making
            # it an unreliable primary path. Clipboard never has that problem.
            if self._clipboard_paste(text):
                return True
            print("[Injector] Browser clipboard failed -> VK_PACKET fallback")
            return self._direct_inject(text)

        # Native (non-browser) target with non-text on the clipboard (a screenshot
        # from Win+Shift+S, copied files) — applies to EVERY mode, not just auto.
        # A clipboard paste here would (a) destroy that content and (b) can outright
        # FAIL when the capture tool still holds the clipboard open, which is the
        # "dictated while screenshotting, text never landed" case. WM_CHAR is
        # reliable for native apps and touches nothing on the clipboard, so use it
        # first; fall back to clipboard only if direct injection fully fails.
        if self._clipboard_has_nontext():
            print("[Injector] Clipboard holds non-text (image/files) — direct inject first (preserve it)")
            if self._direct_inject(text):
                return True
            if self._partial_direct:
                return False  # some chars landed — a paste now would duplicate them
            print("[Injector] Direct inject failed -> clipboard paste (last resort)")
            return self._clipboard_paste(text)

        # Non-browser: respect config-driven injection mode.
        # - clipboard  : most universal for office/web/chat apps
        # - keystrokes : direct Win32/SendInput path first
        # - auto       : clipboard first, then direct fallback
        if self.method == "clipboard":
            if self._clipboard_paste(text):
                return True
            print("[Injector] Clipboard mode fallback -> direct methods")
            return self._direct_inject(text)

        if self.method == "keystrokes":
            if self._direct_inject(text):
                return True
            if self._partial_direct:
                return False  # some chars landed — a paste now would duplicate them
            print("[Injector] Keystrokes mode fallback -> clipboard")
            return self._clipboard_paste(text)

        if self._clipboard_paste(text):
            return True
        print("[Injector] Auto mode fallback -> direct methods")
        return self._direct_inject(text)

    def _direct_inject(self, text: str) -> bool:
        cls = _get_fg_class()

        if _is_game_class(cls):
            # Game/overlay windows don't process WM_CHAR or VK_PACKET reliably —
            # fall through immediately so the caller can use clipboard paste.
            print(f"[Injector] Game/overlay window class={cls!r} — skipping direct inject")
            return False

        if _is_browser_class(cls):
            # Browsers: VK_PACKET via SendInput
            ok = _send_unicode(text)
            method = "SendInput/VK_PACKET"
            if not ok:
                posted, failed = _post_wm_char(text, self._target_child)
                ok = posted > 0 and failed == 0
                if posted > 0 and failed > 0:
                    self._partial_direct = True
                method = "WM_CHAR (fallback)"
        else:
            # Native apps (Outlook, Word, etc.): WM_CHAR — modifier-state agnostic.
            # Post to the control focused at record start so the text lands in the
            # right box even if the foreground moved (mid-speech click elsewhere).
            posted, failed = _post_wm_char(text, self._target_child)
            ok = posted > 0 and failed == 0
            method = "WM_CHAR"
            if not ok:
                if posted > 0:
                    # Partial: SOME characters landed. Falling through to another
                    # method (or the inject() retry) would duplicate them.
                    self._partial_direct = True
                    print("[Injector] Partial WM_CHAR injection — not retrying to avoid duplicates")
                    return False
                # Nothing landed at all — safe to try VK_PACKET
                ok = _send_unicode(text)
                method = "SendInput/VK_PACKET (fallback)"

        if ok:
            self.last_method = method
            print(f"[Injector] {len(text)} chars via {method} (class={cls!r})")
            return True

        print("[Injector] All direct methods failed")
        return False

    # ── Clipboard helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _clipboard_has_nontext() -> bool:
        """True when the clipboard holds content with no text representation
        (screenshot, copied files, cells) — content we can't back up/restore."""
        CF_UNICODETEXT = 13
        CF_TEXT = 1
        has_any = False
        has_text = False
        if _u32.OpenClipboard(None):
            try:
                fmt = 0
                while True:
                    fmt = ctypes.windll.user32.EnumClipboardFormats(fmt)
                    if not fmt:
                        break
                    has_any = True
                    if fmt in (CF_UNICODETEXT, CF_TEXT):
                        has_text = True
                        break
            except Exception:
                pass
            finally:
                _u32.CloseClipboard()
        return has_any and not has_text

    @staticmethod
    def _clipboard_set(text: str, bump: bool = True) -> tuple[bool, str]:
        """
        Write *text* to the Windows clipboard via ctypes (no pyperclip dependency).
        Returns (success, previous_text). Success is verified by readback —
        callers MUST NOT send Ctrl+V when success is False, or whatever stale
        content sits on the clipboard (a password, an old copy) gets pasted
        instead of the dictation.

        bump=True advances the global clipboard generation so a delayed restore
        from an earlier paste knows it has been superseded. Restores pass
        bump=False so restoring does not itself look like a new paste.
        """
        global _clip_gen
        if bump:
            with _clip_lock:
                _clip_gen += 1
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        # ── Read previous content ──
        previous = ""
        if _u32.OpenClipboard(None):
            try:
                h = _u32.GetClipboardData(CF_UNICODETEXT)
                if h:
                    ptr = _k32.GlobalLock(h)
                    if ptr:
                        previous = ctypes.wstring_at(ptr)
                        _k32.GlobalUnlock(h)
            except Exception:
                pass
            finally:
                _u32.CloseClipboard()

        if bump:
            global _orig_pending
            with _clip_lock:
                now = time.time()
                if _orig_pending is not None and now - _orig_pending[1] < 10.0:
                    # An earlier paste's restore hasn't fired yet — what's on
                    # the clipboard is OUR injected text, not the user's. Carry
                    # the true original forward to this generation's restore.
                    previous = _orig_pending[0]
                    _orig_pending = (previous, now)
                else:
                    _orig_pending = (previous, now) if previous else None

        # ── Write new content (with readback verification) ──
        encoded = (text + "\x00").encode("utf-16-le")
        expected_prefix = text[:50]
        for _attempt in range(3):
            if _u32.OpenClipboard(None):
                try:
                    _u32.EmptyClipboard()
                    h = _k32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
                    if not h:
                        continue

                    ptr = _k32.GlobalLock(h)
                    if not ptr:
                        _k32.GlobalFree(h)
                        continue

                    ctypes.memmove(ptr, encoded, len(encoded))
                    _k32.GlobalUnlock(h)

                    if not _u32.SetClipboardData(CF_UNICODETEXT, h):
                        _k32.GlobalFree(h)
                        continue
                except Exception:
                    continue
                finally:
                    _u32.CloseClipboard()

                # Readback: verify the clipboard now contains what we wrote.
                time.sleep(0.01)
                verified = False
                if _u32.OpenClipboard(None):
                    try:
                        rh = _u32.GetClipboardData(CF_UNICODETEXT)
                        if rh:
                            rptr = _k32.GlobalLock(rh)
                            if rptr:
                                readback = ctypes.wstring_at(rptr)
                                _k32.GlobalUnlock(rh)
                                if readback[:50] == expected_prefix:
                                    verified = True
                    except Exception:
                        pass
                    finally:
                        _u32.CloseClipboard()

                if verified:
                    return True, previous

                print(f"[Injector] Clipboard readback mismatch on attempt {_attempt + 1} — retrying")

            time.sleep(0.02)

        print("[Injector] Clipboard set failed after 3 attempts")
        return False, previous

    @staticmethod
    def _clipboard_clear() -> bool:
        """Empty the clipboard. Used when there was nothing to restore and the
        user has NOT asked for dictations to be kept there."""
        if not _u32.OpenClipboard(None):
            return False
        try:
            _u32.EmptyClipboard()
            return True
        except Exception:
            return False
        finally:
            _u32.CloseClipboard()

    @staticmethod
    def _clipboard_restore(original: str, gen: int) -> None:
        """Restore the clipboard to *original* after a short delay, but only if
        no newer paste has happened in the meantime (otherwise we'd clobber it).

        With nothing to restore (empty clipboard, or non-text content we could
        not back up) the clipboard is EMPTIED instead — leaving the dictation
        sitting there is exactly what the Copy to Clipboard setting turns on,
        so doing it with the setting off ignored the user's choice. When the
        setting IS on, app.py rewrites the text after injection anyway."""
        if not original:
            if Injector.keep_clipboard:
                return

            def _clear():
                time.sleep(1.5)      # same paste-settling delay as a restore
                with _clip_lock:
                    if _clip_gen != gen:
                        return       # a newer paste owns the clipboard now
                    try:
                        Injector._clipboard_clear()
                    except Exception:
                        pass

            threading.Thread(target=_clear, daemon=True).start()
            return

        def _do():
            global _orig_pending
            # 1.5s: a busy Chrome renderer / RDP session can service the Ctrl+V
            # paste event well after our keystroke lands. Restoring at 0.45s LOST
            # that race — the app read the clipboard after the restore and pasted
            # the user's OLD clipboard content instead of the dictation ("said X,
            # typed something else entirely"). The gen guard makes a longer delay
            # free: any newer paste supersedes this restore.
            time.sleep(1.5)
            # Hold the lock across check AND write: a check-then-unlocked-write
            # let a newer paste's verified clipboard get clobbered between its
            # readback and its Ctrl+V being serviced.
            with _clip_lock:
                if _clip_gen != gen:
                    return  # a newer paste superseded us — it carries the original now
                try:
                    Injector._clipboard_set(original, bump=False)
                except Exception:
                    pass
                finally:
                    _orig_pending = None

        threading.Thread(target=_do, daemon=True).start()

    def _clipboard_paste(self, text: str) -> bool:
        """
        Copy *text* to the clipboard then send Ctrl+V via SendInput.
        Works for all apps — native, browsers, React/Vue/Angular (ChatGPT etc.)
        and any text length. Returns False WITHOUT sending Ctrl+V when the
        clipboard write could not be verified — pasting stale clipboard content
        (a password, an old copy) into the target would be far worse than
        falling back to direct injection.
        """
        try:
            u32 = ctypes.windll.user32

            ok, original = self._clipboard_set(text)
            if not ok:
                return False
            with _clip_lock:
                my_gen = _clip_gen
            time.sleep(0.04)  # settle (write already readback-verified)

            # Release ALL held modifiers (Alt → Paste Special in Office,
            # Ctrl → Ctrl+Ctrl+V, Shift → Ctrl+Shift+V paste-without-format,
            # Win → Win+V clipboard-history popup).
            _release_modifiers()

            # Atomic Ctrl+V via SendInput (no interleaving with hardware events)
            VK_CTRL = 0x11
            VK_V = 0x56
            fg_now = u32.GetForegroundWindow()
            # Never paste into a window that is not the capture target. If the
            # user clicked elsewhere mid-dictation the foreground moved, and a
            # Ctrl+V here would dump the text into that other window. Bail so the
            # caller falls through to a stored-child WM_CHAR post, which lands in
            # the ORIGINAL control and needs no foreground.
            if self._target_hwnd and fg_now != self._target_hwnd:
                print(f"[Injector] Foreground {fg_now:#x} != target "
                      f"{self._target_hwnd:#x} — skip Ctrl+V, fall back to direct")
                return False
            print(f"[Injector] Sending Ctrl+V, fg_hwnd={fg_now:#x}")
            ctrl_dn = _Input(type=_INPUT_KEYBOARD, ki=_KbdInput(wVk=VK_CTRL))
            v_dn = _Input(type=_INPUT_KEYBOARD, ki=_KbdInput(wVk=VK_V))
            v_up = _Input(
                type=_INPUT_KEYBOARD, ki=_KbdInput(wVk=VK_V, dwFlags=_KEYEVENTF_KEYUP)
            )
            ctrl_up = _Input(
                type=_INPUT_KEYBOARD,
                ki=_KbdInput(wVk=VK_CTRL, dwFlags=_KEYEVENTF_KEYUP),
            )
            arr = (_Input * 4)(ctrl_dn, v_dn, v_up, ctrl_up)
            sent = u32.SendInput(4, arr, ctypes.sizeof(_Input))
            time.sleep(0.08)  # let app process the paste before restoring clipboard

            self._clipboard_restore(original, my_gen)

            if sent != 4:
                # SendInput was blocked (commonly UIPI: the foreground window is
                # elevated and we are not). The keystrokes never reached the app.
                print(f"[Injector] Ctrl+V SendInput blocked (sent={sent}/4)")
                if foreground_is_elevated_blocked():
                    print("[Injector] Foreground window is elevated — run FTC Whisper as admin to type into it.")
                return False

            self.last_method = "clipboard"
            print(f"[Injector] Clipboard paste {len(text)} chars.")
            return True

        except Exception as e:
            print(f"[Injector] Clipboard paste failed: {e}")
            return False

    def _terminal_paste(self, text: str) -> bool:
        """
        Paste into Windows Terminal via Ctrl+Shift+V.
        Ctrl+V in a terminal emulator sends a raw ^V byte to the shell process
        instead of triggering a paste — only the terminal app's own paste shortcut works.
        """
        try:
            VK_CTRL = 0x11
            VK_SHIFT = 0x10
            VK_V = 0x56
            u32 = ctypes.windll.user32

            ok, original = self._clipboard_set(text)
            if not ok:
                return False
            with _clip_lock:
                my_gen = _clip_gen
            time.sleep(0.04)

            ctrl_dn = _Input(type=_INPUT_KEYBOARD, ki=_KbdInput(wVk=VK_CTRL))
            shift_dn = _Input(type=_INPUT_KEYBOARD, ki=_KbdInput(wVk=VK_SHIFT))
            v_dn = _Input(type=_INPUT_KEYBOARD, ki=_KbdInput(wVk=VK_V))
            v_up = _Input(type=_INPUT_KEYBOARD, ki=_KbdInput(wVk=VK_V, dwFlags=_KEYEVENTF_KEYUP))
            shift_up = _Input(type=_INPUT_KEYBOARD, ki=_KbdInput(wVk=VK_SHIFT, dwFlags=_KEYEVENTF_KEYUP))
            ctrl_up = _Input(type=_INPUT_KEYBOARD, ki=_KbdInput(wVk=VK_CTRL, dwFlags=_KEYEVENTF_KEYUP))
            arr = (_Input * 6)(ctrl_dn, shift_dn, v_dn, v_up, shift_up, ctrl_up)
            sent = u32.SendInput(6, arr, ctypes.sizeof(_Input))
            time.sleep(0.08)

            self._clipboard_restore(original, my_gen)
            if sent != 6:
                print(f"[Injector] Terminal Ctrl+Shift+V blocked (sent={sent}/6)")
                return False
            self.last_method = "terminal-paste"
            print(f"[Injector] Terminal Ctrl+Shift+V paste {len(text)} chars.")
            return True

        except Exception as e:
            print(f"[Injector] Terminal paste failed: {e}")
            return False
