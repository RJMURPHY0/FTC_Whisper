"""
Authentication manager for FTC Whisper.
Handles sign-up, sign-in, session persistence, and token refresh via Supabase Auth.
Session tokens are encrypted on disk using Windows DPAPI so they are only readable
by the same Windows user account.
"""

import ctypes
import ctypes.wintypes
import json
import os
import threading
import time
from typing import Optional



# ---------------------------------------------------------------------------
# Windows DPAPI helpers — encrypt/decrypt bytes using the current user's key
# ---------------------------------------------------------------------------


class _BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


_CRYPTPROTECT_UI_FORBIDDEN = 0x01


def _dpapi_encrypt(plaintext: bytes) -> bytes:
    buf_in = _BLOB(
        len(plaintext),
        ctypes.cast(
            ctypes.create_string_buffer(plaintext), ctypes.POINTER(ctypes.c_char)
        ),
    )
    buf_out = _BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(buf_in),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(buf_out),
    ):
        raise OSError(f"CryptProtectData failed (error {ctypes.GetLastError()})")
    enc = bytes(ctypes.string_at(buf_out.pbData, buf_out.cbData))
    ctypes.windll.kernel32.LocalFree(buf_out.pbData)
    return enc


def _dpapi_decrypt(ciphertext: bytes) -> bytes:
    buf_in = _BLOB(
        len(ciphertext),
        ctypes.cast(
            ctypes.create_string_buffer(ciphertext), ctypes.POINTER(ctypes.c_char)
        ),
    )
    buf_out = _BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(buf_in),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(buf_out),
    ):
        raise OSError(f"CryptUnprotectData failed (error {ctypes.GetLastError()})")
    dec = bytes(ctypes.string_at(buf_out.pbData, buf_out.cbData))
    ctypes.windll.kernel32.LocalFree(buf_out.pbData)
    return dec


# ---------------------------------------------------------------------------
# Session file path
# ---------------------------------------------------------------------------


def _session_path() -> str:
    # Store in %APPDATA%\FTC Whisper so the session survives EXE reinstalls
    # or moves to a different folder.
    app_data = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(app_data, "FTC Whisper")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, ".session")


def _last_email_path() -> str:
    """Plain-text file holding the last email that signed in — used only to
    prefill the login field. No password is ever stored here (the DPAPI
    session file handles credentials / auto-login)."""
    app_data = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(app_data, "FTC Whisper")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "last-email.txt")


def _read_last_email() -> str:
    # Prefer this app's own remembered email; fall back to a shared FTC email
    # file if a sibling FTC app (e.g. FTC Contacts) left one in the same folder.
    for path in (_last_email_path(),
                 os.path.join(os.path.dirname(_last_email_path()), "..",
                              "FTC", "last-email.txt")):
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    v = f.read().strip()
                if v:
                    return v
        except Exception:
            pass
    return ""


def _write_last_email(email: str) -> None:
    try:
        if not email:
            return
        with open(_last_email_path(), "w", encoding="utf-8") as f:
            f.write(email.strip())
    except Exception as e:
        print(f"[Auth] Could not save last email: {e}")


# ---------------------------------------------------------------------------
# AuthManager
# ---------------------------------------------------------------------------


_NETWORK_ERROR_MARKERS = (
    "getaddrinfo", "name or service not known", "temporary failure in name",
    "nodename nor servname", "connection", "connect", "timeout", "timed out",
    "unreachable", "ssl", "certificate", "handshake", "max retries",
    "network", "proxy", "remote end closed", "eof occurred", "server disconnected",
)

_AUTH_ERROR_MARKERS = (
    "invalid", "expired", "not found", "unauthorized",
    "refresh_token_not_found", "invalid_grant", "invalid_refresh_token",
)


def _is_definitive_auth_error(msg: str) -> bool:
    """True only for a server verdict that the saved tokens are dead.

    Network markers are tested FIRST and win: a DNS/TLS/connection failure can
    carry text like "not found" or "invalid", and treating that as an auth
    failure deletes a perfectly good session — which is exactly what signed the
    user out after a reboot, when the app starts before the network is up."""
    m = (msg or "").lower()
    if any(k in m for k in _NETWORK_ERROR_MARKERS):
        return False
    return any(k in m for k in _AUTH_ERROR_MARKERS)


