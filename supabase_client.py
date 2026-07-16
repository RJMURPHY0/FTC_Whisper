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


def _tombstones_path() -> str:
    return os.path.join(os.path.dirname(_local_history_path()),
                        "history-tombstones.json")


# Days a deleted transcription stays in Supabase before the actual remote
# delete happens (deletes are immediate in the UI, deferred remotely).
_TOMBSTONE_GRACE_DAYS = 30


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
        # Opportunistic upkeep: remote-delete tombstones past their 30-day grace.
        threading.Thread(target=self._purge_expired_tombstones,
                         daemon=True, name="tombstone-purge").start()

        # Remote history is strictly per-account. Without an authenticated
        # user an unfiltered query would return EVERY user's transcriptions —
        # so signed-out sessions read only the local file.
        if (not self._enabled or not self._user_id
                or self._user_id == "local"):
            return self._filter_tombstoned(self._fetch_local(limit))

        result: list = [None]
        error: list = [None]

        def _fetch() -> None:
            # Try with id + app columns first; fall back for remote tables
            # that don't have the newer columns yet.
            for cols in (
                "id, transcribed_text, refined_text, created_at, app_name, app_exe",
                "transcribed_text, refined_text, created_at, app_name, app_exe",
                "transcribed_text, refined_text, created_at",
            ):
                try:
                    q = (
                        self._get_client()
                        .table(_TABLE)
                        .select(cols)
                        .eq("user_id", self._user_id)
                        .order("created_at", desc=True)
                        .limit(limit)
                    )
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
            return self._filter_tombstoned(self._fetch_local(limit))
        if error[0]:
            print(f"[Supabase] Fetch history failed: {error[0]} — using local history")
            return self._filter_tombstoned(self._fetch_local(limit))
        if not result[0]:
            return self._filter_tombstoned(self._fetch_local(limit))
        return self._filter_tombstoned(self._enrich_from_local(result[0]))

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
        """Soft-clear: history disappears from the app immediately; the actual
        Supabase rows are deleted after a 30-day grace period (tombstone),
        so an accidental clear is recoverable server-side."""
        local_ok = self._clear_local()
        now = datetime.datetime.now(datetime.timezone.utc)
        stone = {
            "all_before": now.isoformat(),
            "purge_after": (now + datetime.timedelta(
                days=_TOMBSTONE_GRACE_DAYS)).isoformat(),
        }
        if self._user_id and self._user_id != "local":
            stone["user_id"] = self._user_id
        self._add_tombstone(stone)
        return local_ok or True

    def delete_transcription(self, item: dict) -> bool:
        """Soft-delete one transcription: removed from the app immediately,
        deleted from Supabase after the 30-day grace period."""
        text = item.get("transcribed_text") or ""
        created = item.get("created_at") or ""
        now = datetime.datetime.now(datetime.timezone.utc)
        stone = {
            "id": item.get("id"),
            "text": text,
            "created_at": created,
            "purge_after": (now + datetime.timedelta(
                days=_TOMBSTONE_GRACE_DAYS)).isoformat(),
        }
        if self._user_id and self._user_id != "local":
            stone["user_id"] = self._user_id
        self._add_tombstone(stone)
        # Remove from the local history file too.
        try:
            path = _local_history_path()
            with _local_history_lock:
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        entries = json.load(f)
                    entries = [e for e in entries
                               if not (e.get("transcribed_text") == text
                                       and e.get("created_at") == created)]
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(entries, f, ensure_ascii=False)
        except Exception as e:
            print(f"[LocalHistory] Delete failed: {e}")
        return True

    # ── Tombstones (deferred remote deletes) ──────────────────────────

    def _load_tombstones(self) -> list:
        try:
            path = _tombstones_path()
            if not os.path.exists(path):
                return []
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or []
        except Exception:
            return []

    def _save_tombstones(self, stones: list) -> None:
        try:
            with open(_tombstones_path(), "w", encoding="utf-8") as f:
                json.dump(stones, f, ensure_ascii=False)
        except Exception as e:
            print(f"[Tombstones] Save failed: {e}")

    def _add_tombstone(self, stone: dict) -> None:
        with _local_history_lock:
            stones = self._load_tombstones()
            stones.append(stone)
            self._save_tombstones(stones)

    def _filter_tombstoned(self, items: list) -> list:
        """Hide rows the user deleted (individually or via Clear) from any
        fetched result — remote rows survive up to 30 days after deletion."""
        stones = self._load_tombstones()
        # A tombstone only hides rows for the account that created it — a
        # Clear by one person must not blank another person's history on a
        # shared machine.
        uid = self._user_id if (self._user_id and self._user_id != "local") else None
        stones = [s for s in stones if s.get("user_id") == uid]
        if not stones or not items:
            return items

        def _dt(iso):
            try:
                return datetime.datetime.fromisoformat(
                    (iso or "").replace("Z", "+00:00"))
            except Exception:
                return None

        cutoffs = [_dt(s.get("all_before")) for s in stones if s.get("all_before")]
        cutoffs = [c for c in cutoffs if c]
        row_ids = {s.get("id") for s in stones if s.get("id") is not None}
        row_keys = {(s.get("text"), s.get("created_at"))
                    for s in stones if s.get("id") is None and s.get("text")}

        out = []
        for it in items:
            if it.get("id") is not None and it.get("id") in row_ids:
                continue
            if (it.get("transcribed_text"), it.get("created_at")) in row_keys:
                continue
            ts = _dt(it.get("created_at"))
            if ts and cutoffs and any(ts <= c for c in cutoffs):
                continue
            out.append(it)
        return out

    def _purge_expired_tombstones(self) -> None:
        """Execute remote deletes for tombstones past their grace period.
        Best-effort: failures keep the tombstone for the next attempt."""
        if getattr(self, "_purge_running", False):
            return
        self._purge_running = True
        try:
            now = datetime.datetime.now(datetime.timezone.utc)

            def _dt(iso):
                try:
                    return datetime.datetime.fromisoformat(
                        (iso or "").replace("Z", "+00:00"))
                except Exception:
                    return None

            stones = self._load_tombstones()
            if not stones:
                return
            keep = []
            changed = False
            for s in stones:
                pa = _dt(s.get("purge_after"))
                if pa is None or pa > now:
                    keep.append(s)
                    continue
                owner = s.get("user_id")
                if not owner:
                    changed = True  # local-only rows: nothing remote to delete
                    continue
                # Only the owning account's client can (and should) delete.
                if (not self._enabled or owner != self._user_id):
                    keep.append(s)
                    continue
                try:
                    q = self._get_client().table(_TABLE).delete().eq("user_id", owner)
                    if s.get("all_before"):
                        q = q.lte("created_at", s["all_before"])
                    elif s.get("id") is not None:
                        q = q.eq("id", s["id"])
                    else:
                        q = (q.eq("transcribed_text", s.get("text") or "")
                              .eq("created_at", s.get("created_at") or ""))
                    q.execute()
                    changed = True
                    print("[Supabase] Purged tombstoned history (30-day grace elapsed).")
                except Exception as e:
                    print(f"[Supabase] Tombstone purge failed (will retry): {e}")
                    keep.append(s)
            if changed:
                with _local_history_lock:
                    self._save_tombstones(keep)
        finally:
            self._purge_running = False

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
                record = {
                    "transcribed_text": text,
                    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "app_name": app_name,
                    "app_exe": app_exe,
                }
                # Tag with the signed-in user so history stays per-person even
                # when several accounts share this Windows machine.
                if self._user_id and self._user_id != "local":
                    record["user_id"] = self._user_id
                entries.insert(0, record)
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
            # Per-person: a signed-in user sees their own entries (plus legacy
            # untagged ones written before tagging existed); signed-out sees
            # only untagged local entries.
            if self._user_id and self._user_id != "local":
                entries = [e for e in entries
                           if e.get("user_id") in (self._user_id, None, "")]
            else:
                entries = [e for e in entries if not e.get("user_id")]
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
