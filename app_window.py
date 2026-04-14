"""
FTC Whisper — Main application window.

Dashboard: Home / Hotkey / History tabs.
Dark theme with rounded-corner cards via Canvas.
"""

import threading
import tkinter as tk
from datetime import datetime
from typing import Callable, Optional
import ctypes

# ── Dark colour palette ───────────────────────────────────────────────────────
C = {
    "bg":            "#0d0d0d",   # near-black window background
    "surface":       "#1a1a1a",   # card surface
    "surface_hover": "#242424",   # card hover / active
    "input_bg":      "#141414",   # entry fields
    "text":          "#ffffff",   # primary text
    "subtext":       "#777777",   # secondary / hint text
    "accent":        "#f39200",   # FTC orange
    "accent_hover":  "#e08200",   # darker orange
    "accent_dim":    "#3d2600",   # very muted orange (badge bg)
    "error":         "#ff5555",
    "success":       "#4ade80",
    "divider":       "#1f1f1f",   # hairline separator
    "border":        "#2d2d2d",   # card border
    "scrollbar":     "#2d2d2d",
}

WINDOW_W = 420
DASH_H   = 560


# ── Rounded card helper ───────────────────────────────────────────────────────

def _rr(canvas, x1, y1, x2, y2, r, **kw):
    """Draw a smooth rounded rectangle on a Canvas."""
    pts = (
        x1+r, y1,   x2-r, y1,   x2,   y1,
        x2,   y1+r, x2,   y2-r, x2,   y2,
        x2-r, y2,   x1+r, y2,   x1,   y2,
        x1,   y2-r, x1,   y1+r, x1,   y1,
    )
    return canvas.create_polygon(pts, smooth=True, **kw)


