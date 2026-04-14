"""
FTC Whisper — Main application window.

Login panel  : shown before authentication.
Dashboard    : shown once authenticated — three tabs:
                 Home    status indicator + user info
                 Hotkey  click-to-record a new hotkey shortcut
                 History scrollable transcription log from Supabase
"""

import threading
import tkinter as tk
from datetime import datetime
from typing import Callable, Optional

C = {
    "bg":            "#4e4e4c",
    "surface":       "#3a3a38",
    "surface_hover": "#444442",
    "input_bg":      "#2e2e2c",
    "text":          "#ffffff",
    "subtext":       "#dadada",
    "accent":        "#f39200",
    "accent_hover":  "#d98200",
    "error":         "#ff6b6b",
    "success":       "#a6e3a1",
    "divider":       "#6e6e6c",
    "scrollbar":     "#5a5a58",
}

WINDOW_W = 420
LOGIN_H  = 520
DASH_H   = 520


class AppWindow:
    _STATUS = {
        "idle":       ("● Ready",         "#a6e3a1"),
        "recording":  ("● Recording…",    "#ff6b6b"),
        "processing": ("● Transcribing…", "#f39200"),
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
        self._login_mode = "login"

        # Hotkey recorder state
        self._recording_hotkey = False
        self._pending_hotkey: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._root = tk.Tk()
        self._root.title("FTC Whisper")
        self._root.configure(bg=C["bg"])
        self._root.resizable(False, False)
        self._root.protocol("WM_DELETE_WINDOW", self._hide)
        print("[DEBUG] AppWindow UI setup finished")

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

    # ------------------------------------------------------------------
    # Shared header
    # ------------------------------------------------------------------

    def _build_header(self) -> None:
        header = tk.Frame(self._root, bg=C["bg"], pady=18)
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

        tk.Frame(self._root, bg=C["divider"], height=1).pack(fill="x", padx=24)

    # ------------------------------------------------------------------
    # Login panel
    # ------------------------------------------------------------------

    def _build_login(self, parent: tk.Frame) -> None:
        tabs = tk.Frame(parent, bg=C["surface"])
        tabs.pack(fill="x", padx=24, pady=(14, 0))

        self._tab_signin = self._make_tab(tabs, "Sign In",
                                          lambda: self._switch_login_mode("login"))
        self._tab_signup = self._make_tab(tabs, "Create Account",
                                          lambda: self._switch_login_mode("signup"))
        self._tab_signin.pack(side="left", expand=True, fill="x")
        self._tab_signup.pack(side="left", expand=True, fill="x")

        card = tk.Frame(parent, bg=C["surface"], padx=22, pady=16)
        card.pack(fill="both", expand=True, padx=24, pady=(0, 22))

        self._email_var = tk.StringVar()
        self._pass_var  = tk.StringVar()

        self._flabel(card, "Email")
        self._email_entry = self._entry(card, self._email_var)
        self._email_entry.pack(fill="x", pady=(4, 12))

        self._flabel(card, "Password")
        self._pass_entry = self._entry(card, self._pass_var, show="•")
        self._pass_entry.pack(fill="x", pady=(4, 16))

        self._login_status_var = tk.StringVar(value="")
        self._login_status_lbl = tk.Label(
            card, textvariable=self._login_status_var,
            bg=C["surface"], fg=C["error"],
            font=("Segoe UI", 10), wraplength=340,
        )
        self._login_status_lbl.pack(pady=(0, 8))

        self._submit_btn = tk.Label(
            card, text="Sign In",
            fg=C["bg"], bg=C["accent"],
            font=("Segoe UI", 12, "bold"),
            padx=16, pady=10,
            cursor="hand2",
        )
        self._submit_btn.bind("<Button-1>", lambda _e: self._submit())
        self._submit_btn.bind("<Enter>",    lambda _e: self._submit_btn.configure(bg=C["accent_hover"]))
        self._submit_btn.bind("<Leave>",    lambda _e: self._submit_btn.configure(bg=C["accent"]))
        self._submit_btn.pack(fill="x")
        self._submit_busy = False

        self._root.bind("<Return>", lambda _e: self._on_return())
        self._switch_login_mode("login")

    def _show_login(self) -> None:
        self._dash_frame.pack_forget()
        self._login_frame.pack(fill="both", expand=True)
        self._resize(WINDOW_W, LOGIN_H)

    def _switch_login_mode(self, mode: str) -> None:
        self._login_mode = mode
        if mode == "login":
            self._tab_signin.configure(fg=C["accent"])
            self._tab_signup.configure(fg=C["subtext"])
            self._submit_btn.configure(text="Sign In")
        else:
            self._tab_signin.configure(fg=C["subtext"])
            self._tab_signup.configure(fg=C["accent"])
            self._submit_btn.configure(text="Create Account")
        self._login_status_var.set("")

    def _on_return(self) -> None:
        if self._login_frame.winfo_ismapped():
            self._submit()

    def _submit(self) -> None:
        print("[DEBUG] _submit called", flush=True)
        if getattr(self, "_submit_busy", False):
            return
        email    = self._email_var.get().strip()
        password = self._pass_var.get()

        if not email or not password:
            self._set_login_status("Please enter your email and password.", error=True)
            return
        if len(password) < 6:
            self._set_login_status("Password must be at least 6 characters.", error=True)
            return

        self._submit_busy = True
        self._submit_btn.configure(bg=C["divider"], cursor="")
        self._set_login_status("Connecting…", error=False)

        def _run():
            if self._login_mode == "login":
                ok, msg = self._auth.sign_in(email, password)
            else:
                ok, msg = self._auth.sign_up(email, password)
            self._root.after(0, self._handle_auth_result, ok, msg)

        threading.Thread(target=_run, daemon=True).start()

    def _handle_auth_result(self, ok: bool, msg: str) -> None:
        self._submit_busy = False
        self._submit_btn.configure(bg=C["accent"], cursor="hand2")
        if ok:
            # Both auto-confirmed signup and sign-in land here
            self._set_login_status("Welcome!", error=False)
            self._root.after(400, self._on_login_success)
        else:
            self._set_login_status(msg, error=True)

    def _fire_authenticated(self) -> None:
        """Called via after() when a saved session is restored at startup."""
        threading.Thread(
            target=self._on_authenticated, args=(self._auth,), daemon=True
        ).start()

    def _on_login_success(self) -> None:
        self._show_dashboard()
        threading.Thread(
            target=self._on_authenticated, args=(self._auth,), daemon=True
        ).start()

    def _set_login_status(self, msg: str, error: bool = True) -> None:
        self._login_status_var.set(msg)
        self._login_status_lbl.configure(fg=C["error"] if error else C["success"])

    # ------------------------------------------------------------------
    # Dashboard shell (tab bar + content switcher + footer)
    # ------------------------------------------------------------------

    def _build_dashboard(self, parent: tk.Frame) -> None:
        # ── Tab bar ───────────────────────────────────────────────────
        tab_bar = tk.Frame(parent, bg=C["surface"])
        tab_bar.pack(fill="x", padx=24, pady=(12, 0))

        self._dash_tabs = {}
        for name, label in [("home", "Home"), ("hotkey", "Hotkey"), ("history", "History")]:
            t = self._make_tab(tab_bar, label, lambda n=name: self._switch_dash_tab(n))
            t.pack(side="left", expand=True, fill="x")
            self._dash_tabs[name] = t

        # ── Content frames ────────────────────────────────────────────
        self._dash_content = tk.Frame(parent, bg=C["bg"])
        self._dash_content.pack(fill="both", expand=True, padx=24, pady=(0, 0))

        self._home_frame    = tk.Frame(self._dash_content, bg=C["bg"])
        self._hotkey_frame  = tk.Frame(self._dash_content, bg=C["bg"])
        self._history_frame = tk.Frame(self._dash_content, bg=C["bg"])

        self._build_home_tab(self._home_frame)
        self._build_hotkey_tab(self._hotkey_frame)
        self._build_history_tab(self._history_frame)

        # ── Footer ────────────────────────────────────────────────────
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

        tk.Frame(parent, bg=C["divider"], height=1).pack(fill="x", padx=24, before=footer)

        self._switch_dash_tab("home")

    def _switch_dash_tab(self, name: str) -> None:
        for n, frame in [("home",    self._home_frame),
                         ("hotkey",  self._hotkey_frame),
                         ("history", self._history_frame)]:
            if n == name:
                frame.pack(fill="both", expand=True, pady=(8, 0))
                self._dash_tabs[n].configure(fg=C["accent"])
            else:
                frame.pack_forget()
                self._dash_tabs[n].configure(fg=C["subtext"])

        if name == "history":
            self._load_history()

    def _show_dashboard(self) -> None:
        self._dash_frame.pack(fill="both", expand=True)
        self._resize(WINDOW_W, DASH_H)
        if hasattr(self, "_email_display"):
            self._email_display.configure(text=self._auth.user_email or "")

    # ------------------------------------------------------------------
    # Home tab
    # ------------------------------------------------------------------

    def _build_home_tab(self, parent: tk.Frame) -> None:
        # Status card
        sc = tk.Frame(parent, bg=C["surface"], padx=16, pady=14)
        sc.pack(fill="x", pady=(0, 10))

        self._status_lbl = tk.Label(
            sc, text="● Ready",
            fg=C["success"], bg=C["surface"],
            font=("Segoe UI", 13, "bold"), anchor="w",
        )
        self._status_lbl.pack(fill="x")

        self._hotkey_hint = tk.Label(
            sc, text=f"Hold  {self._hotkey}  to dictate",
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 10), anchor="w",
        )
        self._hotkey_hint.pack(fill="x", pady=(4, 0))

        # Info card
        ic = tk.Frame(parent, bg=C["surface"], padx=16, pady=12)
        ic.pack(fill="x")

        tk.Label(
            ic, text="Hold your hotkey to start recording. Release to transcribe.",
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 9), wraplength=340, justify="left", anchor="w",
        ).pack(fill="x")

    # ------------------------------------------------------------------
    # Hotkey tab
    # ------------------------------------------------------------------

    def _build_hotkey_tab(self, parent: tk.Frame) -> None:
        # Current hotkey display
        cur = tk.Frame(parent, bg=C["surface"], padx=16, pady=14)
        cur.pack(fill="x", pady=(0, 10))

        tk.Label(cur, text="Current shortcut",
                 fg=C["subtext"], bg=C["surface"],
                 font=("Segoe UI", 9), anchor="w").pack(fill="x")

        self._hotkey_display_var = tk.StringVar(value=self._hotkey)
        tk.Label(cur, textvariable=self._hotkey_display_var,
                 fg=C["accent"], bg=C["surface"],
                 font=("Segoe UI", 16, "bold"), anchor="w").pack(fill="x", pady=(4, 0))

        # Recorder card
        rec = tk.Frame(parent, bg=C["surface"], padx=16, pady=14)
        rec.pack(fill="x")

        self._hotkey_record_msg = tk.Label(
            rec,
            text="Click  Change Shortcut  then press any key or combination (e.g. F9, Alt+V).",
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 9), wraplength=340, justify="left", anchor="w",
        )
        self._hotkey_record_msg.pack(fill="x", pady=(0, 12))

        btn_row = tk.Frame(rec, bg=C["surface"])
        btn_row.pack(fill="x")

        self._record_btn = self._surface_btn(
            btn_row, "Change Shortcut", self._toggle_hotkey_recording)
        self._record_btn.pack(side="left", padx=(0, 8))

        self._save_btn = tk.Label(
            btn_row, text="Save",
            fg=C["bg"], bg=C["divider"],
            font=("Segoe UI", 10, "bold"), padx=14, pady=8,
        )
        self._save_btn.pack(side="left")
        # Save is disabled until a valid combo is recorded

    def _toggle_hotkey_recording(self) -> None:
        if self._recording_hotkey:
            self._stop_hotkey_recording(cancelled=True)
        else:
            self._start_hotkey_recording()

    def _start_hotkey_recording(self) -> None:
        self._recording_hotkey = True
        self._pending_hotkey = None
        self._record_btn.configure(text="Cancel", bg=C["error"])
        self._hotkey_record_msg.configure(
            text="Press your new key or combination now… (Escape to cancel)",
            fg=C["accent"],
        )
        self._hotkey_display_var.set("…")
        self._root.focus_force()
        self._root.bind("<KeyPress>",   self._on_hk_keypress)
        self._root.bind("<KeyRelease>", self._on_hk_keyrelease)

    # tkinter state bitmask for modifiers (Windows)
    _TK_CTRL  = 0x0004
    _TK_ALT   = 0x20000
    _TK_SHIFT = 0x0001

    def _on_hk_keypress(self, event) -> str:
        keysym = event.keysym.lower()

        # Escape cancels
        if keysym == "escape":
            self._stop_hotkey_recording(cancelled=True)
            return "break"

        # Ignore pure modifier keypresses — wait for a base key
        if keysym in ("control_l", "control_r", "alt_l", "alt_r",
                      "shift_l", "shift_r", "super_l", "super_r", "meta_l", "meta_r"):
            return "break"

        # Build modifier prefix from event.state (reliable on Windows)
        mods = []
        if event.state & self._TK_CTRL:  mods.append("ctrl")
        if event.state & self._TK_ALT:   mods.append("alt")
        if event.state & self._TK_SHIFT: mods.append("shift")

        base = self._norm_keysym(keysym)
        combo = "+".join(mods + [base]) if mods else base

        self._pending_hotkey = combo
        self._hotkey_display_var.set(combo.upper())
        # Small delay so the user can see the key before recording stops
        self._root.after(300, lambda: self._stop_hotkey_recording(cancelled=False))
        return "break"

    def _on_hk_keyrelease(self, event) -> None:
        pass  # everything handled in keypress

    def _stop_hotkey_recording(self, cancelled: bool) -> None:
        self._recording_hotkey = False
        self._root.unbind("<KeyPress>")
        self._root.unbind("<KeyRelease>")
        self._record_btn.configure(text="Change Shortcut", bg=C["surface"], cursor="hand2")

        if cancelled or not self._pending_hotkey:
            self._hotkey_display_var.set(self._hotkey)
            self._hotkey_record_msg.configure(
                text="Click  Change Shortcut  then press any key or combination (e.g. F9, Alt+V).",
                fg=C["subtext"],
            )
            self._save_btn.configure(bg=C["divider"], cursor="", fg=C["bg"])
        else:
            self._hotkey_display_var.set(self._pending_hotkey.upper())
            self._hotkey_record_msg.configure(
                text=f"New shortcut: {self._pending_hotkey.upper()} — click Save to apply.",
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
        self._hotkey_display_var.set(self._hotkey)
        if hasattr(self, "_hotkey_hint"):
            self._hotkey_hint.configure(text=f"Hold  {self._hotkey}  to dictate")
        self._pending_hotkey = None
        self._save_btn.configure(bg=C["divider"], cursor="", fg=C["bg"])
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
        """Normalise a tkinter keysym to a hotkey_manager-compatible key name."""
        _MAP = {
            "return": "enter", "prior": "pageup", "next": "pagedown",
            "caps_lock": "caps lock", "escape": "esc",
        }
        k = keysym.lower()
        return _MAP.get(k, k)

    # ------------------------------------------------------------------
    # History tab
    # ------------------------------------------------------------------

    def _build_history_tab(self, parent: tk.Frame) -> None:
        top = tk.Frame(parent, bg=C["bg"])
        top.pack(fill="x", pady=(0, 6))

        tk.Label(top, text="Recent transcriptions",
                 fg=C["subtext"], bg=C["bg"],
                 font=("Segoe UI", 9)).pack(side="left")

        self._ghost_btn(top, "↻ Refresh", self._load_history).pack(side="right")
        self._ghost_btn(top, "✕ Clear All", self._confirm_clear_history).pack(side="right", padx=(0, 8))

        # Scrollable text area
        wrap = tk.Frame(parent, bg=C["surface"])
        wrap.pack(fill="both", expand=True)

        sb = tk.Scrollbar(wrap, bg=C["surface"], troughcolor=C["bg"],
                          activebackground=C["scrollbar"])
        self._history_text = tk.Text(
            wrap,
            bg=C["surface"], fg=C["text"],
            font=("Segoe UI", 10), wrap=tk.WORD,
            relief="flat", bd=6,
            state=tk.DISABLED, cursor="arrow",
            yscrollcommand=sb.set,
            selectbackground=C["surface_hover"],
            inactiveselectbackground=C["surface_hover"],
        )
        sb.config(command=self._history_text.yview)
        sb.pack(side="right", fill="y")
        self._history_text.pack(side="left", fill="both", expand=True)

        self._history_text.tag_configure(
            "ts",   foreground=C["subtext"], font=("Segoe UI", 8))
        self._history_text.tag_configure(
            "body", foreground=C["text"],    font=("Segoe UI", 10))
        self._history_text.tag_configure(
            "sep",  foreground=C["divider"], font=("Segoe UI", 6))
        self._history_text.tag_configure(
            "dim",  foreground=C["subtext"], font=("Segoe UI", 10, "italic"))

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
                # Timestamp
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
        """Show an inline confirmation before wiping history."""
        self._history_write("Delete all history? ", "dim")
        t = self._history_text
        t.configure(state=tk.NORMAL)

        def _do():
            self._history_write("Clearing…\n", "dim")
            threading.Thread(target=self._clear_history, daemon=True).start()

        def _cancel():
            self._load_history()

        # Inline Yes / No labels inside the text widget
        yes = tk.Label(t, text=" Yes, delete all ", fg=C["bg"], bg=C["error"],
                       font=("Segoe UI", 9, "bold"), cursor="hand2")
        yes.bind("<Button-1>", lambda _e: _do())
        no = tk.Label(t, text=" Cancel ", fg=C["subtext"], bg=C["surface"],
                      font=("Segoe UI", 9), cursor="hand2")
        no.bind("<Button-1>", lambda _e: _cancel())
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

    # ------------------------------------------------------------------
    # Footer / common actions
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Widget helpers
    # ------------------------------------------------------------------

    def _make_tab(self, parent, text, cmd) -> tk.Label:
        lbl = tk.Label(
            parent, text=text,
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 10), padx=10, pady=8, cursor="hand2",
        )
        lbl.bind("<Button-1>", lambda _e: cmd())
        return lbl

    def _flabel(self, parent, text) -> tk.Label:
        lbl = tk.Label(
            parent, text=text,
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 10), anchor="w",
        )
        lbl.pack(fill="x")
        return lbl

    def _entry(self, parent, var, show="") -> tk.Entry:
        return tk.Entry(
            parent, textvariable=var, show=show,
            bg=C["input_bg"], fg=C["text"],
            insertbackground=C["text"],
            relief="flat", font=("Segoe UI", 11), bd=0,
        )

    def _accent_btn(self, parent, text, cmd) -> tk.Button:
        btn = tk.Button(
            parent, text=text,
            fg=C["bg"], bg=C["accent"],
            font=("Segoe UI", 12, "bold"),
            padx=16, pady=2, cursor="hand2",
            relief="flat", activebackground=C["accent"],
            activeforeground=C["bg"],
            command=cmd,
            highlightthickness=0
        )
        return btn

    def _surface_btn(self, parent, text, cmd) -> tk.Label:
        btn = tk.Label(
            parent, text=text,
            fg=C["text"], bg=C["surface"],
            font=("Segoe UI", 10), padx=12, pady=8, cursor="hand2",
        )
        btn.bind("<Button-1>", lambda _e: cmd())
        btn.bind("<Enter>",    lambda _e: btn.configure(bg=C["surface_hover"]))
        btn.bind("<Leave>",    lambda _e: btn.configure(bg=C["surface"]))
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
