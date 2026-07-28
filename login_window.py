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
    "field_bg": "#141414",     # login email/password bars — dark, one colour with the page (the light bars read as white boxes)
    "field_border": "#333333", # subtle field outline; turns accent on focus
    "field_text": "#ffffff",   # light text on the dark bars
    "text": "#ffffff",
    "subtext": "#777777",
    "accent": "#f39200",
    "accent_hover": "#e08200",
    "error": "#ff5555",
    "success": "#4ade80",
    "divider": "#2d2d2d",
    "card": "#161616",         # rounded card fill (slightly darker than surface)
    "card_border": "#2a2a2a",  # hairline around the card
    "seg_track": "#0f0f0f",    # segmented-toggle track
    "seg_active": "#2b2b2b",   # active segment pill
}

WINDOW_W = 400
WINDOW_H = 648


def _round_rect(canvas, x1, y1, x2, y2, r, **kw):
    """Draw a rounded rectangle (smoothed polygon) on a tk.Canvas."""
    r = max(0, min(r, (x2 - x1) // 2, (y2 - y1) // 2))
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


class _RoundedButton(tk.Canvas):
    """A full-width rounded button drawn on a Canvas, with hover + a .set() API
    so callers can restyle it (text/colour/cursor) like a Label."""

    def __init__(self, parent, text, command, *, bg, fg, hover=None,
                 font=("Segoe UI", 12, "bold"), height=46, radius=12):
        super().__init__(parent, height=height, bg=C["surface"],
                         highlightthickness=0, bd=0)
        self._text, self._bg, self._fg = text, bg, fg
        self._hover = hover or bg
        self._font, self._radius, self._cmd = font, radius, command
        self._cur = bg
        self.configure(cursor="hand2")
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Button-1>", lambda _e: self._cmd() if self._cmd else None)
        self.bind("<Enter>", lambda _e: self._paint(self._hover))
        self.bind("<Leave>", lambda _e: self._paint(self._bg))

    def _paint(self, c):
        self._cur = c
        self._draw()

    def _draw(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return
        self.delete("all")
        _round_rect(self, 1, 1, w - 1, h - 1, self._radius, fill=self._cur, outline="")
        self.create_text(w // 2, h // 2, text=self._text, fill=self._fg, font=self._font)

    def set(self, text=None, bg=None, fg=None, hover=None, cursor=None):
        if text is not None:
            self._text = text
        if bg is not None:
            self._bg = self._cur = bg
        if fg is not None:
            self._fg = fg
        if hover is not None:
            self._hover = hover
        if cursor is not None:
            self.configure(cursor=cursor)
        self._draw()


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
        self._submitting = False

    def embed(self, frame: tk.Frame) -> None:
        """Build login UI into an existing frame (in-window, no Toplevel)."""
        self._embedded = True
        self._root = frame.winfo_toplevel()
        self._build_ui(container=frame)

    def reset(self) -> None:
        """Clear form fields and reset to login mode — call before showing again."""
        self._submitting = False
        if hasattr(self, "_email_var"):
            try:
                self._email_var.set(self._auth.last_email or "")
            except Exception:
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
        # Window / taskbar icon — FTC swirl (logo.ico)
        try:
            from logo_cache import get_icon_path
            _ico = get_icon_path()
            if _ico:
                self._root.iconbitmap(default=_ico)
        except Exception:
            pass
        self._root.configure(bg=C["bg"])
        self._root.resizable(False, False)
        self._root.protocol("WM_DELETE_WINDOW", self._handle_close)
        self._root.geometry(f"{WINDOW_W}x{WINDOW_H}+{x}+{y}")

        self._build_ui()
        self._apply_dark_frame()

        if parent is not None:
            self._root.wait_window()
        else:
            self._root.mainloop()

    def _apply_dark_frame(self) -> None:
        """Windows 11: dark caption/border on the standalone login window.
        Unset, DWM draws its default light frame — white edging around the
        dark UI. (The embedded login inherits the main window's treatment.)"""
        try:
            import ctypes
            self._root.update_idletasks()
            hwnd = ctypes.windll.user32.GetAncestor(self._root.winfo_id(), 2)
            dwm = ctypes.windll.dwmapi

            def _cr(hex_color):
                h = hex_color.lstrip("#")
                r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
                return (b << 16) | (g << 8) | r

            for attr, val in ((20, 1),                    # immersive dark mode
                              (34, _cr(C["bg"])),         # border
                              (35, _cr(C["surface"])),    # caption
                              (36, _cr("#ffffff"))):      # caption text
                dwm.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(ctypes.c_int(val)),
                    ctypes.sizeof(ctypes.c_int))
        except Exception:
            pass

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

        # ── Rounded card holding the segmented toggle + form ───────────
        body = tk.Frame(c, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=22, pady=(0, 24))

        self._card_cv = tk.Canvas(body, bg=C["bg"], highlightthickness=0, bd=0)
        self._card_cv.pack(fill="both", expand=True)

        holder = tk.Frame(self._card_cv, bg=C["surface"])
        holder_win = self._card_cv.create_window(0, 0, window=holder, anchor="nw")

        # Inset the (square-cornered) content frame by >= the corner radius so it
        # never pokes past the rounded arc — the canvas fill covers the straight
        # edges seamlessly (same surface colour).
        _CARD_PAD = 18

        def _draw_card(_e=None):
            w, h = self._card_cv.winfo_width(), self._card_cv.winfo_height()
            if w < 2 or h < 2:
                return
            self._card_cv.delete("cardbg")
            _round_rect(self._card_cv, 1, 1, w - 2, h - 2, 18,
                        fill=C["surface"], outline=C["card_border"], tags="cardbg")
            self._card_cv.tag_lower("cardbg")
            self._card_cv.coords(holder_win, _CARD_PAD, _CARD_PAD)
            self._card_cv.itemconfigure(
                holder_win, width=w - 2 * _CARD_PAD, height=h - 2 * _CARD_PAD)
        self._card_cv.bind("<Configure>", _draw_card)

        # Segmented Sign In / Create Account toggle
        self._seg = tk.Canvas(holder, height=40, bg=C["surface"],
                              highlightthickness=0, bd=0, cursor="hand2")
        self._seg.pack(fill="x", pady=(2, 18))
        self._seg.bind("<Configure>", lambda _e: self._draw_segment())
        self._seg.bind("<Button-1>", self._seg_click)

        # ── Form area (children keep surface bg — blends into the card) ─
        self._card = tk.Frame(holder, bg=C["surface"])
        self._card.pack(fill="both", expand=True)

        self._email_var = tk.StringVar()
        self._password_var = tk.StringVar()
        self._confirm_var = tk.StringVar()

        # Email
        self._field_label(self._card, "Email")
        email_wrap, self._email_entry, _ = self._rounded_field(self._card, self._email_var)
        email_wrap.pack(fill="x", pady=(5, 14))

        # Password
        self._field_label(self._card, "Password")
        pass_wrap, self._pass_entry, self._pass_eye = self._rounded_field(
            self._card, self._password_var, show="•", with_eye=True)
        pass_wrap.pack(fill="x", pady=(5, 14))
        self._pass_visible = False
        self._pass_eye.bind("<Button-1>", lambda _e: self._toggle_pass())

        # Confirm password section — kept in a frame so _switch can reliably
        # reposition it above the submit button using before=
        self._confirm_section = tk.Frame(self._card, bg=C["surface"])
        tk.Label(
            self._confirm_section, text="Confirm Password",
            fg=C["subtext"], bg=C["surface"],
            font=("Segoe UI", 10), anchor="w",
        ).pack(fill="x")
        confirm_wrap, self._confirm_entry, self._confirm_eye = self._rounded_field(
            self._confirm_section, self._confirm_var, show="•", with_eye=True)
        confirm_wrap.pack(fill="x", pady=(5, 12))
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

        # Submit button (rounded)
        self._submit_btn = _RoundedButton(
            self._card, "Sign In", self._submit,
            bg=C["accent"], fg=C["bg"], hover=C["accent_hover"], height=46,
        )
        self._submit_btn.pack(fill="x", pady=(6, 0))

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

        # Google sign-in button (rounded)
        self._google_btn = _RoundedButton(
            self._card, "Continue with Google", self._sign_in_google,
            bg=C["input_bg"], fg=C["text"], hover=C["divider"],
            font=("Segoe UI", 11), height=44,
        )

        # Divider + Google button always visible — packed once here
        self._divider_frame.pack(fill="x", pady=(12, 0))
        self._google_btn.pack(fill="x", pady=(8, 0))

        # Enter key submits
        self._root.bind("<Return>", lambda _e: self._submit())

        self._switch("login")

        # Prefill the last email that signed in — returning users only type their
        # password (password is never stored; the DPAPI session handles re-login).
        try:
            remembered = self._auth.last_email
        except Exception:
            remembered = ""
        if remembered:
            self._email_var.set(remembered)
            self._root.after(60, lambda: self._pass_entry.focus_set())
        else:
            self._root.after(60, lambda: self._email_entry.focus_set())

    def _draw_segment(self) -> None:
        t = self._seg
        w, h = t.winfo_width(), t.winfo_height()
        if w < 2 or h < 2:
            return
        t.delete("all")
        _round_rect(t, 1, 1, w - 2, h - 2, (h - 2) // 2,
                    fill=C["seg_track"], outline="")
        half = w / 2
        pad = 4
        if self._mode == "login":
            x0, x1 = pad, half - pad / 2
        else:
            x0, x1 = half + pad / 2, w - pad
        _round_rect(t, x0, pad, x1, h - pad, (h - 2 * pad) // 2,
                    fill=C["seg_active"], outline="")
        login_fg = C["accent"] if self._mode == "login" else C["subtext"]
        signup_fg = C["accent"] if self._mode == "signup" else C["subtext"]
        t.create_text(half / 2, h / 2, text="Sign In",
                      fill=login_fg, font=("Segoe UI", 10, "bold"))
        t.create_text(half + half / 2, h / 2, text="Create Account",
                      fill=signup_fg, font=("Segoe UI", 10, "bold"))

    def _seg_click(self, event) -> None:
        half = self._seg.winfo_width() / 2
        self._switch("login" if event.x < half else "signup")

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

    def _rounded_field(self, parent, var, show="", with_eye=False):
        """A rounded, light-grey input field. Returns (canvas, entry, eye|None).
        The rounded shape is drawn on a Canvas; the Entry (and optional eye toggle)
        sit inside it, inset so only the rounded outline shows."""
        H, R, PAD = 44, 12, 14
        wrap = tk.Canvas(parent, height=H, bg=C["surface"], highlightthickness=0, bd=0)
        inner = tk.Frame(wrap, bg=C["field_bg"])
        entry = tk.Entry(
            inner, textvariable=var, show=show,
            bg=C["field_bg"], fg=C["field_text"],
            insertbackground=C["accent"], relief="flat", bd=0,
            highlightthickness=0, font=("Segoe UI", 12),
        )
        entry.pack(side="left", fill="x", expand=True, ipady=1)
        eye = None
        if with_eye:
            eye = tk.Label(inner, text="👁", bg=C["field_bg"], fg=C["subtext"],
                           font=("Segoe UI", 12), cursor="hand2", padx=2)
            eye.pack(side="right")
        win = wrap.create_window(PAD, H // 2, window=inner, anchor="w")

        state = {"focus": False}

        def _draw(_e=None):
            w = wrap.winfo_width()
            if w <= 1:
                return
            wrap.delete("bg")
            outline = C["accent"] if state["focus"] else C["field_border"]
            width = 2 if state["focus"] else 1
            _round_rect(wrap, 2, 3, w - 2, H - 3, R,
                        fill=C["field_bg"], outline=outline, width=width, tags="bg")
            wrap.tag_lower("bg")
            wrap.itemconfigure(win, width=max(1, w - PAD * 2))

        wrap.bind("<Configure>", _draw)

        def _focus(v):
            state["focus"] = v
            _draw()

        entry.bind("<FocusIn>", lambda _e: _focus(True))
        entry.bind("<FocusOut>", lambda _e: _focus(False))
        # Clicking anywhere on the field focuses the entry
        wrap.bind("<Button-1>", lambda _e: entry.focus_set())
        return wrap, entry, eye

    # ------------------------------------------------------------------
    # Mode switching
    # ------------------------------------------------------------------

    def _switch(self, mode: str, clear_status: bool = True) -> None:
        self._mode = mode
        self._forgot_link.pack_forget()
        self._resend_link.pack_forget()
        if mode == "login":
            self._confirm_section.pack_forget()
            self._submit_btn.set(text="Sign In")
            # before=divider so the link sits under Sign In, not below Google
            # (packing it last was pushing it off the bottom of the window).
            self._forgot_link.pack(anchor="center", pady=(10, 0),
                                   before=self._divider_frame)
            if self._pending_confirm_email:
                self._resend_link.pack(anchor="center", pady=(4, 0),
                                       before=self._divider_frame)
        else:
            self._confirm_section.pack(fill="x", before=self._submit_btn)
            self._submit_btn.set(text="Create Account")
        if hasattr(self, "_seg"):
            self._draw_segment()
        if clear_status:
            self._status_var.set("")
            self._status_frame.pack_forget()

    # ------------------------------------------------------------------
    # Form submission
    # ------------------------------------------------------------------

    def _submit(self) -> None:
        if self._submitting:
            return
        email = self._email_entry.get().strip()
        password = self._pass_entry.get()

        if not email or not password:
            self._set_status("Please enter your email and password.", error=True)
            return

        if self._mode == "signup":
            if password != self._confirm_entry.get():
                self._set_status("Passwords do not match.", error=True)
                return
            if len(password) < 6:
                self._set_status("Password must be at least 6 characters.", error=True)
                return

        self._submitting = True
        self._set_status("Please wait…", error=False)
        self._submit_btn.set(bg=C["divider"], hover=C["divider"], cursor="")

        def _run():
            if self._mode == "login":
                ok, msg = self._auth.sign_in(email, password)
            else:
                ok, msg = self._auth.sign_up(email, password)

            self._root.after(0, self._handle_result, ok, msg)

        threading.Thread(target=_run, daemon=True).start()

    def _handle_result(self, ok: bool, msg: str) -> None:
        self._submitting = False
        print(f"[Login] ok={ok} msg={msg!r}")

        if ok and self._auth.is_authenticated:
            self._pending_confirm_email = None
            name = self._auth.user_email or "you"
            self._submit_btn.set(
                text=f"✓  Welcome back, {name}!",
                bg=C["success"], fg=C["bg"], hover=C["success"], cursor="",
            )
            self._root.after(1200, self._finish)
            return

        self._submit_btn.set(bg=C["accent"], hover=C["accent_hover"], cursor="hand2")

        if ok and self._mode == "signup":
            email = self._email_entry.get().strip()
            self._pending_confirm_email = email
            self._set_status(
                f"✉  Confirmation email sent to {email}\n"
                "Check your inbox, click the link, then sign in here.",
                error=False,
            )
            self._pass_entry.delete(0, "end")
            self._confirm_entry.delete(0, "end")
            self._switch("login", clear_status=False)
            return

        if not ok and ("email not confirmed" in msg.lower() or "email_not_confirmed" in msg.lower()):
            self._pending_confirm_email = self._email_entry.get().strip()
            self._resend_link.pack_forget()
            self._resend_link.pack(anchor="center", pady=(4, 0))

        self._submit_btn.set(
            text=f"✕  {msg}",
            bg=C["error"], fg=C["text"], hover=C["error"], cursor="hand2",
        )
        self._root.after(3000, lambda: self._submit_btn.set(
            text="Sign In", bg=C["accent"], fg=C["bg"], hover=C["accent_hover"], cursor="hand2"
        ))

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
        email = self._email_entry.get().strip()
        if not email:
            self._set_status("Enter your email address above first.", error=True)
            return
        self._set_status("Sending reset email…", error=False)

        def _run():
            ok, msg = self._auth.reset_password(email)
            self._root.after(0, self._set_status, msg, not ok)

        threading.Thread(target=_run, daemon=True).start()

    def _resend_confirmation(self) -> None:
        email = self._pending_confirm_email or self._email_entry.get().strip()
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
