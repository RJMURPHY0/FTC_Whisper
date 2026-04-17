"""
Global hotkey manager — detects key press/release system-wide.
Supports hold-to-talk and toggle modes with a clean state machine.

Modifier+key combos (e.g. Alt+V, Ctrl+C) use Win32 RegisterHotKey which
suppresses the combo at the OS kernel level — no low-level hook, no keyboard
lockup risk.  Single keys and CapsLock fall back to the keyboard library.
"""

import atexit
import ctypes
import ctypes.wintypes as _wt
import threading
import time
from enum import Enum
from typing import Callable, Optional
import keyboard as kb

# Always release keyboard hooks on exit, even on crash
atexit.register(kb.unhook_all)

_user32 = ctypes.windll.user32

# Win32 modifier flags
_MOD_FLAGS = {"ctrl": 0x0002, "alt": 0x0001, "shift": 0x0004, "super": 0x0008}
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_KEYEVENTF_KEYUP = 0x0002

_MODIFIER_VKS = {
    "alt": (0xA4, 0xA5, 0x12),
    "ctrl": (0xA2, 0xA3, 0x11),
    "shift": (0xA0, 0xA1, 0x10),
    "super": (0x5B, 0x5C),
}

# Virtual key code lookup table
_VK_MAP: dict = {
    **{chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)},
    **{str(d): 0x30 + d for d in range(10)},
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "f12": 0x7B,
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "esc": 0x1B,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "insert": 0x2D,
    "delete": 0x2E,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "`": 0xC0,
    "-": 0xBD,
    "=": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    "\\": 0xDC,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
}


def _vk_code(key: str) -> int:
    k = key.lower()
    if k in _VK_MAP:
        return _VK_MAP[k]
    if len(key) == 1:
        result = _user32.VkKeyScanW(ord(key))
        if result != -1:
            return result & 0xFF
    return 0


class AppState(Enum):
    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"


