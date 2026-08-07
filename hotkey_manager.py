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

_KB_MODIFIER_NAMES = {
    "alt": "alt",
    "ctrl": "ctrl",
    "shift": "shift",
    "super": "windows",
}

# Human "same time" presses still arrive as ordered keyboard events. Keep this
# deliberately short: it catches the opposite order without turning a normal
# base-key press followed later by Alt/Ctrl into a hotkey.
_SIMULTANEOUS_CHORD_S = 0.12

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


def _mask_menu_tap() -> None:
    """Inject a no-op key (VK 0xFF press+release) into the input stream.

    RegisterHotKey swallows the base key, so the foreground app sees the
    hotkey's Alt (or Win) go down and up with nothing in between — a "clean
    tap", which Office interprets as ribbon KeyTips activation (the yellow
    letter badges in Outlook/Word) and Explorer as menu/Start activation.
    VK 0xFF maps to no character and no action, but its presence between the
    modifier's down and up breaks the clean-tap detection in every app.
    Same trick AutoHotkey/PowerToys use for their Alt-based hotkeys.
    """
    try:
        _user32.keybd_event(0xFF, 0, 0, 0)
        _user32.keybd_event(0xFF, 0, _KEYEVENTF_KEYUP, 0)
    except Exception:
        pass


def _vk_code(key: str) -> int:
    k = key.lower()
    if k in _VK_MAP:
        return _VK_MAP[k]
    if len(key) == 1:
        result = _user32.VkKeyScanW(ord(key))
        if result != -1:
            return result & 0xFF
    return 0


def _vk_is_down(vk: int) -> bool:
    return bool(vk and (_user32.GetAsyncKeyState(vk) & 0x8000))


def _modifiers_are_down(modifiers, assume_down: str = "") -> bool:
    """Return whether every modifier is physically down.

    ``assume_down`` is used from a press callback because Windows may update
    GetAsyncKeyState immediately after that callback returns.
    """
    for mod in modifiers:
        if mod == assume_down:
            continue
        vks = _MODIFIER_VKS.get(mod, ())
        if vks and not any(_vk_is_down(vk) for vk in vks):
            return False
    return True


