"""
Supabase integration for FTC Whisper.
Logs transcriptions and AI refinements. All calls are fire-and-forget
on a background thread — a Supabase outage will never block the app.
"""

import threading
import datetime
from queue import Queue, Full
from typing import Optional

# Table name in Supabase
_TABLE = "transcriptions"


class SupabaseLogger:
    def __init__(self, url: str, key: str):
        self._url = url
        self._key = key
        self._client = None
        self._enabled = bool(url and key)
        self._user_id: Optional[str] = None
        self._write_queue: Queue[dict] = Queue(maxsize=200)
        self._worker_started = False
        self._worker_lock = threading.Lock()

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def set_user(self, user_id: Optional[str]) -> None:
        """Set the authenticated user ID to include in all log entries."""
        self._user_id = user_id

    def set_client(self, client) -> None:
        """Share an already-authenticated Supabase client (bypasses RLS)."""
        self._client = client

    def _get_client(self):
        if self._client is None:
            from supabase import create_client

            self._client = create_client(self._url, self._key)
        return self._client

    # ------------------------------------------------------------------
    # Public API — all fire-and-forget
    # ------------------------------------------------------------------

    def log_transcription(self, text: str) -> None:
        """Save a new transcription record."""
        if not self._enabled:
            return
        payload = {
            "transcribed_text": text,
            "created_at": datetime.datetime.utcnow().isoformat(),
        }
        if self._user_id and self._user_id != "local":
            payload["user_id"] = self._user_id
        self._run(payload)

    def log_refinement(self, original: str, refined: str, mode: str) -> None:
        """Insert a refinement record."""
        if not self._enabled:
            return
        payload = {
            "transcribed_text": original,
            "refined_text": refined,
            "refinement_mode": mode,
            "created_at": datetime.datetime.utcnow().isoformat(),
        }
        if self._user_id and self._user_id != "local":
            payload["user_id"] = self._user_id
        self._run(payload)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def fetch_history(self, limit: int = 30) -> list:
        """Fetch recent transcriptions (synchronous, 10 s timeout)."""
        if not self._enabled:
            return []

        result: list = [None]
        error: list = [None]

        def _fetch() -> None:
            try:
                q = (
                    self._get_client()
                    .table(_TABLE)
                    .select("transcribed_text, refined_text, created_at")
                    .order("created_at", desc=True)
                    .limit(limit)
                )
                if self._user_id and self._user_id != "local":
                    q = q.eq("user_id", self._user_id)
                result[0] = q.execute().data or []
            except Exception as e:
                error[0] = e

        t = threading.Thread(target=_fetch, daemon=True)
        t.start()
        t.join(timeout=10.0)

        if t.is_alive():
            print("[Supabase] Fetch history timed out")
            return []
        if error[0]:
            print(f"[Supabase] Fetch history failed: {error[0]}")
            return []
        return result[0] or []

    def clear_history(self) -> bool:
        """Delete all transcription records for the current user. Returns True on success."""
        if not self._enabled:
            return False
        if not self._user_id or self._user_id == "local":
            print("[Supabase] Clear history skipped: no authenticated user_id")
            return False
        try:
            q = self._get_client().table(_TABLE).delete()
            q = q.eq("user_id", self._user_id)
            q.execute()
            print("[Supabase] History cleared.")
            return True
        except Exception as e:
            print(f"[Supabase] Clear history failed: {e}")
            return False

    def _run(self, payload: dict) -> None:
        """Queue payload for background insert without spawning unbounded threads."""
        if not self._enabled:
            return
        self._ensure_worker()
        try:
            self._write_queue.put_nowait(payload)
        except Full:
            print("[Supabase] Log queue full — dropping oldest entry")
            try:
                _ = self._write_queue.get_nowait()
            except Exception:
                pass
            try:
                self._write_queue.put_nowait(payload)
            except Exception:
                print("[Supabase] Log drop persisted — queue saturated")

    def _ensure_worker(self) -> None:
        if self._worker_started:
            return
        with self._worker_lock:
            if self._worker_started:
                return
            threading.Thread(
                target=self._worker_loop, daemon=True, name="supabase-logger"
            ).start()
            self._worker_started = True

    def _worker_loop(self) -> None:
        while True:
            payload = self._write_queue.get()
            try:
                self._insert(payload)
            finally:
                self._write_queue.task_done()

    def _insert(self, payload: dict) -> None:
        try:
            self._get_client().table(_TABLE).insert(payload).execute()
            print(f"[Supabase] Logged: {list(payload.keys())}")
        except Exception as e:
            print(f"[Supabase] Log failed (non-fatal): {e}")