class HotkeyManager:
    def __init__(
        self,
        hotkey: str = "alt+v",
        mode: str = "hold",
        on_start_recording: Optional[Callable] = None,
        on_stop_recording: Optional[Callable] = None,
        on_cancel_recording: Optional[Callable] = None,
        on_state_change: Optional[Callable[[AppState], None]] = None,
    ):
        self.hotkey = hotkey.lower()
        self.mode = mode
        self.on_start_recording = on_start_recording
        self.on_stop_recording = on_stop_recording
        self.on_cancel_recording = on_cancel_recording
        self.on_state_change = on_state_change

        self._state = AppState.IDLE
        self._lock = threading.Lock()
        self._registered = False
        self._polling = False

        # Win32 message loop state
        self._hotkey_thread_id: int = 0
        self._msg_loop_thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()  # set once thread has registered hotkey
        self._win32_ok = False

        # keyboard-library hook handles (so we unhook only ours, not everything)
        self._kb_hooks: list = []

        # Suppresses the bare base key while recording with a combo hotkey,
        # preventing it from being typed if the modifier is released before the base key.
        self._base_key_suppress_hook = None

        self._parse_hotkey(self.hotkey)

    # ------------------------------------------------------------------
    # Hotkey parsing
    # ------------------------------------------------------------------

    def _parse_hotkey(self, hotkey: str) -> None:
        parts = [p.strip() for p in hotkey.split("+")]
        self._base_key = parts[-1]
        self._modifiers = parts[:-1]
        self._is_combo = len(self._modifiers) > 0
        self._suppress_caps = hotkey.replace(" ", "").lower() in (
            "capslock",
            "caps_lock",
            "caps lock",
        )

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    @property
    def state(self) -> AppState:
        return self._state

    def _set_state(self, new_state: AppState) -> None:
        old = self._state
        self._state = new_state
        if old != new_state:
            print(f"[HotkeyManager] {old.value} -> {new_state.value}")
            if self.on_state_change:
                try:
                    self.on_state_change(new_state)
                except Exception as e:
                    print(f"[HotkeyManager] State callback error: {e}")

    def set_idle(self) -> None:
        with self._lock:
            self._set_state(AppState.IDLE)

    # ------------------------------------------------------------------
    # Key event handlers
    # ------------------------------------------------------------------

    def _install_base_key_suppressor(self) -> None:
        """Install a low-level hook that suppresses the bare base key during recording.

        When using a combo like Alt+V in hold mode, releasing Alt before V causes the
        OS to deliver bare V keydown events to the foreground window, typing 'v' before
        the transcription.  Suppressing V at the hook level prevents this entirely.
        """
        if not self._is_combo or not self._win32_ok:
            return
        if self._base_key_suppress_hook is not None:
            return
        try:
            self._base_key_suppress_hook = kb.on_press_key(
                self._base_key, lambda _e: None, suppress=True
            )
        except Exception as e:
            print(f"[HotkeyManager] Could not install base key suppressor: {e}")

    def _remove_base_key_suppressor(self) -> None:
        if self._base_key_suppress_hook is not None:
            try:
                kb.unhook(self._base_key_suppress_hook)
            except Exception:
                pass
            self._base_key_suppress_hook = None

    def _on_key_down(self, _event=None) -> None:
        with self._lock:
            if self.mode == "hold":
                if self._state == AppState.IDLE:
                    self._press_time = time.time()
                    self._set_state(AppState.RECORDING)
                    self._install_base_key_suppressor()
                    if self.on_start_recording:
                        threading.Thread(
                            target=self.on_start_recording, daemon=True
                        ).start()
            elif self.mode == "toggle":
                if self._state == AppState.IDLE:
                    self._set_state(AppState.RECORDING)
                    if self.on_start_recording:
                        threading.Thread(
                            target=self.on_start_recording, daemon=True
                        ).start()
                elif self._state == AppState.RECORDING:
                    self._set_state(AppState.PROCESSING)
                    if self.on_stop_recording:
                        threading.Thread(
                            target=self.on_stop_recording, daemon=True
                        ).start()

    def _on_key_up(self, _event=None) -> None:
        with self._lock:
            if self.mode == "hold" and self._state == AppState.RECORDING:
                self._remove_base_key_suppressor()
                self._release_combo_modifiers_if_needed()
                duration = time.time() - getattr(self, "_press_time", 0.0)
                if duration < 0.3:
                    self._set_state(AppState.IDLE)
                    if self.on_cancel_recording:
                        threading.Thread(
                            target=self.on_cancel_recording, daemon=True
                        ).start()
                    if self._suppress_caps:
                        threading.Thread(
                            target=self._toggle_caps_lock_threaded, daemon=True
                        ).start()
                    return
                self._set_state(AppState.PROCESSING)
                if self.on_stop_recording:
                    threading.Thread(target=self.on_stop_recording, daemon=True).start()

    def _release_combo_modifiers_if_needed(self) -> None:
        """Normalize modifier state after combo release (prevents stuck Alt/menu mode)."""
        if not self._is_combo:
            return
        if not self._win32_ok:
            return
        try:
            for mod in self._modifiers:
                for vk in _MODIFIER_VKS.get(mod, ()):
                    _user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
        except Exception:
            pass

    def _toggle_caps_lock_threaded(self) -> None:
        self.unregister()
        time.sleep(0.01)
        kb.send("caps lock")
        time.sleep(0.01)
        self.register()

    # ------------------------------------------------------------------
    # Win32 RegisterHotKey path
    # ------------------------------------------------------------------

    def _win32_register(self, mods: int, vk: int) -> bool:
        self._loop_ready.clear()
        self._hotkey_thread_id = 0
        self._win32_ok = False
        self._msg_loop_thread = threading.Thread(
            target=self._message_loop,
            args=(mods, vk),
            daemon=True,
            name="hotkey-win32",
        )
        self._msg_loop_thread.start()
        # Wait until the thread has called RegisterHotKey (or failed)
        if not self._loop_ready.wait(timeout=3.0):
            print("[HotkeyManager] Warning: message loop did not start in time")
            return False
        return self._win32_ok

    def _message_loop(self, mods: int, vk: int) -> None:
        HOTKEY_ID = 1
        if not _user32.RegisterHotKey(None, HOTKEY_ID, mods, vk):
            err = ctypes.GetLastError()
            print(
                f"[HotkeyManager] RegisterHotKey failed (error {err}) — "
                "is another app using this combo?"
            )
            self._win32_ok = False
            self._loop_ready.set()
            return

        self._win32_ok = True
        self._hotkey_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        self._loop_ready.set()  # Signal: ID is set, unregister() can safely post WM_QUIT
        print(f"[HotkeyManager] Win32 hotkey active (mods={mods:#x}, vk={vk:#x})")

        msg = _wt.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == _WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self._on_key_down()
                if self.mode == "hold":
                    threading.Thread(
                        target=self._poll_release, args=(vk,), daemon=True
                    ).start()

        _user32.UnregisterHotKey(None, HOTKEY_ID)
        self._hotkey_thread_id = 0
        self._win32_ok = False

    def _poll_release(self, vk: int) -> None:
        self._polling = True
        time.sleep(0.05)
        while self._polling and (_user32.GetAsyncKeyState(vk) & 0x8000):
            time.sleep(0.02)
        if self._polling:
            self._on_key_up()
        self._polling = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self) -> None:
        if self._registered:
            return

        self._kb_hooks = []
        registered = False

        if self._suppress_caps:
            self._kb_hooks.append(
                kb.on_press_key("caps lock", self._on_key_down, suppress=True)
            )
            self._kb_hooks.append(
                kb.on_release_key("caps lock", self._on_key_up, suppress=True)
            )
            registered = True

        elif self._is_combo:
            mods = _MOD_NOREPEAT
            for m in self._modifiers:
                mods |= _MOD_FLAGS.get(m, 0)
            vk = _vk_code(self._base_key)
            if vk == 0:
                print(
                    f"[HotkeyManager] Unknown key '{self._base_key}', "
                    "using keyboard library (no OS-level suppression)."
                )
                self._kb_hooks.append(
                    kb.on_press_key(self._base_key, self._on_key_down)
                )
                self._kb_hooks.append(
                    kb.on_release_key(self._base_key, self._on_key_up)
                )
                registered = True
            else:
                registered = self._win32_register(mods, vk)
                if not registered:
                    print(
                        "[HotkeyManager] Hotkey not active — combo was not registered."
                    )

        else:
            # Single key — use keyboard library
            self._kb_hooks.append(kb.on_press_key(self._base_key, self._on_key_down))
            self._kb_hooks.append(kb.on_release_key(self._base_key, self._on_key_up))
            registered = True

        self._registered = registered
        if registered:
            print(f"[HotkeyManager] Registered '{self.hotkey}' (mode: {self.mode})")

    def unregister(self) -> None:
        if not self._registered:
            return

        self._polling = False
        self._remove_base_key_suppressor()

        # Remove any keyboard-library hooks we installed
        for h in self._kb_hooks:
            try:
                kb.unhook(h)
            except Exception:
                pass
        self._kb_hooks = []

        # Stop Win32 message loop if running
        if self._hotkey_thread_id:
            _user32.PostThreadMessageW(self._hotkey_thread_id, _WM_QUIT, 0, 0)
        if self._msg_loop_thread:
            self._msg_loop_thread.join(timeout=2.0)
            self._msg_loop_thread = None

        self._registered = False
        print("[HotkeyManager] Unregistered")

    def update_hotkey(self, new_hotkey: str) -> None:
        """Swap to a new hotkey without losing callbacks."""
        self.unregister()
        self.hotkey = new_hotkey.lower()
        self._parse_hotkey(self.hotkey)
        self.register()
        print(f"[HotkeyManager] Hotkey updated to '{self.hotkey}'")