class AuthManager:
    def __init__(self, supabase_url: str, supabase_key: str):
        self._url = supabase_url
        self._key = supabase_key
        self._client = None
        self._user = None
        # Session-restore concurrency. Supabase ROTATES refresh tokens, so two
        # restores racing on the same on-disk token guarantee one of them gets
        # "Refresh Token Not Found" — see _load_saved_session.
        self._restore_state_lock = threading.Lock()
        self._restore_thread = None
        self._restore_listener = None

    def set_restore_listener(self, fn) -> None:
        """Register a callback fired from a background thread when a session
        restore succeeds AFTER the caller stopped waiting for it. A cold boot
        routinely overruns the wait (client import + DNS + TLS + token refresh);
        without this the app ends up authenticated internally while the UI still
        shows the login form."""
        self._restore_listener = fn

    def _fire_restore_listener(self) -> None:
        fn = self._restore_listener
        if fn is None:
            return
        try:
            fn()
        except Exception as e:
            print(f"[Auth] Restore listener failed: {e}")

    def _get_client(self):
        if not self._client:
            try:
                from supabase import create_client  # lazy — only loaded when auth is used
                # supabase-auth ships NO timeout of its own, so every auth
                # round trip ran on httpx's 5-SECOND default. On a machine
                # that just booted (Wi-Fi still associating, Defender
                # scanning the freshly-updated exe, OneDrive saturating the
                # link) 5s is routinely blown, and both sign-in and session
                # restore failed with "The read operation timed out" until
                # the machine settled. 30s read / 15s connect rides that out;
                # a genuinely dead network still fails fast at connect.
                options = None
                try:
                    import httpx
                    from supabase.lib.client_options import SyncClientOptions
                    options = SyncClientOptions(
                        httpx_client=httpx.Client(
                            timeout=httpx.Timeout(30.0, connect=15.0)))
                except Exception as e:  # a future SDK rename must not kill auth
                    print(f"[Auth] Timeout options unavailable ({e}) — "
                          "using SDK defaults")
                if options is not None:
                    self._client = create_client(self._url, self._key,
                                                 options=options)
                else:
                    self._client = create_client(self._url, self._key)
                self._attach_auth_listener(self._client)
            except Exception as e:
                print(f"[Auth] ERROR creating Supabase client: {e}")
                raise
        return self._client

    def _attach_auth_listener(self, client) -> None:
        """Save session to disk whenever Supabase refreshes tokens."""
        def _on_state_change(event, session) -> None:
            if session:
                event_str = str(event)
                if "TOKEN_REFRESHED" in event_str or "SIGNED_IN" in event_str:
                    self._save_session(session)
                    print(f"[Auth] Session auto-saved ({event_str})")
        try:
            client.auth.on_auth_state_change(_on_state_change)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Session persistence (DPAPI-encrypted binary file)
    # ------------------------------------------------------------------

    def _save_session(self, session) -> None:
        try:
            payload = json.dumps(
                {
                    "access_token": session.access_token,
                    "refresh_token": session.refresh_token,
                }
            ).encode()
            encrypted = _dpapi_encrypt(payload)
            with open(_session_path(), "wb") as f:
                f.write(encrypted)
        except Exception as e:
            print(f"[Auth] Could not save session: {e}")

    def _load_saved_session(self, wait_seconds: float = 15.0) -> bool:
        """Try to restore a previous session from disk.

        Stops waiting after `wait_seconds` so a bad network never freezes
        startup — but the worker is NOT abandoned: it keeps going and calls the
        restore listener if it lands late. A cold boot routinely overruns the
        wait (lazy supabase import + DNS on a NIC that is still coming up + a
        rotating-refresh-token round trip), and the old code returned False,
        started a second restore, and ended with the user signed out.

        Only ONE restore may talk to the server at a time. Supabase rotates
        refresh tokens, so a second attempt replaying the same on-disk token
        gets "Refresh Token Not Found" — which the error heuristic below used
        to read as "these credentials are dead" and delete a session the first
        attempt had just refreshed successfully."""
        path = _session_path()
        if not os.path.exists(path):
            return False

        with self._restore_state_lock:
            running = self._restore_thread
            if running is not None and not running.is_alive():
                running = None
        if running is not None:
            running.join(timeout=wait_seconds)
            return self.is_authenticated

        handoff = threading.Lock()
        state = {"done": False, "ok": False, "abandoned": False}

        def _finish(ok: bool) -> None:
            with handoff:
                state["done"] = True
                state["ok"] = ok
                late = state["abandoned"]
            if late and ok:
                print("[Auth] Session restored after the startup wait — promoting.")
                self._fire_restore_listener()

        def _restore() -> None:
            ok = False
            try:
                ok = self._restore_once(path)
            finally:
                _finish(ok)

        t = threading.Thread(target=_restore, daemon=True, name="auth-restore")
        with self._restore_state_lock:
            self._restore_thread = t
        t.start()
        t.join(timeout=wait_seconds)

        with handoff:
            if state["done"]:
                return state["ok"]
            state["abandoned"] = True

        # Still in flight. Keep the session file — the worker owns the outcome
        # now and will promote the UI itself if it succeeds.
        print(f"[Auth] Session restore still running after {wait_seconds:.0f}s "
              f"— continuing in the background (session kept)")
        return False

    def _restore_once(self, path: str) -> bool:
        """One restore attempt, run to completion. Returns True when the user
        is signed in. Never raises."""
        raw = b""
        try:
            with open(path, "rb") as f:
                raw = f.read()

            # Support both new (DPAPI-encrypted) and legacy (plain JSON) files
            try:
                data = json.loads(_dpapi_decrypt(raw).decode())
            except Exception:
                # Fall back to plain JSON for sessions saved before the upgrade
                try:
                    data = json.loads(raw.decode())
                except Exception:
                    # Neither decrypts nor parses — corrupt file or a
                    # transient DPAPI/profile hiccup. Do NOT let this fall
                    # into the auth-keyword heuristic below (the decode
                    # error text contains "invalid", which used to delete
                    # the session and silently sign the user out).
                    print("[Auth] Session file unreadable (decrypt/parse "
                          "failed) — keeping it; will retry next launch.")
                    return False

            client = self._get_client()
            at = data.get("access_token") or ""
            rt = data.get("refresh_token") or ""
            if not at or not rt:
                raise KeyError("Missing tokens in session file")
            r = client.auth.set_session(at, rt)
            if r and r.user:
                self._user = r.user
                # Re-save with encryption in case it was a legacy file
                self._save_session(
                    r.session
                    or type(
                        "S",
                        (),
                        {
                            "access_token": data["access_token"],
                            "refresh_token": data["refresh_token"],
                        },
                    )()
                )
                print(f"[Auth] Restored session for {self._user.email}")
                return True
            return False
        except Exception as e:
            if not _is_definitive_auth_error(str(e)):
                print(f"[Auth] Session restore failed (network/other): {e}")
                return False
            # A server verdict of "these tokens are dead" — but only act on it
            # if nothing else has already succeeded. Deleting a session that a
            # concurrent (or earlier, late-landing) attempt just refreshed is
            # what signed the user out for real.
            if self._user is not None:
                print(f"[Auth] Ignoring stale auth error (already signed in): {e}")
                return False
            if self._session_bytes_changed(path, raw):
                print(f"[Auth] Ignoring stale auth error (session file was "
                      f"refreshed meanwhile): {e}")
                return False
            print(f"[Auth] Session restore failed (auth error): {e}")
            self._clear_session()
            return False

    @staticmethod
    def _session_bytes_changed(path: str, previous: bytes) -> bool:
        """True when the session file no longer holds the bytes this attempt
        read — i.e. someone wrote a fresher session while we were in flight."""
        try:
            with open(path, "rb") as f:
                return f.read() != previous
        except Exception:
            return False

    def _clear_session(self) -> None:
        path = _session_path()
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
        self._user = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_authenticated(self) -> bool:
        return self._user is not None

    @property
    def user_id(self) -> Optional[str]:
        return str(self._user.id) if self._user else None

    @property
    def user_email(self) -> Optional[str]:
        return self._user.email if self._user else None

    @property
    def last_email(self) -> str:
        """Last email that successfully signed in (for prefilling the form)."""
        return _read_last_email()

    def try_restore_session(self, wait_seconds: float = 15.0) -> bool:
        """Called at startup — returns True if a valid saved session exists."""
        return self._load_saved_session(wait_seconds)

    @property
    def restore_in_flight(self) -> bool:
        """True while a restore attempt is still talking to the server. The UI
        uses this to keep the 'Signing you in…' splash up instead of dropping
        the user onto the login form mid-attempt."""
        t = self._restore_thread
        return bool(t is not None and t.is_alive())

    def has_saved_session(self) -> bool:
        """True if an encrypted session file still exists on disk. A failed restore
        keeps the file on network/timeout errors and deletes it only on a definitive
        auth failure — so this signals whether a restore is still worth retrying."""
        return os.path.exists(_session_path())

    def sign_in_offline(self) -> None:
        """Mark as authenticated without Supabase — used when auth is disabled."""
        import types

        self._user = types.SimpleNamespace(id="local", email="")

    def sign_up(self, email: str, password: str) -> tuple[bool, str]:
        try:
            client = self._get_client()
            result = client.auth.sign_up({"email": email, "password": password})
            print(f"[Auth] Sign-up result: user={result.user}, session={result.session}")
            if result.user:
                if result.session:
                    self._user = result.user
                    self._save_session(result.session)
                    return True, "Account created and signed in!"
                # User created but email confirmation required
                return True, "Account created — confirmation email sent."
            return False, "Sign-up failed: no user returned. Please try again."
        except Exception as e:
            msg = str(e)
            print(f"[Auth] Sign-up error: {msg}")
            if "already registered" in msg.lower() or "already exists" in msg.lower():
                return False, "An account with that email already exists. Try signing in."
            if "password" in msg.lower() and "weak" in msg.lower():
                return False, "Password too weak — use at least 6 characters."
            return False, f"Sign-up failed: {msg}"

    def sign_in(self, email: str, password: str) -> tuple[bool, str]:
        msg = ""
        for attempt in (1, 2):
            try:
                client = self._get_client()
                print(f"[Auth] Signing in as {email}...")
                result = client.auth.sign_in_with_password(
                    {"email": email, "password": password}
                )
                if result.user and result.session:
                    self._user = result.user
                    self._save_session(result.session)
                    _write_last_email(result.user.email or email)
                    return True, f"Welcome back, {result.user.email}"
                return False, "Sign-in failed — please check your email and password."
            except Exception as e:
                # Supabase wraps the real message in several ways
                msg = getattr(e, "message", None) or (e.args[0] if e.args else str(e))
                msg = str(msg)
                print(f"[Auth] Sign-in error (attempt {attempt}): {msg!r}")
                low = msg.lower()
                # Network markers win, same rule as _is_definitive_auth_error:
                # a TLS/proxy failure can carry words like "invalid", and
                # reading that as a wrong password would mislead the user.
                if any(k in low for k in _NETWORK_ERROR_MARKERS):
                    if attempt == 1:
                        # One transient hiccup (cold boot, Wi-Fi
                        # reassociating) must not surface as a failure.
                        time.sleep(1.5)
                        continue
                    return False, ("Can't reach the sign-in server — check "
                                   "your internet connection and try again.")
                if "email not confirmed" in low or "email_not_confirmed" in low:
                    return False, "Email not confirmed — check your inbox and click the confirmation link."
                if "invalid" in low or "credentials" in low or "wrong" in low:
                    return False, "Incorrect email or password."
                if "user not found" in low or "no user" in low:
                    return False, "No account found with that email."
                return False, f"Sign-in failed: {msg or 'unknown error — check your connection.'}"
        return False, f"Sign-in failed: {msg or 'unknown error — check your connection.'}"

    def reset_password(self, email: str) -> tuple[bool, str]:
        try:
            client = self._get_client()
            client.auth.reset_password_email(email)
            return True, "Password reset email sent — check your inbox."
        except Exception as e:
            msg = str(e)
            print(f"[Auth] Reset password error: {msg}")
            if "user not found" in msg.lower() or "no user" in msg.lower():
                return False, "No account found with that email."
            return False, "Reset failed — please try again."

    def resend_confirmation(self, email: str) -> tuple[bool, str]:
        try:
            client = self._get_client()
            client.auth.resend({"type": "signup", "email": email})
            return True, "Confirmation email resent — check your inbox."
        except Exception as e:
            msg = str(e)
            print(f"[Auth] Resend confirmation error: {msg}")
            return False, "Could not resend — please try again."

    def sign_out(self) -> None:
        # Grab client before clearing so we can revoke server-side in background
        client = self._client
        self._clear_session()
        self._client = None

        def _revoke():
            try:
                if client:
                    client.auth.sign_out()
            except Exception:
                pass

        threading.Thread(target=_revoke, daemon=True).start()
        print("[Auth] Signed out.")
