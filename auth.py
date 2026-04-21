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
import sys
import threading
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
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, ".session")


# ---------------------------------------------------------------------------
# AuthManager
# ---------------------------------------------------------------------------


class AuthManager:
    def __init__(self, supabase_url: str, supabase_key: str):
        self._url = supabase_url
        self._key = supabase_key
        self._client = None
        self._user = None

    def _get_client(self):
        if not self._client:
            try:
                from supabase import create_client

                self._client = create_client(self._url, self._key)
            except Exception as e:
                print(f"[Auth] ERROR creating Supabase client: {e}")
                raise
        return self._client

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

    def _load_saved_session(self) -> bool:
        """Try to restore a previous session from disk.
        Times out after 8 s so a bad network never freezes startup."""
        path = _session_path()
        if not os.path.exists(path):
            return False

        result: list = [False]

        def _restore() -> None:
            try:
                with open(path, "rb") as f:
                    raw = f.read()

                # Support both new (DPAPI-encrypted) and legacy (plain JSON) files
                try:
                    data = json.loads(_dpapi_decrypt(raw).decode())
                except Exception:
                    # Fall back to plain JSON for sessions saved before the upgrade
                    data = json.loads(raw.decode())

                client = self._get_client()
                at = data.get("access_token") or data.get("access_token", "")
                rt = data.get("refresh_token") or data.get("refresh_token", "")
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
                    result[0] = True
            except Exception as e:
                print(f"[Auth] Session restore failed: {e}")
                self._clear_session()

        t = threading.Thread(target=_restore, daemon=True)
        t.start()
        t.join(timeout=8.0)

        if t.is_alive():
            print("[Auth] Session restore timed out (8 s) — proceeding offline")
            self._clear_session()
            return False

        return result[0]

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

    def try_restore_session(self) -> bool:
        """Called at startup — returns True if a valid saved session exists."""
        return self._load_saved_session()

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
        try:
            client = self._get_client()
            result = client.auth.sign_in_with_password(
                {"email": email, "password": password}
            )
            if result.user and result.session:
                self._user = result.user
                self._save_session(result.session)
                return True, f"Welcome back, {result.user.email}"
            return False, "Sign-in failed — please check your email and password."
        except Exception as e:
            # Supabase wraps the real message in several ways
            msg = getattr(e, "message", None) or getattr(e, "args", [""])[0] if e.args else str(e)
            msg = str(msg)
            print(f"[Auth] Sign-in error: {msg!r}")
            if "email not confirmed" in msg.lower() or "email_not_confirmed" in msg.lower():
                return False, "Email not confirmed — an admin needs to confirm your account first."
            if "invalid" in msg.lower() or "credentials" in msg.lower() or "wrong" in msg.lower():
                return False, "Incorrect email or password."
            if "user not found" in msg.lower() or "no user" in msg.lower():
                return False, "No account found with that email."
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
        try:
            self._get_client().auth.sign_out()
        except Exception:
            pass
        self._clear_session()
        self._client = None  # force fresh client on next sign-in
        print("[Auth] Signed out.")