# ---------------------------------------------------------------------------
# TriggerHotkeyManager — simple one-shot hotkey (fires on press, no hold/release)
# Uses Win32 RegisterHotKey with HOTKEY_ID=2, coexists with HotkeyManager's ID=1.
# ---------------------------------------------------------------------------


class TriggerHotkeyManager:
    """Fires on_trigger once each time the hotkey is pressed.

    Designed for actions like "refine selection" where hold/release semantics
    are not needed.  Uses Win32 RegisterHotKey (HOTKEY_ID=2) so the combo is
    captured at the OS level without low-level hooks.
    """

    def __init__(
        self,
        hotkey: str = "alt+r",
        on_trigger: Optional[Callable] = None,
    ):
        self.hotkey = hotkey.lower()
        self.on_trigger = on_trigger
        self._registered = False
        self._hotkey_thread_id: int = 0
        self._msg_loop_thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        self._win32_ok = False
        self._kb_hooks: list = []
        self._kb_hotkeys: list = []
        self._parse_hotkey(self.hotkey)

    def _parse_hotkey(self, hotkey: str) -> None:
        parts = [p.strip() for p in hotkey.split("+")]
        self._base_key = parts[-1]
        self._modifiers = parts[:-1]
        self._is_combo = len(self._modifiers) > 0

    def _fire(self) -> None:
        if self.on_trigger:
            threading.Thread(target=self.on_trigger, daemon=True).start()

    def _message_loop(self, mods: int, vk: int) -> None:
        HOTKEY_ID = 2
        if not _user32.RegisterHotKey(None, HOTKEY_ID, mods, vk):
            err = ctypes.GetLastError()
            print(
                f"[TriggerHotkeyManager] RegisterHotKey failed (error {err}) — "
                "is another app using this combo?"
            )
            self._win32_ok = False
            self._loop_ready.set()
            return

        self._win32_ok = True
        self._hotkey_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        self._loop_ready.set()
        print(
            f"[TriggerHotkeyManager] Win32 hotkey active (mods={mods:#x}, vk={vk:#x})"
        )

        msg = _wt.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == _WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self._fire()

        _user32.UnregisterHotKey(None, HOTKEY_ID)
        self._hotkey_thread_id = 0
        self._win32_ok = False

    def register(self) -> None:
        if self._registered:
            return
        self._kb_hooks = []
        self._kb_hotkeys = []
        registered = False

        if self._is_combo:
            mods = _MOD_NOREPEAT
            for m in self._modifiers:
                mods |= _MOD_FLAGS.get(m, 0)
            vk = _vk_code(self._base_key)
            if vk:
                self._loop_ready.clear()
                self._hotkey_thread_id = 0
                self._win32_ok = False
                self._msg_loop_thread = threading.Thread(
                    target=self._message_loop,
                    args=(mods, vk),
                    daemon=True,
                    name="refine-hotkey-win32",
                )
                self._msg_loop_thread.start()
                if not self._loop_ready.wait(timeout=3.0):
                    print(
                        "[TriggerHotkeyManager] Warning: message loop did not start in time"
                    )
                    self._win32_ok = False
                registered = self._win32_ok
                if not registered:
                    try:
                        hk = kb.add_hotkey(self.hotkey, self._fire, suppress=False)
                        self._kb_hotkeys.append(hk)
                        registered = True
                        print(
                            "[TriggerHotkeyManager] Falling back to keyboard hook "
                            f"for '{self.hotkey}'"
                        )
                    except Exception as e:
                        print(
                            f"[TriggerHotkeyManager] Fallback registration failed: {e}"
                        )
            else:
                try:
                    hk = kb.add_hotkey(self.hotkey, self._fire, suppress=False)
                    self._kb_hotkeys.append(hk)
                    registered = True
                except Exception as e:
                    print(
                        "[TriggerHotkeyManager] Keyboard fallback registration failed: "
                        f"{e}"
                    )
        else:
            self._kb_hooks.append(
                kb.on_press_key(self._base_key, lambda _e: self._fire())
            )
            registered = True

        self._registered = registered
        if registered:
            print(f"[TriggerHotkeyManager] Registered '{self.hotkey}'")

    def unregister(self) -> None:
        if not self._registered:
            return
        for h in self._kb_hooks:
            try:
                kb.unhook(h)
            except Exception:
                pass
        self._kb_hooks = []
        for h in self._kb_hotkeys:
            try:
                kb.remove_hotkey(h)
            except Exception:
                pass
        self._kb_hotkeys = []
        if self._hotkey_thread_id:
            _user32.PostThreadMessageW(self._hotkey_thread_id, _WM_QUIT, 0, 0)
        if self._msg_loop_thread:
            self._msg_loop_thread.join(timeout=2.0)
            self._msg_loop_thread = None
        self._registered = False
        print("[TriggerHotkeyManager] Unregistered")

    def update_hotkey(self, new_hotkey: str) -> None:
        self.unregister()
        self.hotkey = new_hotkey.lower()
        self._parse_hotkey(self.hotkey)
        self.register()
        print(f"[TriggerHotkeyManager] Hotkey updated to '{self.hotkey}'")