def _simultaneous_chord_ready(base_vk: int, modifiers, base_pressed_at: float,
                              now: Optional[float] = None,
                              assume_modifier_down: str = "") -> bool:
    """True when a base-first press is still inside the chord grace window."""
    if not base_vk or base_pressed_at <= 0:
        return False
    current = time.monotonic() if now is None else now
    age = current - base_pressed_at
    return (
        0.0 <= age <= _SIMULTANEOUS_CHORD_S
        and _vk_is_down(base_vk)
        and _modifiers_are_down(modifiers, assume_modifier_down)
    )


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
        ptt_hotkey: str = "",
    ):
        self.hotkey = hotkey.lower()
        self.mode = mode
        # Optional SECOND bind with hold semantics (push-to-talk), sharing the
        # same state machine so the two binds can never start two recordings.
        # Empty string = disabled.
        self.ptt_hotkey = (ptt_hotkey or "").lower()
        self.on_start_recording = on_start_recording
        self.on_stop_recording = on_stop_recording
        self.on_cancel_recording = on_cancel_recording
        self.on_state_change = on_state_change

        self._state = AppState.IDLE
        self._lock = threading.Lock()
        self._registered = False
        self._pollers = {"main": False, "ptt": False}
        self._combo_active = {"main": False, "ptt": False}
        self._combo_base_down = {"main": False, "ptt": False}
        self._combo_base_pressed_at = {"main": 0.0, "ptt": 0.0}
        self._combo_guard = threading.Lock()
        # Which bind started the current recording ("main"/"ptt") — a release
        # or toggle-press from the OTHER bind must never stop it.
        self._rec_source = "main"

        # Win32 message loop state
        self._hotkey_thread_id: int = 0
        self._msg_loop_thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()  # set once thread has registered hotkey
        self._win32_ok = False        # main combo registered via Win32
        self._win32_ok_ptt = False

        # keyboard-library hook handles (so we unhook only ours, not everything)
        self._kb_hooks: list = []

        # Suppresses the bare base key while recording with a combo hotkey,
        # preventing it from being typed if the modifier is released before the base key.
        self._base_key_suppress_hook = None
        self._ptt_suppress_hook = None

        self._parse_hotkey(self.hotkey)
        self._parse_ptt(self.ptt_hotkey)

    # ------------------------------------------------------------------
    # Hotkey parsing
    # ------------------------------------------------------------------

    def _parse_hotkey(self, hotkey: str) -> None:
        parts = [p.strip() for p in hotkey.split("+")]
        self._base_key = parts[-1]
        self._modifiers = parts[:-1]
        self._is_combo = len(self._modifiers) > 0
        # Alt and Win activate app menus on a "clean" tap — needs masking
        self._menu_modifier = any(m in ("alt", "super") for m in self._modifiers)
        self._suppress_caps = hotkey.replace(" ", "").lower() in (
            "capslock",
            "caps_lock",
            "caps lock",
        )

    def _parse_ptt(self, hotkey: str) -> None:
        if not hotkey or hotkey == self.hotkey:
            # Same combo as the main bind would double-register — disable.
            self._ptt_base_key = ""
            self._ptt_modifiers = []
            self._ptt_is_combo = False
            self._ptt_menu_modifier = False
            return
        parts = [p.strip() for p in hotkey.split("+")]
        self._ptt_base_key = parts[-1]
        self._ptt_modifiers = parts[:-1]
        self._ptt_is_combo = len(self._ptt_modifiers) > 0
        self._ptt_menu_modifier = any(
            m in ("alt", "super") for m in self._ptt_modifiers)

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

    def _combo_parts(self, source: str):
        if source == "ptt":
            return (self._ptt_base_key, self._ptt_modifiers,
                    self._ptt_menu_modifier)
        return self._base_key, self._modifiers, self._menu_modifier

    def _observe_combo_base_press(self, _event=None,
                                  source: str = "main") -> None:
        """Activate whether the modifier or base key arrived first."""
        if not self._combo_base_down[source]:
            self._combo_base_pressed_at[source] = time.monotonic()
            self._combo_base_down[source] = True
        _base, modifiers, _menu = self._combo_parts(source)
        if _modifiers_are_down(modifiers):
            self._activate_win32_combo(source)

    def _observe_combo_base_release(self, _event=None,
                                    source: str = "main") -> None:
        self._combo_base_down[source] = False
        self._combo_base_pressed_at[source] = 0.0

    def _observe_combo_modifier_press(self, _event=None,
                                      source: str = "main",
                                      modifier: str = "") -> None:
        base, modifiers, _menu = self._combo_parts(source)
        if _simultaneous_chord_ready(
            _vk_code(base), modifiers, self._combo_base_pressed_at[source],
            assume_modifier_down=modifier,
        ):
            self._activate_win32_combo(source)

    def _activate_win32_combo(self, source: str) -> None:
        """Latch one physical chord so hooks and WM_HOTKEY cannot double-fire."""
        base, _modifiers, menu_modifier = self._combo_parts(source)
        vk = _vk_code(base)
        if not vk:
            return
        with self._combo_guard:
            if self._combo_active[source]:
                return
            self._combo_active[source] = True
        if menu_modifier:
            _mask_menu_tap()
        self._on_key_down(source=source)
        if not self._pollers[source]:
            self._pollers[source] = True
            threading.Thread(
                target=self._poll_release, args=(vk, source), daemon=True
            ).start()

    def _install_simultaneous_combo_observers(self) -> None:
        """Observe combo parts without suppressing ordinary base-key typing."""
        sources = []
        if self._win32_ok:
            sources.append("main")
        if self._win32_ok_ptt:
            sources.append("ptt")
        for source in sources:
            base, modifiers, _menu = self._combo_parts(source)
            try:
                self._kb_hooks.append(kb.on_press_key(
                    base,
                    lambda event, s=source: self._observe_combo_base_press(
                        event, source=s),
                    suppress=False,
                ))
                self._kb_hooks.append(kb.on_release_key(
                    base,
                    lambda event, s=source: self._observe_combo_base_release(
                        event, source=s),
                    suppress=False,
                ))
                for modifier in dict.fromkeys(modifiers):
                    key_name = _KB_MODIFIER_NAMES.get(modifier, modifier)
                    self._kb_hooks.append(kb.on_press_key(
                        key_name,
                        lambda event, s=source, m=modifier:
                            self._observe_combo_modifier_press(
                                event, source=s, modifier=m),
                        suppress=False,
                    ))
            except Exception as e:
                # RegisterHotKey remains operational in modifier-first order if
                # optional chord observers cannot be installed.
                print(
                    "[HotkeyManager] Simultaneous chord observer unavailable "
                    f"for {source}: {e}"
                )

    def _install_base_key_suppressor(self) -> None:
        """Install a conditional hook that suppresses the base key ONLY during recording.

        Installed eagerly at register() time so there is zero timing gap between
        WM_HOTKEY firing and the suppressor being active. Chrome (and other apps
        using raw-input or WH_KEYBOARD_LL) see key events before the Win32 message
        queue, so RegisterHotKey alone doesn't stop V-repeats from leaking into the
        foreground window during hold mode.

        The keyboard library suppresses a key when the blocking_key hook returns
        falsy. We return None (falsy) while recording and True (truthy) otherwise,
        giving us conditional suppression with no timing gap.
        """
        if not self._is_combo or not self._win32_ok:
            return
        if self._base_key_suppress_hook is not None:
            return

        def _should_suppress(_event):
            # Only suppress in hold mode — toggle mode needs the key through so the
            # second Alt+V press can fire WM_HOTKEY and stop recording. Never
            # suppress while the PTT bind owns the recording (typing the main
            # base key elsewhere must keep working).
            return None if (self._state == AppState.RECORDING
                            and self.mode == "hold"
                            and self._rec_source == "main") else True

        try:
            self._base_key_suppress_hook = kb.on_press_key(
                self._base_key, _should_suppress, suppress=True
            )
        except Exception as e:
            print(f"[HotkeyManager] Could not install base key suppressor: {e}")

    def _install_ptt_suppressor(self) -> None:
        """Suppress the PTT base key while a PTT recording is live — hold mode
        auto-repeats the base key into the focused app otherwise."""
        if not self._ptt_is_combo or not self._win32_ok_ptt:
            return
        if self._ptt_suppress_hook is not None:
            return
        if self._ptt_base_key == self._base_key:
            return  # the main suppressor's hook already owns this key

        def _should_suppress(_event):
            return None if (self._state == AppState.RECORDING
                            and self._rec_source == "ptt") else True

        try:
            self._ptt_suppress_hook = kb.on_press_key(
                self._ptt_base_key, _should_suppress, suppress=True
            )
        except Exception as e:
            print(f"[HotkeyManager] Could not install PTT suppressor: {e}")

    def _remove_base_key_suppressor(self) -> None:
        if self._base_key_suppress_hook is not None:
            try:
                kb.unhook(self._base_key_suppress_hook)
            except Exception:
                pass
            self._base_key_suppress_hook = None
        if self._ptt_suppress_hook is not None:
            try:
                kb.unhook(self._ptt_suppress_hook)
            except Exception:
                pass
            self._ptt_suppress_hook = None

    def _on_key_down(self, _event=None, source: str = "main") -> None:
        with self._lock:
            hold = (source == "ptt") or self.mode == "hold"
            if hold:
                if self._state == AppState.IDLE:
                    self._press_time = time.time()
                    self._rec_source = source
                    self._set_state(AppState.RECORDING)
                    if self.on_start_recording:
                        threading.Thread(
                            target=self.on_start_recording, daemon=True
                        ).start()
                # else: the other bind owns an active recording — ignore.
            else:  # toggle semantics (main bind only)
                # Debounce: single-key hotkeys (F-keys, CapsLock fallback) auto-
                # repeat while held, firing _on_key_down every ~30ms — without
                # this a held key toggles recording on/off repeatedly.
                now = time.time()
                if now - getattr(self, "_last_toggle_ts", 0.0) < 0.35:
                    return
                self._last_toggle_ts = now
                if self._state == AppState.IDLE:
                    self._rec_source = "main"
                    self._set_state(AppState.RECORDING)
                    if self.on_start_recording:
                        threading.Thread(
                            target=self.on_start_recording, daemon=True
                        ).start()
                elif (self._state == AppState.RECORDING
                      and self._rec_source == "main"):
                    self._set_state(AppState.PROCESSING)
                    if self.on_stop_recording:
                        threading.Thread(
                            target=self.on_stop_recording, daemon=True
                        ).start()

    def _on_key_up(self, _event=None, source: str = "main") -> None:
        with self._lock:
            hold = (source == "ptt") or self.mode == "hold"
            if (hold and self._state == AppState.RECORDING
                    and self._rec_source == source):
                self._release_combo_modifiers_if_needed(source)
                duration = time.time() - getattr(self, "_press_time", 0.0)
                if duration < 0.3:
                    self._set_state(AppState.IDLE)
                    if self.on_cancel_recording:
                        threading.Thread(
                            target=self.on_cancel_recording, daemon=True
                        ).start()
                    if self._suppress_caps and source == "main":
                        threading.Thread(
                            target=self._toggle_caps_lock_threaded, daemon=True
                        ).start()
                    return
                self._set_state(AppState.PROCESSING)
                if self.on_stop_recording:
                    threading.Thread(target=self.on_stop_recording, daemon=True).start()

    def _release_combo_modifiers_if_needed(self, source: str = "main") -> None:
        """Normalize modifier state after combo release (prevents stuck Alt/menu mode)."""
        if source == "ptt":
            is_combo, win_ok = self._ptt_is_combo, self._win32_ok_ptt
            menu_mod, mods = self._ptt_menu_modifier, self._ptt_modifiers
        else:
            is_combo, win_ok = self._is_combo, self._win32_ok
            menu_mod, mods = self._menu_modifier, self._modifiers
        if not is_combo or not win_ok:
            return
        try:
            # An injected Alt-up with nothing before it reads as a completed
            # clean Alt tap — mask first or Office pops KeyTips right here.
            if menu_mod:
                _mask_menu_tap()
            for mod in mods:
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

    _MAIN_ID = 1
    _PTT_ID = 3   # HOTKEY_ID=2 belongs to TriggerHotkeyManager

    def _win32_register(self, entries) -> bool:
        """Start ONE message loop registering every (id, mods, vk, source)
        entry. Returns True if the MAIN entry registered (or wasn't in the
        list); per-source success lands in _win32_ok/_win32_ok_ptt."""
        self._loop_ready.clear()
        self._hotkey_thread_id = 0
        self._win32_ok = False
        self._win32_ok_ptt = False
        self._msg_loop_thread = threading.Thread(
            target=self._message_loop,
            args=(entries,),
            daemon=True,
            name="hotkey-win32",
        )
        self._msg_loop_thread.start()
        # Wait until the thread has called RegisterHotKey (or failed)
        if not self._loop_ready.wait(timeout=3.0):
            print("[HotkeyManager] Warning: message loop did not start in time")
            return False
        has_main = any(src == "main" for _i, _m, _v, src in entries)
        return self._win32_ok if has_main else True

    def _message_loop(self, entries) -> None:
        by_id = {}
        ok_ids = []
        for hid, mods, vk, source in entries:
            if _user32.RegisterHotKey(None, hid, mods, vk):
                by_id[hid] = (vk, source)
                ok_ids.append(hid)
                if source == "main":
                    self._win32_ok = True
                else:
                    self._win32_ok_ptt = True
                print(f"[HotkeyManager] Win32 hotkey active "
                      f"({source}, mods={mods:#x}, vk={vk:#x})")
            else:
                err = ctypes.GetLastError()
                print(
                    f"[HotkeyManager] RegisterHotKey failed for {source} "
                    f"(error {err}) — is another app using this combo?"
                )
        if not ok_ids:
            self._loop_ready.set()
            return

        self._hotkey_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        self._loop_ready.set()  # Signal: ID is set, unregister() can safely post WM_QUIT

        msg = _wt.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == _WM_HOTKEY and msg.wParam in by_id:
                _vk, source = by_id[msg.wParam]
                self._activate_win32_combo(source)

        for hid in ok_ids:
            _user32.UnregisterHotKey(None, hid)
        self._hotkey_thread_id = 0
        self._win32_ok = False
        self._win32_ok_ptt = False

    def _poll_release(self, vk: int, source: str = "main") -> None:
        time.sleep(0.02)  # 20ms: enough to let hardware state settle after WM_HOTKEY
        _up_count = 0
        while self._pollers[source]:
            _base, modifiers, _menu = self._combo_parts(source)
            # The physical chord ends when either part is released, so release
            # order is as flexible as press order.
            if _vk_is_down(vk) and _modifiers_are_down(modifiers):
                _up_count = 0
            else:
                _up_count += 1
                if _up_count >= 2:  # 40ms of consistent key-up = real release
                    break
            time.sleep(0.02)
        hold = (source == "ptt") or self.mode == "hold"
        if self._pollers[source] and hold:
            self._on_key_up(source=source)
        self._pollers[source] = False
        with self._combo_guard:
            self._combo_active[source] = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self) -> None:
        if self._registered:
            return

        self._kb_hooks = []
        registered = False
        win32_entries = []
        main_in_win32 = False

        # The PTT combo rides in the same Win32 message loop when it can.
        ptt_vk = _vk_code(self._ptt_base_key) if self._ptt_base_key else 0
        if self._ptt_base_key and self._ptt_is_combo and ptt_vk:
            pmods = _MOD_NOREPEAT
            for m in self._ptt_modifiers:
                pmods |= _MOD_FLAGS.get(m, 0)
            win32_entries.append((self._PTT_ID, pmods, ptt_vk, "ptt"))

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
                win32_entries.insert(0, (self._MAIN_ID, mods, vk, "main"))
                main_in_win32 = True
                registered = self._win32_register(win32_entries)
                if registered:
                    self._install_base_key_suppressor()
                else:
                    # RegisterHotKey failed (another app owns the combo). Fall
                    # back to the keyboard library so the app is never left with
                    # NO dictation hotkey at all — combo detection still works,
                    # just without OS-level suppression.
                    try:
                        self._kb_hooks.append(
                            kb.on_press_key(self._base_key, self._kb_combo_down)
                        )
                        self._kb_hooks.append(
                            kb.on_release_key(self._base_key, self._kb_combo_up)
                        )
                        # Without this, every dictation leaks the literal base
                        # key ("v" — with auto-repeat in hold mode) into the
                        # focused document while recording.
                        self._install_base_key_suppressor()
                        registered = True
                        print(
                            "[HotkeyManager] Win32 combo unavailable — using "
                            "keyboard-hook fallback (base key suppressed)."
                        )
                    except Exception as e:
                        print(f"[HotkeyManager] Fallback registration failed: {e}")

        else:
            # Single key — use keyboard library. suppress=True so the key does
            # not leak a character into the focused app on every press/release.
            try:
                self._kb_hooks.append(
                    kb.on_press_key(self._base_key, self._on_key_down, suppress=True))
                self._kb_hooks.append(
                    kb.on_release_key(self._base_key, self._on_key_up, suppress=True))
            except Exception:
                # Some keys can't be hooked with suppression — degrade gracefully
                self._kb_hooks.append(kb.on_press_key(self._base_key, self._on_key_down))
                self._kb_hooks.append(kb.on_release_key(self._base_key, self._on_key_up))
            registered = True

        # PTT registration when the main bind didn't start the Win32 loop
        # (caps / single-key / unknown-key main paths).
        if win32_entries and not main_in_win32:
            self._win32_register(win32_entries)
        if self._win32_ok_ptt:
            self._install_ptt_suppressor()
        elif self._ptt_base_key and not self._ptt_is_combo:
            # Single-key PTT (e.g. an F-key): keyboard-library hold semantics.
            try:
                self._kb_hooks.append(kb.on_press_key(
                    self._ptt_base_key,
                    lambda _e: self._on_key_down(source="ptt"), suppress=True))
                self._kb_hooks.append(kb.on_release_key(
                    self._ptt_base_key,
                    lambda _e: self._on_key_up(source="ptt"), suppress=True))
            except Exception:
                self._kb_hooks.append(kb.on_press_key(
                    self._ptt_base_key,
                    lambda _e: self._on_key_down(source="ptt")))
                self._kb_hooks.append(kb.on_release_key(
                    self._ptt_base_key,
                    lambda _e: self._on_key_up(source="ptt")))

        if self._win32_ok or self._win32_ok_ptt:
            self._install_simultaneous_combo_observers()

        self._registered = registered
        if registered:
            ptt_note = (f" + PTT '{self.ptt_hotkey}'"
                        if self._ptt_base_key else "")
            print(f"[HotkeyManager] Registered '{self.hotkey}' "
                  f"(mode: {self.mode}){ptt_note}")

    def _kb_combo_down(self, _event=None) -> None:
        """Keyboard-library fallback for combos: fire only when all modifiers
        are physically held (RegisterHotKey did this filtering for us)."""
        if self._combo_modifiers_held():
            self._on_key_down()

    def _kb_combo_up(self, _event=None) -> None:
        if self.mode == "hold" and self._state == AppState.RECORDING:
            self._on_key_up()

    def _combo_modifiers_held(self) -> bool:
        for mod in self._modifiers:
            vks = _MODIFIER_VKS.get(mod, ())
            if vks and not any(_user32.GetAsyncKeyState(vk) & 0x8000 for vk in vks):
                return False
        return True

    def unregister(self) -> None:
        if not self._registered:
            return

        # Unregistering mid-recording would kill the release-poller without
        # ever firing key-up, wedging the state machine at RECORDING. Cancel
        # the recording first so downstream state stays consistent.
        fire_cancel = False
        with self._lock:
            if self._state == AppState.RECORDING:
                self._set_state(AppState.IDLE)
                fire_cancel = True
        if fire_cancel and self.on_cancel_recording:
            threading.Thread(target=self.on_cancel_recording, daemon=True).start()

        self._pollers["main"] = False
        self._pollers["ptt"] = False
        with self._combo_guard:
            self._combo_active["main"] = False
            self._combo_active["ptt"] = False
        self._combo_base_down["main"] = False
        self._combo_base_down["ptt"] = False
        self._combo_base_pressed_at["main"] = 0.0
        self._combo_base_pressed_at["ptt"] = 0.0
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
        # A PTT combo identical to the new main combo silently disables itself
        # (and re-enables if the main moves away again) — re-parse.
        self._parse_ptt(self.ptt_hotkey)
        self.register()
        print(f"[HotkeyManager] Hotkey updated to '{self.hotkey}'")

    def update_ptt_hotkey(self, new_hotkey: str) -> None:
        """Swap/set/clear the push-to-talk bind ('' disables it)."""
        self.unregister()
        self.ptt_hotkey = (new_hotkey or "").lower()
        self._parse_ptt(self.ptt_hotkey)
        self.register()
        print(f"[HotkeyManager] PTT hotkey updated to "
              f"'{self.ptt_hotkey or '(disabled)'}'")


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
        self._combo_active = False
        self._combo_base_down = False
        self._combo_base_pressed_at = 0.0
        self._combo_guard = threading.Lock()
        self._parse_hotkey(self.hotkey)

    def _parse_hotkey(self, hotkey: str) -> None:
        parts = [p.strip() for p in hotkey.split("+")]
        self._base_key = parts[-1]
        self._modifiers = parts[:-1]
        self._is_combo = len(self._modifiers) > 0
        self._menu_modifier = any(m in ("alt", "super") for m in self._modifiers)

    def _fire(self) -> None:
        if self.on_trigger:
            threading.Thread(target=self.on_trigger, daemon=True).start()

    def _activate_combo(self) -> None:
        """Fire once per physical chord, regardless of press ordering."""
        with self._combo_guard:
            if self._combo_active:
                return
            self._combo_active = True
        if self._menu_modifier:
            _mask_menu_tap()
        self._fire()
        vk = _vk_code(self._base_key)
        if vk:
            threading.Thread(
                target=self._wait_for_combo_release, args=(vk,), daemon=True
            ).start()

    def _wait_for_combo_release(self, vk: int) -> None:
        time.sleep(0.02)
        up_count = 0
        while self._registered and self._combo_active:
            if _vk_is_down(vk) and _modifiers_are_down(self._modifiers):
                up_count = 0
            else:
                up_count += 1
                if up_count >= 2:
                    break
            time.sleep(0.02)
        with self._combo_guard:
            self._combo_active = False

    def _observe_combo_base_press(self, _event=None) -> None:
        if not self._combo_base_down:
            self._combo_base_pressed_at = time.monotonic()
            self._combo_base_down = True
        if _modifiers_are_down(self._modifiers):
            self._activate_combo()

    def _observe_combo_base_release(self, _event=None) -> None:
        self._combo_base_down = False
        self._combo_base_pressed_at = 0.0

    def _observe_combo_modifier_press(self, _event=None,
                                      modifier: str = "") -> None:
        if _simultaneous_chord_ready(
            _vk_code(self._base_key), self._modifiers,
            self._combo_base_pressed_at,
            assume_modifier_down=modifier,
        ):
            self._activate_combo()

    def _install_simultaneous_combo_observers(self) -> None:
        try:
            self._kb_hooks.append(kb.on_press_key(
                self._base_key, self._observe_combo_base_press, suppress=False))
            self._kb_hooks.append(kb.on_release_key(
                self._base_key, self._observe_combo_base_release,
                suppress=False))
            for modifier in dict.fromkeys(self._modifiers):
                key_name = _KB_MODIFIER_NAMES.get(modifier, modifier)
                self._kb_hooks.append(kb.on_press_key(
                    key_name,
                    lambda event, m=modifier:
                        self._observe_combo_modifier_press(event, modifier=m),
                    suppress=False,
                ))
        except Exception as e:
            print(
                "[TriggerHotkeyManager] Simultaneous chord observer "
                f"unavailable: {e}"
            )

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
                self._activate_combo()

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
                        hk = kb.add_hotkey(
                            self.hotkey, self._activate_combo, suppress=False)
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
                    hk = kb.add_hotkey(
                        self.hotkey, self._activate_combo, suppress=False)
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
        if registered and self._is_combo:
            self._install_simultaneous_combo_observers()
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
        with self._combo_guard:
            self._combo_active = False
        self._combo_base_down = False
        self._combo_base_pressed_at = 0.0
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
