"""
Floating UI popup with three modes:
  - status:     small pill during recording / transcribing
  - icon:       small clickable pencil badge near the cursor after injection
  - refinement: full AI refinement panel (triggered by clicking the icon)
"""

import ctypes
import ctypes.wintypes
import threading
import tkinter as tk
from typing import Callable, Optional

# ctypes structs at module level — avoids re-defining classes on every call
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.wintypes.DWORD),
                ("rcMonitor", _RECT), ("rcWork", _RECT),
                ("dwFlags", ctypes.wintypes.DWORD)]

# FTC brand palette
C = {
    "bg":           "#4e4e4c",
    "surface":      "#3a3a38",
    "hover":        "#5e5e5c",
    "text":         "#ffffff",
    "subtext":      "#dadada",
    "accent":       "#f39200",
    "accent_hover": "#d98200",
    "divider":      "#6e6e6c",
    "btn_bg":       "#3a3a38",
    "btn_hover":    "#5e5e5c",
}


class FloatingPopup:
    def __init__(self):
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run_tk, daemon=True)
        self._thread.start()
        self._ready.wait()

        self._mode: Optional[str] = None
        self._on_replace: Optional[Callable] = None
        self._target_hwnd: int = 0
        self._cursor_x: int = 0
        self._cursor_y: int = 0
        self._ai_refiner = None
        self._original_text: str = ""
        self._current_result: Optional[str] = None
        self._ai_busy: bool = False  # Prevents stacked concurrent API calls

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_ai_refiner(self, refiner) -> None:
        self._ai_refiner = refiner

    def show_status(self, text: str, hwnd: int = 0) -> None:
        self._target_hwnd = hwnd
        if self.root:
            self.root.after(0, self._enter_status_mode, text)

    def show_cursor_icon(self, text: str, on_replace: Callable[[str], None], hwnd: int = 0) -> None:
        """Show the small pencil icon near the cursor. Full panel opens on click."""
        self._on_replace = on_replace
        self._target_hwnd = hwnd
        self._original_text = text
        self._current_result = None
        self._cursor_x, self._cursor_y = self._get_cursor_pos()
        if self.root:
            self.root.after(0, self._enter_icon_mode)

    def hide(self) -> None:
        if self.root:
            self.root.after(0, self._do_hide)

    @property
    def is_user_facing(self) -> bool:
        """True while the icon or refinement panel is visible (user may be interacting)."""
        return self._mode in ("icon", "refinement")

    # ------------------------------------------------------------------
    # Tkinter setup
    # ------------------------------------------------------------------

    def _run_tk(self) -> None:
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.96)
        self.root.attributes("-toolwindow", True)  # no taskbar entry, no focus steal
        self.root.configure(bg=C["bg"])

        self._build_status_frame()
        self._build_icon_frame()
        self._build_refinement_frame()

        self.root.withdraw()
        self._ready.set()
        self.root.mainloop()

    def _build_status_frame(self) -> None:
        self._status_frame = tk.Frame(self.root, bg=C["bg"], padx=22, pady=12)
        self._status_label = tk.Label(
            self._status_frame,
            text="",
            fg=C["subtext"],
            bg=C["bg"],
            font=("Segoe UI", 13, "bold"),
        )
        self._status_label.pack()

    def _build_icon_frame(self) -> None:
        """Small FTC-branded badge near cursor — click to open refinement panel."""
        bg = C["bg"]
        self._icon_frame = tk.Frame(self.root, bg=bg, padx=6, pady=4)

        from logo_cache import get_logo_photo
        self._icon_photo = get_logo_photo(self.root, bg, max_w=72, max_h=28)

        if self._icon_photo:
            lbl = tk.Label(self._icon_frame, image=self._icon_photo, bg=bg, cursor="hand2")
        else:
            lbl = tk.Label(
                self._icon_frame,
                text="FTC",
                fg=C["accent"],
                bg=bg,
                font=("Segoe UI", 9, "bold"),
                cursor="hand2",
            )
        lbl.pack(side="left", padx=(0, 2))

        # Divider between logo and X
        tk.Frame(self._icon_frame, bg=C["divider"], width=1).pack(
            side="left", fill="y", padx=(2, 4))

        # ✕ dismiss button — stop event propagation so it doesn't open the panel
        close = tk.Label(
            self._icon_frame, text="✕",
            fg=C["subtext"], bg=bg,
            font=("Segoe UI", 9, "bold"), cursor="hand2", padx=3,
        )
        close.pack(side="left")

        def _close_click(_e):
            self.root.after(0, self._do_hide)
            return "break"  # stop propagation — prevents _expand_to_panel from firing

        close.bind("<Button-1>", _close_click)
        close.bind("<Enter>", lambda _e: close.configure(fg=C["accent"]))
        close.bind("<Leave>", lambda _e: close.configure(fg=C["subtext"]))

        for w in (self._icon_frame, lbl):
            w.bind("<Button-1>", lambda _e: self.root.after(0, self._expand_to_panel))
            w.bind("<Enter>",    lambda _e: self._icon_frame.configure(bg=C["hover"]))
            w.bind("<Leave>",    lambda _e: self._icon_frame.configure(bg=C["bg"]))

        self._space_hook = None

    def _build_refinement_frame(self) -> None:
        f = tk.Frame(self.root, bg=C["bg"], padx=18, pady=14)
        self._refine_frame = f

        # Top row: badge + AI buttons + close
        top = tk.Frame(f, bg=C["bg"])
        top.pack(fill="x", pady=(0, 8))

        tk.Label(
            top, text="  ✓ Inserted  ",
            fg=C["bg"], bg=C["accent"],
            font=("Segoe UI", 9, "bold"),
            padx=4, pady=3,
        ).pack(side="left", padx=(0, 12))

        for label, mode in [
            ("✉  Email",      "email"),
            ("🎩 Formal",     "formal"),
            ("💬 Casual",     "casual"),
            ("✨ Fix",        "punctuation"),
            ("✂  Short",     "concise"),
            ("⚡ Optimise",  "prompt_optimiser"),
        ]:
            self._btn(top, label, lambda m=mode: self._run_ai(m)).pack(side="left", padx=(0, 5))

        close = tk.Label(
            top, text="✕",
            fg=C["subtext"], bg=C["bg"],
            font=("Segoe UI", 13), cursor="hand2", padx=8,
        )
        close.pack(side="right")
        close.bind("<Button-1>", lambda _e: self._do_hide())
        close.bind("<Enter>", lambda _e: close.configure(fg=C["accent"]))
        close.bind("<Leave>", lambda _e: close.configure(fg=C["subtext"]))

        # AI loading indicator
        self._ai_status = tk.Label(
            f, text="",
            fg=C["accent"], bg=C["bg"],
            font=("Segoe UI", 10, "italic"),
        )
        self._ai_status.pack(anchor="w")

        # Result area — hidden until AI responds
        self._result_frame = tk.Frame(f, bg=C["bg"])

        tk.Frame(self._result_frame, bg=C["divider"], height=1).pack(fill="x", pady=(4, 8))

        self._result_text = tk.Label(
            self._result_frame,
            text="",
            fg=C["subtext"], bg=C["bg"],
            font=("Segoe UI", 12),
            wraplength=520, justify="left",
        )
        self._result_text.pack(anchor="w")

        btn_row = tk.Frame(self._result_frame, bg=C["bg"])
        btn_row.pack(fill="x", pady=(8, 0))

        replace = tk.Label(
            btn_row, text="  ↩  Replace  ",
            fg=C["bg"], bg=C["accent"],
            font=("Segoe UI", 10, "bold"),
            padx=10, pady=6, cursor="hand2",
        )
        replace.pack(side="left")
        replace.bind("<Button-1>", lambda _e: self._do_replace())
        replace.bind("<Enter>", lambda _e: replace.configure(bg=C["accent_hover"]))
        replace.bind("<Leave>", lambda _e: replace.configure(bg=C["accent"]))

        self.root.bind("<Escape>", lambda _e: self._do_hide())

    def _btn(self, parent: tk.Frame, text: str, command: Callable) -> tk.Label:
        b = tk.Label(
            parent, text=text,
            fg=C["subtext"], bg=C["btn_bg"],
            font=("Segoe UI", 10),
            padx=9, pady=5, cursor="hand2",
        )
        b.bind("<Button-1>", lambda _e: command())
        b.bind("<Enter>", lambda _e: b.configure(fg=C["accent"], bg=C["btn_hover"]))
        b.bind("<Leave>", lambda _e: b.configure(fg=C["subtext"], bg=C["btn_bg"]))
        return b

    # ------------------------------------------------------------------
    # Mode transitions
    # ------------------------------------------------------------------

    def _hide_all_frames(self) -> None:
        for frame in (self._status_frame, self._icon_frame, self._refine_frame):
            frame.pack_forget()

    def _enter_status_mode(self, text: str) -> None:
        self._hide_all_frames()
        self._status_label.configure(text=text)
        self._status_frame.pack()
        self._mode = "status"
        self._reposition()
        self.root.deiconify()
        self.root.lift()

    def _enter_icon_mode(self) -> None:
        self._hide_all_frames()
        self._result_frame.pack_forget()
        self._ai_status.configure(text="")
        self._icon_frame.pack()
        self._mode = "icon"
        self._reposition(self._cursor_x, self._cursor_y, near_cursor=True)
        self.root.deiconify()
        self.root.lift()
        self._register_space_dismiss()

    def _expand_to_panel(self) -> None:
        self._hide_all_frames()
        self._result_frame.pack_forget()
        self._ai_status.configure(text="")
        self._refine_frame.pack()
        self._mode = "refinement"
        self._reposition(self._cursor_x, self._cursor_y)
        self.root.lift()

    def _register_space_dismiss(self) -> None:
        """Dismiss the icon when the user presses Space (lets the key through)."""
        try:
            import keyboard as kb
            self._unregister_space_dismiss()
            def _on_space(_e):
                self._unregister_space_dismiss()
                if self.root:
                    self.root.after(0, self._do_hide)
            self._space_hook = kb.on_press_key("space", _on_space, suppress=False)
        except Exception:
            pass

    def _unregister_space_dismiss(self) -> None:
        if self._space_hook is not None:
            try:
                import keyboard as kb
                kb.unhook(self._space_hook)
            except Exception:
                pass
            self._space_hook = None

    def _do_hide(self) -> None:
        self._unregister_space_dismiss()
        self._mode = None
        self._ai_busy = False
        self._hide_all_frames()
        self.root.withdraw()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _do_replace(self) -> None:
        if self._current_result and self._on_replace:
            result = self._current_result
            self._do_hide()
            threading.Thread(target=self._on_replace, args=(result,), daemon=True).start()

    # ------------------------------------------------------------------
    # AI refinement
    # ------------------------------------------------------------------

    def _run_ai(self, mode: str) -> None:
        if not self._ai_refiner or not self._ai_refiner.is_available:
            self._ai_status.configure(text="⚠  Set ANTHROPIC_API_KEY to enable AI refinement")
            return
        if self._ai_busy:
            return

        self._ai_busy = True
        self._ai_status.configure(text=f"✦  Refining ({mode})…")
        self._result_frame.pack_forget()
        text = self._original_text

        def _worker():
            result = self._ai_refiner.refine(text, mode)
            self.root.after(0, self._show_ai_result, result)

        threading.Thread(target=_worker, daemon=True).start()

    def _show_ai_result(self, text: str) -> None:
        self._ai_busy = False
        self._current_result = text
        self._ai_status.configure(text="")
        display = text if len(text) <= 140 else text[:137] + "…"
        self._result_text.configure(text=display)
        self._result_frame.pack(fill="x")
        self._reposition(self._cursor_x, self._cursor_y)

    # ------------------------------------------------------------------
    # Positioning
    # ------------------------------------------------------------------

    @staticmethod
    def _get_cursor_pos() -> tuple[int, int]:
        try:
            pt = _POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            return pt.x, pt.y
        except Exception:
            return 0, 0

    def _get_monitor_workarea(self, x: int = 0, y: int = 0) -> tuple[int, int, int, int]:
        """Work area (excludes taskbar) of the monitor containing the active window."""
        try:
            MONITOR_DEFAULTTONEAREST = 2
            if self._target_hwnd:
                hmon = ctypes.windll.user32.MonitorFromWindow(
                    self._target_hwnd, MONITOR_DEFAULTTONEAREST)
            else:
                pt = _POINT()
                pt.x, pt.y = x, y
                hmon = ctypes.windll.user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info))
            r = info.rcWork
            return r.left, r.top, r.right, r.bottom
        except Exception:
            return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _reposition(self, cx: int = 0, cy: int = 0, near_cursor: bool = False) -> None:
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        left, top, right, bottom = self._get_monitor_workarea(cx, cy)
        # Only use cursor position if it was successfully determined
        if near_cursor and cx > 0 and cy > 0:
            x = max(left, min(cx + 10, right - w))
            y = max(top, min(cy - h - 12, bottom - h))
        else:
            x = left + (right - left - w) // 2
            y = bottom - h - 130
        self.root.geometry(f"+{x}+{y}")