class AppWindow:
    _STATUS = {
        "idle":       ("● Ready",         C["success"]),
        "recording":  ("● Recording…",    "#ff5555"),
        "processing": ("⚙ Transcribing…", C["accent"]),
    }

    def __init__(
        self,
        auth,
        on_authenticated: Callable,
        on_sign_out: Callable,
        on_open_config: Callable,
        on_quit: Callable,
        on_hotkey_change: Callable,
        db=None,
        hotkey: str = "alt+v",
    ):
        self._auth             = auth
        self._on_authenticated = on_authenticated
        self._on_sign_out      = on_sign_out
        self._open_config_cb   = on_open_config
        self._on_quit          = on_quit
        self._on_hotkey_change = on_hotkey_change
        self._db               = db
        self._hotkey           = hotkey.upper()
        self._root: Optional[tk.Tk] = None

        # Hotkey recorder state
        self._recording_hotkey = False
        self._pending_hotkey: Optional[str] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self) -> None:
        self._root = tk.Tk()
        self._root.title("FTC Whisper")
        self._root.configure(bg=C["bg"])
        self._root.resizable(False, False)
        self._root.protocol("WM_DELETE_WINDOW", self._hide)

        self._apply_dark_titlebar()

        self._build_header()

        self._dash_frame = tk.Frame(self._root, bg=C["bg"])
        self._build_dashboard(self._dash_frame)
        self._show_dashboard()
        self._root.after(50, self._fire_authenticated)

        self._root.mainloop()

    def show(self) -> None:
        if self._root:
            self._root.after(0, self._do_show)

    def _do_show(self) -> None:
        self._root.deiconify()
        self._root.lift()
        self._root.focus_force()

    def update_status(self, state: str) -> None:
        if self._root and hasattr(self, "_status_lbl"):
            text, color = self._STATUS.get(state, ("● Ready", C["success"]))
            self._root.after(0, lambda: self._status_lbl.configure(text=text, fg=color))

    # ── Windows dark title bar ────────────────────────────────────────────────

    def _apply_dark_titlebar(self) -> None:
        try:
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            hwnd = ctypes.windll.user32.GetParent(self._root.winfo_id())
            if not hwnd:
                hwnd = self._root.winfo_id()
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int),
            )
        except Exception:
            pass

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self) -> None:
        header = tk.Frame(self._root, bg=C["bg"], pady=20)
        header.pack(fill="x")

        from logo_cache import get_logo_photo
        self._logo_photo = get_logo_photo(self._root, C["bg"], max_w=180, max_h=60)

        if self._logo_photo:
            tk.Label(header, image=self._logo_photo, bg=C["bg"]).pack()
        else:
            tk.Label(
                header, text="FTC Whisper",
                fg=C["accent"], bg=C["bg"],
                font=("Segoe UI", 22, "bold"),
            ).pack()

        # Hairline divider
        tk.Frame(self._root, bg=C["divider"], height=1).pack(fill="x")

    # ── Dashboard shell ───────────────────────────────────────────────────────

    def _build_dashboard(self, parent: tk.Frame) -> None:
        # Tab bar with underline indicator
        tab_bar = tk.Frame(parent, bg=C["bg"])
        tab_bar.pack(fill="x", padx=20, pady=(14, 0))

        self._dash_tabs = {}
        self._tab_indicators = {}

        for name, label in [("home", "Home"), ("hotkey", "Hotkey"), ("history", "History")]:
            col = tk.Frame(tab_bar, bg=C["bg"])
            col.pack(side="left", expand=True, fill="x")

            btn = tk.Label(
                col, text=label,
                fg=C["subtext"], bg=C["bg"],
                font=("Segoe UI", 10), pady=8, cursor="hand2",
            )
            btn.pack(fill="x")
            btn.bind("<Button-1>", lambda _e, n=name: self._switch_dash_tab(n))
            btn.bind("<Enter>",    lambda _e, b=btn: b.configure(fg=C["text"]) if b.cget("fg") != C["accent"] else None)
            btn.bind("<Leave>",    lambda _e, b=btn, n=name: b.configure(fg=C["accent"] if self._dash_tabs.get(n) and b.cget("fg") == C["text"] else b.cget("fg")))

            ind = tk.Frame(col, bg=C["bg"], height=2)
            ind.pack(fill="x")

            self._dash_tabs[name] = btn
            self._tab_indicators[name] = ind

        tk.Frame(parent, bg=C["divider"], height=1).pack(fill="x", padx=0)

        # Content area
        self._dash_content = tk.Frame(parent, bg=C["bg"])
        self._dash_content.pack(fill="both", expand=True, pady=(10, 0))

        self._home_frame    = tk.Frame(self._dash_content, bg=C["bg"])
        self._hotkey_frame  = tk.Frame(self._dash_content, bg=C["bg"])
        self._history_frame = tk.Frame(self._dash_content, bg=C["bg"])

        self._build_home_tab(self._home_frame)
        self._build_hotkey_tab(self._hotkey_frame)
        self._build_history_tab(self._history_frame)

        # Footer
        footer = tk.Frame(parent, bg=C["bg"], padx=24, pady=10)
        footer.pack(fill="x", side="bottom")

        self._email_display = tk.Label(
            footer, text=self._auth.user_email or "",
            fg=C["subtext"], bg=C["bg"],
            font=("Segoe UI", 9), anchor="w",
        )
        self._email_display.pack(side="left", fill="x", expand=True)

        self._ghost_btn(footer, "Quit",     self._do_quit).pack(side="right", padx=(8, 0))
        self._ghost_btn(footer, "Sign Out", self._do_sign_out).pack(side="right")

        tk.Frame(parent, bg=C["divider"], height=1).pack(fill="x", before=footer)

        self._switch_dash_tab("home")

    def _switch_dash_tab(self, name: str) -> None:
        for n, frame in [("home",    self._home_frame),
                         ("hotkey",  self._hotkey_frame),
                         ("history", self._history_frame)]:
            active = (n == name)
            if active:
                frame.pack(fill="both", expand=True)
                self._dash_tabs[n].configure(fg=C["accent"])
                self._tab_indicators[n].configure(bg=C["accent"])
            else:
                frame.pack_forget()
                self._dash_tabs[n].configure(fg=C["subtext"])
                self._tab_indicators[n].configure(bg=C["bg"])

        if name == "history":
            self._load_history()

    def _show_dashboard(self) -> None:
        self._dash_frame.pack(fill="both", expand=True)
        self._resize(WINDOW_W, DASH_H)
        if hasattr(self, "_email_display"):
            self._email_display.configure(text=self._auth.user_email or "")

    # ── Home tab ──────────────────────────────────────────────────────────────

    def _build_home_tab(self, parent: tk.Frame) -> None:
        # Status card
        sc = self._card(parent, margin=(0, 8))
        self._status_lbl = tk.Label(
            sc, text="● Ready",
            fg=C["success"], bg=C["surface"],
            font=("Segoe UI", 17, "bold"), anchor="w",
        )
        self._status_lbl.pack(fill="x")

        tk.Frame(sc, bg=C["border"], height=1).pack(fill="x", pady=(10, 10))

        hint_row = tk.Frame(sc, bg=C["surface"])
        hint_row.pack(fill="x")

        # Hotkey pill
        pill_bg = tk.Frame(hint_row, bg=C["accent_dim"], padx=8, pady=3)
        pill_bg.pack(side="left")
        hint_text = self._hotkey if self._hotkey else "—"
        self._home_hotkey_lbl = tk.Label(
            pill_bg, text=hint_text,
            fg=C["accent"], bg=C["accent_dim"],
            font=("Segoe UI", 10, "bold"),
        )
        self._home_hotkey_lbl.pack()

        tk.Label(
            hint_row, text=" hold to dictate",
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 10),
        ).pack(side="left")

        # Instructions card
        ic = self._card(parent, margin=(0, 0))
        tk.Label(
            ic, text="Hold the hotkey and speak.\nRelease to transcribe into your cursor.",
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 10), justify="left", anchor="w",
        ).pack(fill="x")

    # ── Hotkey tab ────────────────────────────────────────────────────────────

    def _build_hotkey_tab(self, parent: tk.Frame) -> None:
        # Current shortcut card
        cur = self._card(parent, margin=(0, 8))

        tk.Label(cur, text="Current shortcut",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 9), anchor="w").pack(fill="x")

        self._hotkey_display_var = tk.StringVar(value=self._hotkey or "—")
        self._hotkey_display_lbl = tk.Label(
            cur, textvariable=self._hotkey_display_var,
            fg=C["accent"], bg=C["surface"],
            font=("Segoe UI", 22, "bold"), anchor="w",
        )
        self._hotkey_display_lbl.pack(fill="x", pady=(4, 0))

        # Recorder card
        rec = self._card(parent, margin=(0, 0))

        self._hotkey_record_msg = tk.Label(
            rec,
            text="Click  Change Shortcut  then press any key\nor combination (e.g. F9, Alt+V).",
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 9), justify="left", anchor="w",
        )
        self._hotkey_record_msg.pack(fill="x", pady=(0, 12))

        btn_row = tk.Frame(rec, bg=C["surface"])
        btn_row.pack(fill="x")

        self._record_btn = self._surface_btn(
            btn_row, "Change Shortcut", self._toggle_hotkey_recording)
        self._record_btn.pack(side="left", padx=(0, 8))

        self._save_btn = tk.Label(
            btn_row, text="Save",
            fg=C["subtext"], bg=C["border"],
            font=("Segoe UI", 10, "bold"), padx=14, pady=8,
        )
        self._save_btn.pack(side="left")

    def _toggle_hotkey_recording(self) -> None:
        if self._recording_hotkey:
            self._stop_hotkey_recording(cancelled=True)
        else:
            self._start_hotkey_recording()

    def _start_hotkey_recording(self) -> None:
        self._recording_hotkey = True
        self._pending_hotkey = None
        self._record_btn.configure(text="Cancel", bg=C["error"], fg=C["text"])
        self._hotkey_record_msg.configure(
            text="Press your new key or combination…\n(Escape to cancel)",
            fg=C["accent"],
        )
        self._hotkey_display_var.set("…")
        self._root.focus_force()
        self._root.bind("<KeyPress>",   self._on_hk_keypress)
        self._root.bind("<KeyRelease>", self._on_hk_keyrelease)

    _TK_CTRL  = 0x0004
    _TK_ALT   = 0x20000
    _TK_SHIFT = 0x0001

    def _on_hk_keypress(self, event) -> str:
        keysym = event.keysym.lower()
        if keysym == "escape":
            self._stop_hotkey_recording(cancelled=True)
            return "break"
        if keysym in ("control_l", "control_r", "alt_l", "alt_r",
                      "shift_l", "shift_r", "super_l", "super_r", "meta_l", "meta_r"):
            return "break"
        mods = []
        if event.state & self._TK_CTRL:  mods.append("ctrl")
        if event.state & self._TK_ALT:   mods.append("alt")
        if event.state & self._TK_SHIFT: mods.append("shift")
        base = self._norm_keysym(keysym)
        combo = "+".join(mods + [base]) if mods else base
        self._pending_hotkey = combo
        self._hotkey_display_var.set(combo.upper())
        self._root.after(300, lambda: self._stop_hotkey_recording(cancelled=False))
        return "break"

    def _on_hk_keyrelease(self, event) -> None:
        pass

    def _stop_hotkey_recording(self, cancelled: bool) -> None:
        self._recording_hotkey = False
        self._root.unbind("<KeyPress>")
        self._root.unbind("<KeyRelease>")
        self._record_btn.configure(text="Change Shortcut",
                                   bg=C["surface"], fg=C["text"], cursor="hand2")

        if cancelled or not self._pending_hotkey:
            self._hotkey_display_var.set(self._hotkey or "—")
            self._hotkey_record_msg.configure(
                text="Click  Change Shortcut  then press any key\nor combination (e.g. F9, Alt+V).",
                fg=C["subtext"],
            )
            self._save_btn.configure(bg=C["border"], cursor="", fg=C["subtext"])
        else:
            self._hotkey_display_var.set(self._pending_hotkey.upper())
            self._hotkey_record_msg.configure(
                text=f"New shortcut: {self._pending_hotkey.upper()}\nClick Save to apply.",
                fg=C["success"],
            )
            self._save_btn.configure(bg=C["accent"], cursor="hand2", fg=C["bg"])
            self._save_btn.bind("<Button-1>", lambda _e: self._save_hotkey())
            self._save_btn.bind("<Enter>",    lambda _e: self._save_btn.configure(bg=C["accent_hover"]))
            self._save_btn.bind("<Leave>",    lambda _e: self._save_btn.configure(bg=C["accent"]))

    def _save_hotkey(self) -> None:
        if not self._pending_hotkey:
            return
        new_hotkey = self._pending_hotkey
        self._hotkey = new_hotkey.upper()
        self._hotkey_display_var.set(self._hotkey or "—")
        if hasattr(self, "_home_hotkey_lbl"):
            self._home_hotkey_lbl.configure(text=self._hotkey or "—")
        self._pending_hotkey = None
        self._save_btn.configure(bg=C["border"], cursor="", fg=C["subtext"])
        self._save_btn.unbind("<Button-1>")
        self._hotkey_record_msg.configure(
            text=f"Shortcut updated to {self._hotkey}.",
            fg=C["success"],
        )
        threading.Thread(
            target=self._on_hotkey_change, args=(new_hotkey,), daemon=True
        ).start()

    @staticmethod
    def _norm_keysym(keysym: str) -> str:
        _MAP = {
            "return": "enter", "prior": "pageup", "next": "pagedown",
            "caps_lock": "caps lock", "escape": "esc",
        }
        return _MAP.get(keysym.lower(), keysym.lower())

    # ── History tab ───────────────────────────────────────────────────────────

    def _build_history_tab(self, parent: tk.Frame) -> None:
        top = tk.Frame(parent, bg=C["bg"])
        top.pack(fill="x", padx=20, pady=(0, 8))

        tk.Label(top, text="Recent transcriptions",
                 fg=C["subtext"], bg=C["bg"],
                 font=("Segoe UI", 9)).pack(side="left")

        self._ghost_btn(top, "↻ Refresh", self._load_history).pack(side="right")
        self._ghost_btn(top, "✕ Clear",   self._confirm_clear_history).pack(side="right", padx=(0, 8))

        # Scrollable area inside a rounded card
        wrap_canvas = tk.Canvas(parent, bg=C["bg"], highlightthickness=0, bd=0)
        wrap_canvas.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        def _draw_wrap(_event=None):
            wrap_canvas.update_idletasks()
            cw = wrap_canvas.winfo_width()
            ch = wrap_canvas.winfo_height()
            if cw < 2 or ch < 2:
                return
            wrap_canvas.delete("bg")
            _rr(wrap_canvas, 0, 0, cw-1, ch-1, 10,
                fill=C["surface"], outline=C["border"], tags="bg")
            wrap_canvas.tag_lower("bg")

        wrap_canvas.bind("<Configure>", _draw_wrap)

        inner_wrap = tk.Frame(wrap_canvas, bg=C["surface"])
        wrap_canvas.create_window(1, 1, window=inner_wrap, anchor="nw")
        inner_wrap.bind("<Configure>", lambda e: wrap_canvas.configure(
            scrollregion=wrap_canvas.bbox("all")))

        sb = tk.Scrollbar(inner_wrap, bg=C["surface"], troughcolor=C["bg"],
                          activebackground=C["scrollbar"])
        self._history_text = tk.Text(
            inner_wrap,
            bg=C["surface"], fg=C["text"],
            font=("Segoe UI", 10), wrap=tk.WORD,
            relief="flat", bd=8,
            state=tk.DISABLED, cursor="arrow",
            yscrollcommand=sb.set,
            selectbackground=C["surface_hover"],
            inactiveselectbackground=C["surface_hover"],
        )
        sb.config(command=self._history_text.yview)
        sb.pack(side="right", fill="y")
        self._history_text.pack(side="left", fill="both", expand=True)

        self._history_text.tag_configure("ts",   foreground=C["subtext"], font=("Segoe UI", 8))
        self._history_text.tag_configure("body", foreground=C["text"],    font=("Segoe UI", 10))
        self._history_text.tag_configure("sep",  foreground=C["border"],  font=("Segoe UI", 6))
        self._history_text.tag_configure("dim",  foreground=C["subtext"], font=("Segoe UI", 10, "italic"))

        # Make the inner_wrap fill the canvas
        def _resize_inner(_event=None):
            wrap_canvas.update_idletasks()
            cw = wrap_canvas.winfo_width()
            if cw > 2:
                wrap_canvas.itemconfigure(wrap_canvas.find_withtag("all")[0] if wrap_canvas.find_withtag("all") else 1,
                                          width=cw - 2)
        wrap_canvas.bind("<Configure>", lambda e: (_draw_wrap(e), _resize_inner(e)))

    def _load_history(self) -> None:
        self._history_write("Loading…\n", "dim")

        def _fetch():
            items = self._db.fetch_history() if self._db else []
            self._root.after(0, self._populate_history, items)

        threading.Thread(target=_fetch, daemon=True).start()

    def _populate_history(self, items: list) -> None:
        t = self._history_text
        t.configure(state=tk.NORMAL)
        t.delete("1.0", tk.END)

        if not items:
            t.insert(tk.END, "No transcriptions yet.\n", "dim")
        else:
            for item in items:
                raw_ts = item.get("created_at", "")
                try:
                    dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                    ts = dt.astimezone().strftime("%d %b %Y  %H:%M")
                except Exception:
                    ts = raw_ts[:16]

                text = item.get("refined_text") or item.get("transcribed_text", "")
                t.insert(tk.END, f"{ts}\n", "ts")
                t.insert(tk.END, f"{text}\n", "body")
                t.insert(tk.END, "─" * 44 + "\n", "sep")

        t.configure(state=tk.DISABLED)

    def _confirm_clear_history(self) -> None:
        self._history_write("Delete all history? ", "dim")
        t = self._history_text
        t.configure(state=tk.NORMAL)

        def _do():
            self._history_write("Clearing…\n", "dim")
            threading.Thread(target=self._clear_history, daemon=True).start()

        yes = tk.Label(t, text=" Yes, delete all ", fg=C["bg"], bg=C["error"],
                       font=("Segoe UI", 9, "bold"), cursor="hand2")
        yes.bind("<Button-1>", lambda _e: _do())
        no = tk.Label(t, text=" Cancel ", fg=C["subtext"], bg=C["surface"],
                      font=("Segoe UI", 9), cursor="hand2")
        no.bind("<Button-1>", lambda _e: self._load_history())
        t.window_create(tk.END, window=yes)
        t.insert(tk.END, "  ")
        t.window_create(tk.END, window=no)
        t.configure(state=tk.DISABLED)

    def _clear_history(self) -> None:
        if self._db:
            self._db.clear_history()
        self._root.after(0, self._load_history)

    def _history_write(self, text: str, tag: str = "dim") -> None:
        t = self._history_text
        t.configure(state=tk.NORMAL)
        t.delete("1.0", tk.END)
        t.insert(tk.END, text, tag)
        t.configure(state=tk.DISABLED)

    # ── Auth callbacks ────────────────────────────────────────────────────────

    def _fire_authenticated(self) -> None:
        threading.Thread(
            target=self._on_authenticated, args=(self._auth,), daemon=True
        ).start()

    def _do_sign_out(self) -> None:
        self._on_sign_out()

    def _do_quit(self) -> None:
        self._on_quit()
        if self._root:
            self._root.destroy()

    def _hide(self) -> None:
        self._root.withdraw()

    def _resize(self, w: int, h: int) -> None:
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        x  = (sw - w) // 2
        y  = (sh - h) // 2
        self._root.geometry(f"{w}x{h}+{x}+{y}")

    # ── Rounded card ─────────────────────────────────────────────────────────

    def _card(self, parent: tk.Frame, inner_pad=(18, 14),
              radius: int = 10, margin=(0, 8)) -> tk.Frame:
        """Return an inner Frame sitting inside a rounded-corner Canvas card."""
        cv = tk.Canvas(parent, bg=C["bg"], highlightthickness=0, bd=0)
        cv.pack(fill="x", padx=20, pady=margin)
        px, py = inner_pad
        inner = tk.Frame(cv, bg=C["surface"])
        wid = cv.create_window(px, py, window=inner, anchor="nw")

        def sync(_=None):
            cv.update_idletasks()
            cw = cv.winfo_width()
            fh = inner.winfo_reqheight()
            if cw < 2:
                return
            ch = fh + 2 * py
            cv.configure(height=ch)
            cv.coords(wid, px, py)
            cv.itemconfigure(wid, width=max(1, cw - 2 * px))
            cv.delete("bg")
            _rr(cv, 0, 0, cw - 1, ch - 1, radius,
                fill=C["surface"], outline=C["border"], tags="bg")
            cv.tag_lower("bg")

        cv.bind("<Configure>", sync)
        inner.bind("<Configure>", sync)
        return inner

    # ── Widget helpers ────────────────────────────────────────────────────────

    def _surface_btn(self, parent, text, cmd) -> tk.Label:
        btn = tk.Label(
            parent, text=text,
            fg=C["text"], bg=C["surface_hover"],
            font=("Segoe UI", 10), padx=12, pady=8, cursor="hand2",
        )
        btn.bind("<Button-1>", lambda _e: cmd())
        btn.bind("<Enter>",    lambda _e: btn.configure(bg=C["accent"], fg=C["bg"]))
        btn.bind("<Leave>",    lambda _e: btn.configure(bg=C["surface_hover"], fg=C["text"]))
        return btn

    def _ghost_btn(self, parent, text, cmd) -> tk.Label:
        btn = tk.Label(
            parent, text=text,
            fg=C["subtext"], bg=C["bg"],
            font=("Segoe UI", 9), cursor="hand2",
        )
        btn.bind("<Button-1>", lambda _e: cmd())
        btn.bind("<Enter>",    lambda _e: btn.configure(fg=C["text"]))
        btn.bind("<Leave>",    lambda _e: btn.configure(fg=C["subtext"]))
        return btn
