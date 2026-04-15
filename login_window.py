"""
FTC Whisper — Login / Sign-up window.
Shown on first launch and whenever the session has expired.
Blocks the app from starting until the user is authenticated.
"""

import threading
import tkinter as tk
from typing import Callable, Optional

# FTC brand palette
C = {
    "bg": "#0d0d0d",
    "surface": "#1a1a1a",
    "input_bg": "#141414",
    "text": "#ffffff",
    "subtext": "#777777",
    "accent": "#f39200",
    "accent_hover": "#e08200",
    "error": "#ff5555",
    "success": "#4ade80",
    "divider": "#2d2d2d",
}

WINDOW_W = 400
WINDOW_H = 500


class LoginWindow:
    """
    Modal login/register window. Calls on_success(auth_manager) when the
    user successfully authenticates, or on_cancel() if they close the window.
    """

    def __init__(
        self,
        auth_manager,
        on_success: Callable,
        on_cancel: Optional[Callable] = None,
    ):
        self._auth = auth_manager
        self._on_success = on_success
        self._on_cancel = on_cancel
        self._mode = "login"  # "login" | "signup"

    def run(self) -> None:
        """Build and run the window on the current thread (blocking)."""
        self._root = tk.Tk()
        self._root.title("FTC Whisper")
        self._root.configure(bg=C["bg"])
        self._root.resizable(False, False)
        self._root.protocol("WM_DELETE_WINDOW", self._handle_close)

        # Centre on screen
        self._root.update_idletasks()
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        x = (sw - WINDOW_W) // 2
        y = (sh - WINDOW_H) // 2
        self._root.geometry(f"{WINDOW_W}x{WINDOW_H}+{x}+{y}")

        self._build_ui()
        self._root.mainloop()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = self._root

        # ── Logo / header ──────────────────────────────────────────────
        header = tk.Frame(root, bg=C["bg"], pady=28)
        header.pack(fill="x")

        from logo_cache import get_logo_photo

        self._logo_photo = get_logo_photo(self._root, C["bg"], max_w=160, max_h=60)

        if self._logo_photo:
            tk.Label(header, image=self._logo_photo, bg=C["bg"]).pack()
        else:
            tk.Label(
                header,
                text="FTC Whisper",
                fg=C["accent"],
                bg=C["bg"],
                font=("Segoe UI", 22, "bold"),
            ).pack()

        # ── Tab bar ────────────────────────────────────────────────────
        tabs = tk.Frame(root, bg=C["surface"])
        tabs.pack(fill="x", padx=32)

        self._login_tab = self._tab(tabs, "Sign In", lambda: self._switch("login"))
        self._login_tab.pack(side="left", expand=True, fill="x")

        self._signup_tab = self._tab(
            tabs, "Create Account", lambda: self._switch("signup")
        )
        self._signup_tab.pack(side="left", expand=True, fill="x")

        # ── Form card ──────────────────────────────────────────────────
        self._card = tk.Frame(root, bg=C["surface"], padx=32, pady=24)
        self._card.pack(fill="both", expand=True, padx=32, pady=(0, 32))

        self._email_var = tk.StringVar()
        self._password_var = tk.StringVar()
        self._confirm_var = tk.StringVar()

        # Email
        self._field_label(self._card, "Email")
        self._email_entry = self._entry(self._card, self._email_var)
        self._email_entry.pack(fill="x", pady=(4, 12))

        # Password
        self._field_label(self._card, "Password")
        self._pass_entry = self._entry(self._card, self._password_var, show="•")
        self._pass_entry.pack(fill="x", pady=(4, 12))

        # Confirm password (sign-up only)
        self._confirm_label = self._field_label(self._card, "Confirm Password")
        self._confirm_entry = self._entry(self._card, self._confirm_var, show="•")
        self._confirm_entry.pack(fill="x", pady=(4, 12))

        # Status message
        self._status_var = tk.StringVar()
        self._status_lbl = tk.Label(
            self._card,
            textvariable=self._status_var,
            fg=C["error"],
            bg=C["surface"],
            font=("Segoe UI", 10),
            wraplength=300,
        )
        self._status_lbl.pack(pady=(0, 10))

        # Submit button
        self._submit_btn = tk.Label(
            self._card,
            text="Sign In",
            fg=C["bg"],
            bg=C["accent"],
            font=("Segoe UI", 12, "bold"),
            padx=16,
            pady=10,
            cursor="hand2",
        )
        self._submit_btn.pack(fill="x", pady=(4, 0))
        self._submit_btn.bind("<Button-1>", lambda _e: self._submit())
        self._submit_btn.bind(
            "<Enter>", lambda _e: self._submit_btn.configure(bg=C["accent_hover"])
        )
        self._submit_btn.bind(
            "<Leave>", lambda _e: self._submit_btn.configure(bg=C["accent"])
        )

        # Enter key submits
        self._root.bind("<Return>", lambda _e: self._submit())

        self._switch("login")

    def _tab(self, parent, text, command) -> tk.Label:
        lbl = tk.Label(
            parent,
            text=text,
            fg=C["subtext"],
            bg=C["surface"],
            font=("Segoe UI", 10),
            padx=12,
            pady=8,
            cursor="hand2",
        )
        lbl.bind("<Button-1>", lambda _e: command())
        return lbl

    def _field_label(self, parent, text) -> tk.Label:
        lbl = tk.Label(
            parent,
            text=text,
            fg=C["subtext"],
            bg=C["surface"],
            font=("Segoe UI", 10),
            anchor="w",
        )
        lbl.pack(fill="x")
        return lbl

    def _entry(self, parent, var, show="") -> tk.Entry:
        return tk.Entry(
            parent,
            textvariable=var,
            show=show,
            bg=C["input_bg"],
            fg=C["text"],
            insertbackground=C["text"],
            relief="flat",
            font=("Segoe UI", 11),
            bd=0,
        )

    # ------------------------------------------------------------------
    # Mode switching
    # ------------------------------------------------------------------

    def _switch(self, mode: str, clear_status: bool = True) -> None:
        self._mode = mode
        if mode == "login":
            self._login_tab.configure(fg=C["accent"], bg=C["surface"])
            self._signup_tab.configure(fg=C["subtext"], bg=C["surface"])
            self._confirm_label.pack_forget()
            self._confirm_entry.pack_forget()
            self._submit_btn.configure(text="Sign In")
        else:
            self._login_tab.configure(fg=C["subtext"], bg=C["surface"])
            self._signup_tab.configure(fg=C["accent"], bg=C["surface"])
            self._confirm_label.pack(fill="x")
            self._confirm_entry.pack(fill="x", pady=(4, 12))
            self._submit_btn.configure(text="Create Account")
        if clear_status:
            self._status_var.set("")

    # ------------------------------------------------------------------
    # Form submission
    # ------------------------------------------------------------------

    def _submit(self) -> None:
        email = self._email_var.get().strip()
        password = self._password_var.get()

        if not email or not password:
            self._set_status("Please enter your email and password.", error=True)
            return

        if self._mode == "signup":
            if password != self._confirm_var.get():
                self._set_status("Passwords do not match.", error=True)
                return
            if len(password) < 6:
                self._set_status("Password must be at least 6 characters.", error=True)
                return

        self._set_status("Please wait…", error=False)
        self._submit_btn.configure(bg=C["divider"], cursor="")

        def _run():
            if self._mode == "login":
                ok, msg = self._auth.sign_in(email, password)
            else:
                ok, msg = self._auth.sign_up(email, password)

            self._root.after(0, self._handle_result, ok, msg)

        threading.Thread(target=_run, daemon=True).start()

    def _handle_result(self, ok: bool, msg: str) -> None:
        self._submit_btn.configure(bg=C["accent"], cursor="hand2")
        if ok and self._auth.is_authenticated:
            self._set_status(msg, error=False)
            self._root.after(600, self._finish)
            return

        if ok and self._mode == "signup":
            self._set_status(msg, error=False)
            self._password_var.set("")
            self._confirm_var.set("")
            self._switch("login", clear_status=False)
            return

        self._set_status(msg, error=True)

    def _finish(self) -> None:
        self._root.destroy()
        self._on_success(self._auth)

    def _handle_close(self) -> None:
        self._root.destroy()
        if self._on_cancel:
            self._on_cancel()

    def _set_status(self, msg: str, error: bool = True) -> None:
        self._status_var.set(msg)
        self._status_lbl.configure(fg=C["error"] if error else C["success"])
