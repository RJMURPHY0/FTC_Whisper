"""
Supabase integration for FTC Whisper.
Logs transcriptions and AI refinements. All calls are fire-and-forget
on a background thread — a Supabase outage will never block the app.
"""

import json
import os
import threading
import datetime
from queue import Queue, Full
from typing import Optional

# Table name in Supabase
_TABLE = "transcriptions"

_local_history_lock = threading.Lock()


def _local_history_path() -> str:
    app_data = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(app_data, "FTC Whisper")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "history.json")


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

    def log_transcription(self, text: str, app_name: str = "",
                          app_exe: str = "") -> None:
        """Save a new transcription record (with the app it was injected into)."""
        self._append_local(text, app_name=app_name, app_exe=app_exe)
        if not self._enabled:
            return
        payload = {
            "transcribed_text": text,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if app_name:
            payload["app_name"] = app_name
        if app_exe:
            payload["app_exe"] = app_exe
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
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if self._user_id and self._user_id != "local":
            payload["user_id"] = self._user_id
        self._run(payload)

    def log_update_event(self, stage: str, from_version: str = "",
                         to_version: str = "", ok=None, detail: str = "") -> None:
        """Fire-and-forget: record an auto-update outcome to the update_events
        table so update success/failure can be monitored across the whole fleet
        (which devices update vs. get stuck). Best-effort — a missing table, RLS
        block, or outage is swallowed and never affects the update itself.

        stage ∈ {"download_start","download_ok","download_fail","swap_started",
                 "manual_fallback_browser","announced"}.
        """
        if not self._enabled:
            return
        payload = {
            "stage": stage,
            "from_version": from_version,
            "to_version": to_version,
            "ok": ok,
            "detail": (detail or "")[:500],
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if self._user_id and self._user_id != "local":
            payload["user_id"] = self._user_id

        def _insert():
            try:
                self._get_client().table("update_events").insert(payload).execute()
                print(f"[Supabase] update_event: {stage} ok={ok}")
            except Exception as e:
                print(f"[Supabase] update_event log failed (non-fatal): {e}")

        threading.Thread(target=_insert, daemon=True,
                         name="supabase-update-log").start()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def fetch_app_setting(self, key: str) -> str:
        """Fetch a single value from the app_settings table (synchronous, 8 s timeout)."""
        if not self._enabled:
            return ""
        result: list = [""]
        def _fetch():
            try:
                r = (self._get_client()
                     .table("app_settings")
                     .select("value")
                     .eq("key", key)
                     .limit(1)
                     .execute())
                if r.data:
                    result[0] = r.data[0].get("value", "")
            except Exception as e:
                print(f"[Supabase] fetch_app_setting({key!r}) failed: {e}")
        t = threading.Thread(target=_fetch, daemon=True)
        t.start()
        t.join(timeout=8.0)
        return result[0]

    def fetch_history(self, limit: int = 30) -> list:
        """Fetch recent transcriptions (synchronous, 10 s timeout).
        Falls back to the local history file if Supabase is unavailable or empty."""
        if not self._enabled:
            return self._fetch_local(limit)

        result: list = [None]
        error: list = [None]

        def _fetch() -> None:
            # Try with the app columns first; fall back to the legacy column
            # set if the remote table doesn't have them yet.
            for cols in (
                "transcribed_text, refined_text, created_at, app_name, app_exe",
                "transcribed_text, refined_text, created_at",
            ):
                try:
                    q = (
                        self._get_client()
                        .table(_TABLE)
                        .select(cols)
                        .order("created_at", desc=True)
                        .limit(limit)
                    )
                    if self._user_id and self._user_id != "local":
                        q = q.eq("user_id", self._user_id)
                    result[0] = q.execute().data or []
                    error[0] = None
                    return
                except Exception as e:
                    error[0] = e

        t = threading.Thread(target=_fetch, daemon=True)
        t.start()
        t.join(timeout=10.0)

        if t.is_alive():
            print("[Supabase] Fetch history timed out — using local history")
            return self._fetch_local(limit)
        if error[0]:
            print(f"[Supabase] Fetch history failed: {error[0]} — using local history")
            return self._fetch_local(limit)
        if not result[0]:
            return self._fetch_local(limit)
        return self._enrich_from_local(result[0])

    def _enrich_from_local(self, remote: list) -> list:
        """Fill in app_name/app_exe from the local file for remote rows that
        lack them (remote table without the columns, or rows logged before
        app capture existed). Matched by text + close timestamp."""
        if all(r.get("app_name") for r in remote):
            return remote
        local = self._fetch_local(200)
        if not local:
            return remote

        def _ts(rec):
            try:
                return datetime.datetime.fromisoformat(
                    (rec.get("created_at") or "").replace("Z", "+00:00"))
            except Exception:
                return None

        for r in remote:
            if r.get("app_name"):
                continue
            rt = _ts(r)
            for l in local:
                if not l.get("app_name"):
                    continue
                if l.get("transcribed_text") != r.get("transcribed_text"):
                    continue
                lt = _ts(l)
                if rt and lt and abs((rt - lt).total_seconds()) > 10:
                    continue
                r["app_name"] = l.get("app_name", "")
                r["app_exe"] = l.get("app_exe", "")
                break
        return remote

    def clear_history(self) -> bool:
        """Delete all transcription records for the current user AND the local
        history file. Returns True if anything was cleared. (Clearing only the
        remote left fetch_history falling back to the untouched local file, so
        'Clear' visibly did nothing.)"""
        local_ok = self._clear_local()
        if not self._enabled:
            return local_ok
        if not self._user_id or self._user_id == "local":
            print("[Supabase] Remote clear skipped: no authenticated user_id")
            return local_ok
        try:
            q = self._get_client().table(_TABLE).delete()
            q = q.eq("user_id", self._user_id)
            q.execute()
            print("[Supabase] History cleared.")
            return True
        except Exception as e:
            print(f"[Supabase] Clear history failed: {e}")
            return local_ok

    def _clear_local(self) -> bool:
        try:
            path = _local_history_path()
            with _local_history_lock:
                if os.path.exists(path):
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump([], f)
            return True
        except Exception as e:
            print(f"[LocalHistory] Clear failed: {e}")
            return False

    def _append_local(self, text: str, app_name: str = "",
                      app_exe: str = "") -> None:
        try:
            path = _local_history_path()
            with _local_history_lock:
                entries = []
                if os.path.exists(path):
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            entries = json.load(f)
                    except Exception:
                        entries = []
                entries.insert(0, {
                    "transcribed_text": text,
                    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "app_name": app_name,
                    "app_exe": app_exe,
                })
                entries = entries[:200]
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(entries, f, ensure_ascii=False)
        except Exception as e:
            print(f"[LocalHistory] Write failed: {e}")

    def _fetch_local(self, limit: int = 30) -> list:
        try:
            path = _local_history_path()
            if not os.path.exists(path):
                return []
            with _local_history_lock:
                with open(path, "r", encoding="utf-8") as f:
                    entries = json.load(f)
            return entries[:limit]
        except Exception as e:
            print(f"[LocalHistory] Read failed: {e}")
            return []

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
            # Remote table may not have the app columns yet — retry without
            # them rather than losing the whole record.
            stripped = {k: v for k, v in payload.items()
                        if k not in ("app_name", "app_exe")}
            if stripped != payload:
                try:
                    self._get_client().table(_TABLE).insert(stripped).execute()
                    print(f"[Supabase] Logged (no app cols): {list(stripped.keys())}")
                    return
                except Exception as e2:
                    e = e2
            print(f"[Supabase] Log failed (non-fatal): {e}")
