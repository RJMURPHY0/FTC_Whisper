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
WINDOW_H = 560


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
        self._pending_confirm_email: Optional[str] = None
        self._embedded = False

    def embed(self, frame: tk.Frame) -> None:
        """Build login UI into an existing frame (in-window, no Toplevel)."""
        self._embedded = True
        self._root = frame.winfo_toplevel()
        self._build_ui(container=frame)

    def reset(self) -> None:
        """Clear form fields and reset to login mode — call before showing again."""
        if hasattr(self, "_email_var"):
            self._email_var.set("")
            self._password_var.set("")
        if hasattr(self, "_confirm_var"):
            self._confirm_var.set("")
        self._pending_confirm_email = None
        if hasattr(self, "_status_var"):
            self._status_var.set("")
            if hasattr(self, "_status_frame"):
                self._status_frame.pack_forget()
        if hasattr(self, "_mode"):
            self._switch("login")

    def run(self, parent=None) -> None:
        """Build and run the window on the current thread (blocking).
        If parent is provided, opens as a modal Toplevel over the parent window."""
        if parent is not None:
            self._root = tk.Toplevel(parent)
            self._root.transient(parent)
            self._root.grab_set()
            parent.update_idletasks()
            if parent.winfo_viewable():
                px, py = parent.winfo_x(), parent.winfo_y()
                pw, ph = parent.winfo_width(), parent.winfo_height()
                x = px + (pw - WINDOW_W) // 2
                y = py + (ph - WINDOW_H) // 2
            else:
                sw = parent.winfo_screenwidth()
                sh = parent.winfo_screenheight()
                x = (sw - WINDOW_W) // 2
                y = (sh - WINDOW_H) // 2
        else:
            self._root = tk.Tk()
            self._root.update_idletasks()
            sw = self._root.winfo_screenwidth()
            sh = self._root.winfo_screenheight()
            x = (sw - WINDOW_W) // 2
            y = (sh - WINDOW_H) // 2

        self._root.title("FTC Whisper")
        self._root.configure(bg=C["bg"])
        self._root.resizable(False, False)
        self._root.protocol("WM_DELETE_WINDOW", self._handle_close)
        self._root.geometry(f"{WINDOW_W}x{WINDOW_H}+{x}+{y}")

        self._build_ui()

        if parent is not None:
            self._root.wait_window()
        else:
            self._root.mainloop()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self, container=None) -> None:
        c = container or self._root

        # ── Logo / header ──────────────────────────────────────────────
        header = tk.Frame(c, bg=C["bg"], pady=28)
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
        tabs = tk.Frame(c, bg=C["surface"])
        tabs.pack(fill="x", padx=32)

        self._login_tab = self._tab(tabs, "Sign In", lambda: self._switch("login"))
        self._login_tab.pack(side="left", expand=True, fill="x")

        self._signup_tab = self._tab(
            tabs, "Create Account", lambda: self._switch("signup")
        )
        self._signup_tab.pack(side="left", expand=True, fill="x")

        # ── Form card ──────────────────────────────────────────────────
        self._card = tk.Frame(c, bg=C["surface"], padx=32, pady=24)
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
        pass_row = tk.Frame(self._card, bg=C["surface"])
        pass_row.pack(fill="x", pady=(4, 12))
        self._pass_entry = self._entry(pass_row, self._password_var, show="•")
        self._pass_entry.pack(side="left", fill="x", expand=True)
        self._pass_visible = False
        self._pass_eye = tk.Label(pass_row, text="👁", bg=C["surface"], fg=C["subtext"],
                                  font=("Segoe UI", 11), cursor="hand2", padx=4)
        self._pass_eye.pack(side="left")
        self._pass_eye.bind("<Button-1>", lambda _e: self._toggle_pass())

        # Confirm password section — kept in a frame so _switch can reliably
        # reposition it above the submit button using before=
        self._confirm_section = tk.Frame(self._card, bg=C["surface"])
        tk.Label(
            self._confirm_section, text="Confirm Password",
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 10), anchor="w",
        ).pack(fill="x")
        confirm_row = tk.Frame(self._confirm_section, bg=C["surface"])
        confirm_row.pack(fill="x", pady=(4, 12))
        self._confirm_entry = self._entry(confirm_row, self._confirm_var, show="•")
        self._confirm_entry.pack(side="left", fill="x", expand=True)
        self._confirm_eye = tk.Label(confirm_row, text="👁", bg=C["surface"], fg=C["subtext"],
                                     font=("Segoe UI", 11), cursor="hand2", padx=4)
        self._confirm_eye.pack(side="left")
        self._confirm_eye.bind("<Button-1>", lambda _e: self._toggle_confirm())

        # Status message — hidden until needed
        self._status_var = tk.StringVar()
        self._status_frame = tk.Frame(self._card, bg=C["surface"])
        self._status_lbl = tk.Label(
            self._status_frame,
            textvariable=self._status_var,
            fg=C["error"],
            bg=C["surface"],
            font=("Segoe UI", 11, "bold"),
            wraplength=300,
            justify="center",
            pady=6,
        )
        self._status_lbl.pack(fill="x")
        # Don't pack _status_frame yet — only shown when there's a message

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

        # Forgot password link (login mode only)
        self._forgot_link = tk.Label(
            self._card, text="Forgot password?",
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 9), cursor="hand2",
        )
        self._forgot_link.bind("<Button-1>", lambda _e: self._forgot_password())
        self._forgot_link.bind("<Enter>", lambda _e: self._forgot_link.configure(fg=C["accent"]))
        self._forgot_link.bind("<Leave>", lambda _e: self._forgot_link.configure(fg=C["subtext"]))

        # Resend confirmation link (login mode, when email awaiting confirmation)
        self._resend_link = tk.Label(
            self._card, text="Resend confirmation email",
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 9), cursor="hand2",
        )
        self._resend_link.bind("<Button-1>", lambda _e: self._resend_confirmation())
        self._resend_link.bind("<Enter>", lambda _e: self._resend_link.configure(fg=C["accent"]))
        self._resend_link.bind("<Leave>", lambda _e: self._resend_link.configure(fg=C["subtext"]))

        # Divider
        self._divider_frame = tk.Frame(self._card, bg=C["surface"])
        tk.Frame(self._divider_frame, bg=C["divider"], height=1).pack(
            side="left", fill="x", expand=True, pady=(0, 0)
        )
        tk.Label(
            self._divider_frame, text="  or  ",
            fg=C["subtext"], bg=C["surface"], font=("Segoe UI", 9),
        ).pack(side="left")
        tk.Frame(self._divider_frame, bg=C["divider"], height=1).pack(
            side="left", fill="x", expand=True,
        )

        # Google sign-in button
        self._google_btn = tk.Label(
            self._card, text="Continue with Google",
            fg=C["text"], bg=C["input_bg"],
            font=("Segoe UI", 11), padx=16, pady=9,
            cursor="hand2",
        )
        self._google_btn.bind("<Button-1>", lambda _e: self._sign_in_google())
        self._google_btn.bind("<Enter>", lambda _e: self._google_btn.configure(bg=C["divider"]))
        self._google_btn.bind("<Leave>", lambda _e: self._google_btn.configure(bg=C["input_bg"]))

        # Divider + Google button always visible — packed once here
        self._divider_frame.pack(fill="x", pady=(12, 0))
        self._google_btn.pack(fill="x", pady=(8, 0))

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
        self._forgot_link.pack_forget()
        self._resend_link.pack_forget()
        if mode == "login":
            self._login_tab.configure(fg=C["accent"], bg=C["surface"])
            self._signup_tab.configure(fg=C["subtext"], bg=C["surface"])
            self._confirm_section.pack_forget()
            self._submit_btn.configure(text="Sign In")
            self._forgot_link.pack(anchor="center", pady=(8, 0))
            if self._pending_confirm_email:
                self._resend_link.pack(anchor="center", pady=(4, 0))
        else:
            self._login_tab.configure(fg=C["subtext"], bg=C["surface"])
            self._signup_tab.configure(fg=C["accent"], bg=C["surface"])
            self._confirm_section.pack(fill="x", before=self._submit_btn)
            self._submit_btn.configure(text="Create Account")
        if clear_status:
            self._status_var.set("")
            self._status_frame.pack_forget()

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
        print(f"[Login] ok={ok} msg={msg!r}")

        if ok and self._auth.is_authenticated:
            self._pending_confirm_email = None
            self._set_status(f"✓  {msg}", error=False)
            self._root.after(800, self._finish)
            return

        if ok and self._mode == "signup":
            email = self._email_var.get().strip()
            self._pending_confirm_email = email
            self._set_status(
                f"✉  Confirmation email sent to {email}\n"
                "Check your inbox, click the link, then sign in here.",
                error=False,
            )
            self._password_var.set("")
            self._confirm_var.set("")
            self._switch("login", clear_status=False)
            return

        if not ok and ("email not confirmed" in msg.lower() or "email_not_confirmed" in msg.lower()):
            self._pending_confirm_email = self._email_var.get().strip()
            self._resend_link.pack_forget()
            self._resend_link.pack(anchor="center", pady=(4, 0))

        self._set_status(f"✕  {msg}", error=True)

    def _toggle_pass(self) -> None:
        self._pass_visible = not self._pass_visible
        self._pass_entry.configure(show="" if self._pass_visible else "•")
        self._pass_eye.configure(fg=C["accent"] if self._pass_visible else C["subtext"])

    def _toggle_confirm(self) -> None:
        show = self._confirm_entry.cget("show") == "•"
        self._confirm_entry.configure(show="" if show else "•")
        self._confirm_eye.configure(fg=C["accent"] if show else C["subtext"])

    def _use_offline(self) -> None:
        self._auth.sign_in_offline()
        self._root.destroy()
        self._on_success(self._auth)

    def _forgot_password(self) -> None:
        email = self._email_var.get().strip()
        if not email:
            self._set_status("Enter your email address above first.", error=True)
            return
        self._set_status("Sending reset email…", error=False)

        def _run():
            ok, msg = self._auth.reset_password(email)
            self._root.after(0, self._set_status, msg, not ok)

        threading.Thread(target=_run, daemon=True).start()

    def _resend_confirmation(self) -> None:
        email = self._pending_confirm_email or self._email_var.get().strip()
        if not email:
            self._set_status("Enter your email address above first.", error=True)
            return
        self._set_status("Resending confirmation email…", error=False)

        def _run():
            ok, msg = self._auth.resend_confirmation(email)
            self._root.after(0, self._set_status, msg, not ok)

        threading.Thread(target=_run, daemon=True).start()

    def _sign_in_google(self) -> None:
        import http.server
        import random
        import urllib.parse
        import webbrowser

        # Port 0 lets the OS pick a free port — no collision risk
        server = http.server.HTTPServer(("localhost", 0), None)
        port = server.server_address[1]
        server.server_close()
        redirect_uri = f"http://localhost:{port}"
        code_holder: dict = {}
        done = threading.Event()

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                code = params.get("code", [""])[0]
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body style='font-family:sans-serif;padding:40px'>"
                    b"<h2>Signed in! You can close this tab and return to FTC Whisper.</h2>"
                    b"</body></html>"
                )
                if code:
                    code_holder["code"] = code
                    done.set()

            def log_message(self, *_):
                pass

        server = http.server.HTTPServer(("localhost", port), _Handler)  # type: ignore[arg-type]

        def _serve():
            while not done.is_set():
                server.handle_request()
            server.server_close()

        threading.Thread(target=_serve, daemon=True).start()

        try:
            client = self._auth._get_client()
            result = client.auth.sign_in_with_oauth(
                {"provider": "google", "options": {"redirect_to": redirect_uri}}
            )
            webbrowser.open(result.url)
            self._set_status("Browser opened — sign in with Google…", error=False)
        except Exception as e:
            self._set_status(f"Google sign-in failed: {e}", error=True)
            return

        def _wait():
            if done.wait(timeout=120):
                code = code_holder.get("code", "")
                if code:
                    self._root.after(0, self._exchange_oauth_code, code)
                else:
                    self._root.after(0, self._set_status, "Google sign-in failed — no code received.", True)
            else:
                self._root.after(0, self._set_status, "Google sign-in timed out.", True)

        threading.Thread(target=_wait, daemon=True).start()

    def _exchange_oauth_code(self, code: str) -> None:
        def _run():
            try:
                client = self._auth._get_client()
                r = client.auth.exchange_code_for_session({"auth_code": code})
                if r and r.user and r.session:
                    self._auth._user = r.user
                    self._auth._save_session(r.session)
                    self._root.after(0, self._handle_result, True, f"Welcome, {r.user.email}")
                else:
                    self._root.after(0, self._set_status, "Google sign-in failed — could not verify session.", True)
            except Exception as e:
                self._root.after(0, self._set_status, f"Google sign-in error: {e}", True)

        threading.Thread(target=_run, daemon=True).start()

    def _finish(self) -> None:
        if not self._embedded:
            self._root.destroy()
        self._on_success(self._auth)

    def _handle_close(self) -> None:
        if not self._embedded:
            self._root.destroy()
        if self._on_cancel:
            self._on_cancel()

    def _set_status(self, msg: str, error: bool = True) -> None:
        self._status_var.set(msg)
        color = C["error"] if error else C["success"]
        self._status_lbl.configure(fg=color)
        self._status_frame.configure(bg=C["surface"])
        self._status_lbl.configure(bg=C["surface"])
        # Show the frame (may already be visible — pack is idempotent)
        self._status_frame.pack(fill="x", pady=(0, 10), before=self._submit_btn)
