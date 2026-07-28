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
from typing import Callable, Optional

# Table name in Supabase
_TABLE = "transcriptions"

_HISTORY_SELECT_SHAPES = (
    "id, transcribed_text, refined_text, created_at, app_name, app_exe",
    "transcribed_text, refined_text, created_at, app_name, app_exe",
    "transcribed_text, refined_text, created_at",
)
_CURRENT_USER = object()

_local_history_lock = threading.Lock()


def _is_missing_column_error(exc, *column_names: str) -> bool:
    """True only for a confirmed Postgres/PostgREST missing-column response."""
    code = str(getattr(exc, "code", "") or "")
    parts = [str(exc), str(getattr(exc, "message", "") or "")]
    text = " ".join(parts).casefold()
    missing = (
        code in {"42703", "PGRST204"}
        or ("column" in text and (
            "does not exist" in text or "schema cache" in text
        ))
    )
    if not missing:
        return False
    return not column_names or any(name.casefold() in text for name in column_names)


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
        # History is local-first: callers can paint this cache immediately while
        # a remote refresh runs. Every cache bucket is account-scoped so a slow
        # response from one sign-in can never leak into the next account.
        self._history_lock = threading.RLock()
        self._history_cache: dict[Optional[str], list] = {}
        self._history_listeners: set[Callable[[list], None]] = set()
        self._history_refreshing: set[str] = set()
        # Once a select shape succeeds, reuse it for the life of this client.
        # Legacy schemas otherwise cost two guaranteed 400 responses per click.
        self._history_select_cols: Optional[str] = None
        self._app_columns_supported: Optional[bool] = None

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def set_user(self, user_id: Optional[str]) -> None:
        """Set the authenticated user ID to include in all log entries."""
        self._user_id = user_id
        items = self.get_cached_history(200)
        with self._history_lock:
            listeners = tuple(self._history_listeners)
        for callback in listeners:
            try:
                callback([dict(r) for r in items])
            except Exception as exc:
                print(f"[History] Listener failed: {exc}")

    def set_client(self, client) -> None:
        """Share an already-authenticated Supabase client (bypasses RLS)."""
        self._client = client
        with self._history_lock:
            self._history_select_cols = None
            self._app_columns_supported = None

    def _get_client(self):
        if self._client is None:
            from supabase import create_client

            self._client = create_client(self._url, self._key)
        return self._client

    # ------------------------------------------------------------------
    # Public API — all fire-and-forget
    # ------------------------------------------------------------------

    def log_transcription(self, text: str, app_name: str = "",
                          app_exe: str = "", created_at: str = "") -> None:
        """Save a new transcription record (with the app it was injected into).
        The caller may mint created_at itself (app.py does, so the saved audio
        clip and the history row share one identity)."""
        owner = self._history_owner()
        # One timestamp is the durable local/remote identity. Previously the two
        # calls to now() differed, forcing fuzzy timestamp matching forever.
        created_at = created_at or datetime.datetime.now(
            datetime.timezone.utc).isoformat()
        record = self._append_local(
            text, app_name=app_name, app_exe=app_exe,
            created_at=created_at, user_id=owner,
        )
        self._remember_local_record(owner, record)
        if not self._enabled:
            return
        payload = {
            "transcribed_text": text,
            "created_at": created_at,
        }
        if app_name:
            payload["app_name"] = app_name
        if app_exe:
            payload["app_exe"] = app_exe
        if owner:
            payload["user_id"] = owner
        self._run(payload)

    def log_refinement(self, original: str, refined: str, mode: str,
                       app_name: str = "", app_exe: str = "") -> None:
        """Insert a refinement record."""
        if not self._enabled:
            return
        payload = {
            "transcribed_text": original,
            "refined_text": refined,
            "refinement_mode": mode,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if app_name:
            payload["app_name"] = app_name
        if app_exe:
            payload["app_exe"] = app_exe
        owner = self._history_owner()
        if owner:
            payload["user_id"] = owner
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

    def log_error_event(self, event_type: str, detail=None, app_name: str = "",
                        app_exe: str = "", window_class: str = "",
                        app_version: str = "",
                        transcription_created_at: str = "") -> None:
        """Fire-and-forget: record a client-side reliability failure to the
        error_events table — a transcription that never landed in the target
        app, a silent mic, an evidence-based mic switch, an empty result. This
        feeds the super-admin error log so failures across the fleet are
        visible instead of dying in a console nobody sees. Best-effort — a
        missing table, RLS block, or outage is swallowed.

        event_type ∈ {"inject_failed","inject_false_success","mic_silent",
                      "mic_switched","transcribe_empty"}.
        detail: dict → stored as jsonb; anything else → {"message": str}.
        transcription_created_at links the event to its transcriptions row.
        """
        if not self._enabled:
            return
        payload = {
            "event_type": event_type,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if isinstance(detail, dict):
            payload["detail"] = detail
        elif detail:
            payload["detail"] = {"message": str(detail)[:500]}
        if app_name:
            payload["app_name"] = app_name
        if app_exe:
            payload["app_exe"] = app_exe
        if window_class:
            payload["window_class"] = window_class
        if app_version:
            payload["app_version"] = app_version
        if transcription_created_at:
            payload["transcription_created_at"] = transcription_created_at
        if self._user_id and self._user_id != "local":
            payload["user_id"] = self._user_id

        def _insert():
            try:
                self._get_client().table("error_events").insert(payload).execute()
                print(f"[Supabase] error_event: {event_type}")
            except Exception as e:
                print(f"[Supabase] error_event log failed (non-fatal): {e}")

        threading.Thread(target=_insert, daemon=True,
                         name="supabase-error-log").start()

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

    def fetch_contact_vocab(self, limit: int = 500) -> list:
        """Contact and company names from the estate CRM (FTC Contacts shares
        this Supabase project) — used as speech-recognition hotwords so the
        names the user actually dictates are recognised. Best-effort: returns
        [] when signed out, offline, or the table/columns aren't there. RLS
        scopes rows to the signed-in user."""
        if not self._enabled or not self._user_id or self._user_id == "local":
            return []
        shapes = ("first_name,last_name,company",
                  "first_name,last_name,company_name",
                  "name,company",
                  "full_name,company")
        rows = []
        for cols in shapes:
            try:
                rows = (self._get_client().table("contacts").select(cols)
                        .limit(limit).execute().data or [])
                break
            except Exception as exc:
                if not _is_missing_column_error(exc):
                    return []
        terms = []
        seen = set()
        for r in rows:
            first = (r.get("first_name") or "").strip()
            last = (r.get("last_name") or "").strip()
            full = " ".join(p for p in (first, last) if p) \
                or (r.get("name") or r.get("full_name") or "").strip()
            company = (r.get("company") or r.get("company_name") or "").strip()
            for term in (full, company):
                t = " ".join(term.split())
                if len(t) >= 3 and t.lower() not in seen:
                    seen.add(t.lower())
                    terms.append(t)
        return terms[:150]

    def set_app_setting(self, key: str, value: str) -> None:
        """Fire-and-forget upsert into app_settings. RLS only grants writes to
        the super-admin account, so this is a silent no-op for everyone else."""
        if not self._enabled:
            return

        def _upsert():
            try:
                (self._get_client()
                 .table("app_settings")
                 .upsert({"key": key, "value": value})
                 .execute())
                print(f"[Supabase] app_setting saved: {key}")
            except Exception as e:
                print(f"[Supabase] set_app_setting({key!r}) failed (non-fatal): {e}")

        threading.Thread(target=_upsert, daemon=True,
                         name="app-setting-save").start()

    # ------------------------------------------------------------------
    # Local-first history cache / refresh listeners
    # ------------------------------------------------------------------

    def _history_owner(self, user_id=_CURRENT_USER) -> Optional[str]:
        value = self._user_id if user_id is _CURRENT_USER else user_id
        return value if value and value != "local" else None

    def add_history_listener(self, callback: Callable[[list], None], *,
                             replay: bool = False, limit: int = 100) -> None:
        """Subscribe to refreshed history snapshots.

        Callbacks run on the worker that changed the cache; tkinter consumers
        must marshal the snapshot through ``root.after``. ``replay`` emits the
        current local/in-memory snapshot immediately.
        """
        with self._history_lock:
            self._history_listeners.add(callback)
        if replay:
            callback(self.get_cached_history(limit))

    def remove_history_listener(self, callback: Callable[[list], None]) -> None:
        with self._history_lock:
            self._history_listeners.discard(callback)

    def get_cached_history(self, limit: int = 30) -> list:
        """Return an account-scoped local/in-memory snapshot without networking."""
        return self._cached_history_for_owner(self._history_owner(), limit)

    def _cached_history_for_owner(self, owner: Optional[str], limit: int) -> list:
        """Read disk once per account, then serve the maintained memory cache."""
        with self._history_lock:
            if owner in self._history_cache:
                return [dict(r) for r in self._history_cache[owner][:limit]]
        local = self._local_snapshot(owner, max(200, limit))
        self._store_history(owner, local, notify=False)
        return [dict(r) for r in local[:limit]]

    def fetch_history(self, limit: int = 30) -> list:
        """Return local/cached history immediately and refresh remote in flight.

        A listener receives the merged remote result. This keeps tab navigation
        independent of network latency while preserving remote-only records.
        """
        items = self.get_cached_history(limit)
        threading.Thread(target=self._purge_expired_tombstones,
                         daemon=True, name="tombstone-purge").start()
        self.refresh_history_async(limit=max(200, limit))
        return items

    def refresh_history_async(self, limit: int = 200) -> None:
        owner = self._history_owner()
        if not self._enabled or owner is None:
            return
        with self._history_lock:
            if owner in self._history_refreshing:
                return
            self._history_refreshing.add(owner)

        def _worker() -> None:
            try:
                self.refresh_history(limit=limit, user_id=owner)
            finally:
                with self._history_lock:
                    self._history_refreshing.discard(owner)

        threading.Thread(target=_worker, daemon=True,
                         name="history-refresh").start()

    def refresh_history(self, limit: int = 200, *, user_id=_CURRENT_USER) -> list:
        """Synchronously refresh one account; normally use ``fetch_history``."""
        owner = self._history_owner(user_id)
        if not self._enabled or owner is None:
            return self._cached_history_for_owner(owner, limit)

        remote, error = self._fetch_remote_history(owner, limit)
        if error is not None:
            print(f"[Supabase] Fetch history failed: {error} — keeping local cache")
            local = self._local_snapshot(owner, max(200, limit))
            with self._history_lock:
                cached = [dict(r) for r in self._history_cache.get(owner, [])]
            merged = self._merge_history(cached, local, max(200, limit))
            return [dict(r) for r in merged[:limit]]

        local = self._fetch_local(max(200, limit), user_id=owner)
        merged = self._filter_tombstoned(
            self._merge_history(remote, local, max(200, limit)), owner)
        self._store_history(owner, merged, notify=True)
        return [dict(r) for r in merged[:limit]]

    def _fetch_remote_history(self, owner: str, limit: int) -> tuple[list, object]:
        with self._history_lock:
            cached_cols = self._history_select_cols
        shapes = (cached_cols,) if cached_cols else _HISTORY_SELECT_SHAPES
        error = None
        for cols in shapes:
            try:
                q = (
                    self._get_client()
                    .table(_TABLE)
                    .select(cols)
                    .eq("user_id", owner)
                    .order("created_at", desc=True)
                    .limit(limit)
                )
                rows = q.execute().data or []
                with self._history_lock:
                    self._history_select_cols = cols
                    self._app_columns_supported = (
                        "app_name" in cols and "app_exe" in cols)
                return [dict(r) for r in rows], None
            except Exception as exc:
                error = exc
                # A connection/RLS/server failure is not schema evidence. Stop
                # instead of issuing more guaranteed-failing select shapes or
                # downgrading metadata capability for the rest of the process.
                if not _is_missing_column_error(exc):
                    return [], exc
        return [], error

    @staticmethod
    def _parse_history_time(record: dict):
        try:
            dt = datetime.datetime.fromisoformat(
                (record.get("created_at") or "").replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt
        except Exception:
            return None

    @classmethod
    def _same_history_event(cls, left: dict, right: dict) -> bool:
        """Conservative legacy identity: exact text and exact/near timestamp.

        New rows share the exact same timestamp. The ten-second tolerance exists
        only for already-shipped builds that called ``now()`` twice; no semantic
        or nearest-neighbour guessing is performed.
        """
        if left.get("transcribed_text") != right.get("transcribed_text"):
            return False
        lraw = left.get("created_at") or ""
        rraw = right.get("created_at") or ""
        if lraw and lraw == rraw:
            return True
        lt, rt = cls._parse_history_time(left), cls._parse_history_time(right)
        return bool(lt and rt and abs((lt - rt).total_seconds()) <= 10)

    def _enrich_from_local(self, remote: list, local: Optional[list] = None) -> list:
        """Fill each missing app field from an exact local event match only."""
        rows = [dict(r) for r in remote]
        if all(r.get("app_name") and r.get("app_exe") for r in rows):
            return rows
        if local is None:
            local = self._fetch_local(200)
        for row in rows:
            need_name = not row.get("app_name")
            need_exe = not row.get("app_exe")
            if not need_name and not need_exe:
                continue
            for candidate in local:
                if not self._same_history_event(row, candidate):
                    continue
                if need_name and candidate.get("app_name"):
                    row["app_name"] = candidate["app_name"]
                    need_name = False
                if need_exe and candidate.get("app_exe"):
                    row["app_exe"] = candidate["app_exe"]
                    need_exe = False
                if not need_name and not need_exe:
                    break
        return rows

    def _merge_history(self, remote: list, local: list, limit: int) -> list:
        rows = self._enrich_from_local(remote, local)
        used_local: set[int] = set()
        for row in rows:
            for index, candidate in enumerate(local):
                if index not in used_local and self._same_history_event(row, candidate):
                    used_local.add(index)
                    break
        rows.extend(dict(row) for index, row in enumerate(local)
                    if index not in used_local)

        def _sort_key(row):
            dt = self._parse_history_time(row)
            return dt.timestamp() if dt else float("-inf")

        rows.sort(key=_sort_key, reverse=True)
        return rows[:limit]

    def _local_snapshot(self, owner: Optional[str], limit: int) -> list:
        return self._filter_tombstoned(
            self._fetch_local(limit, user_id=owner), owner)

    def _store_history(self, owner: Optional[str], items: list, *,
                       notify: bool) -> None:
        snapshot = [dict(r) for r in items]
        with self._history_lock:
            changed = self._history_cache.get(owner) != snapshot
            self._history_cache[owner] = snapshot
            listeners = tuple(self._history_listeners) if changed and notify else ()
        # Never emit an old account's slow response into the current account UI.
        if owner != self._history_owner():
            return
        for callback in listeners:
            try:
                callback([dict(r) for r in snapshot])
            except Exception as exc:
                print(f"[History] Listener failed: {exc}")

    def _publish_history(self, owner: Optional[str], items: list) -> None:
        self._store_history(
            owner, self._filter_tombstoned(items, owner), notify=True)

    def _remember_local_record(self, owner: Optional[str], record: dict) -> None:
        with self._history_lock:
            initialized = owner in self._history_cache
            cached = [dict(r) for r in self._history_cache.get(owner, [])]
        # Once primed, the cache is maintained by log/delete/clear operations;
        # adding one record should not re-read and re-parse the whole JSON file.
        local = [record] if initialized else self._local_snapshot(owner, 200)
        merged = self._merge_history(cached, local, 200)
        self._publish_history(owner, merged)

    def clear_history(self) -> bool:
        """Soft-clear: history disappears from the app immediately; the actual
        Supabase rows are deleted after a 30-day grace period (tombstone),
        so an accidental clear is recoverable server-side."""
        owner = self._history_owner()
        # Stored audio is local-only and unrecoverable server-side, so drop
        # exactly this owner's clips (a shared machine keeps other accounts').
        try:
            import audio_store
            with self._history_lock:
                rows = [dict(r) for r in self._history_cache.get(owner, [])]
            for row in rows:
                audio_store.delete_for(row.get("created_at") or "")
        except Exception:
            pass
        local_ok = self._clear_local(owner)
        now = datetime.datetime.now(datetime.timezone.utc)
        stone = {
            "all_before": now.isoformat(),
            "purge_after": (now + datetime.timedelta(
                days=_TOMBSTONE_GRACE_DAYS)).isoformat(),
        }
        if owner:
            stone["user_id"] = owner
        self._add_tombstone(stone)
        self._publish_history(owner, [])
        return local_ok or True

    def delete_transcription(self, item: dict) -> bool:
        """Soft-delete one transcription: removed from the app immediately,
        deleted from Supabase after the 30-day grace period."""
        text = item.get("transcribed_text") or ""
        created = item.get("created_at") or ""
        try:
            import audio_store
            audio_store.delete_for(created)
        except Exception:
            pass
        now = datetime.datetime.now(datetime.timezone.utc)
        stone = {
            "id": item.get("id"),
            "text": text,
            "created_at": created,
            "purge_after": (now + datetime.timedelta(
                days=_TOMBSTONE_GRACE_DAYS)).isoformat(),
        }
        owner = self._history_owner()
        if owner:
            stone["user_id"] = owner
        self._add_tombstone(stone)
        # Remove from the local history file too.
        try:
            path = _local_history_path()
            with _local_history_lock:
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        entries = json.load(f)
                    entries = [
                        e for e in entries
                        if not (
                            ((e.get("user_id") == owner) if owner
                             else not e.get("user_id"))
                            and e.get("transcribed_text") == text
                            and e.get("created_at") == created
                        )
                    ]
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(entries, f, ensure_ascii=False)
        except Exception as e:
            print(f"[LocalHistory] Delete failed: {e}")
        with self._history_lock:
            cached = [dict(row) for row in self._history_cache.get(owner, [])]
        cached = [row for row in cached
                  if not ((item.get("id") is not None
                           and row.get("id") == item.get("id"))
                          or (row.get("transcribed_text") == text
                              and row.get("created_at") == created))]
        self._publish_history(owner, cached)
        return True

    def update_transcription(self, item: dict, new_text: str) -> bool:
        """Replace one row's text (history "Retry transcription"). created_at
        is deliberately untouched — it is the row's identity and the key of
        its stored audio clip. Remote update is fire-and-forget."""
        old_text = item.get("transcribed_text") or ""
        created = item.get("created_at") or ""
        row_id = item.get("id")
        owner = self._history_owner()
        if not new_text or (new_text == old_text and not item.get("refined_text")):
            return False

        def _matches(row: dict) -> bool:
            if row_id is not None and row.get("id") == row_id:
                return True
            return (row.get("transcribed_text") == old_text
                    and row.get("created_at") == created)

        # Local file
        try:
            path = _local_history_path()
            with _local_history_lock:
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        entries = json.load(f) or []
                    for e in entries:
                        same_owner = ((e.get("user_id") == owner) if owner
                                      else not e.get("user_id"))
                        if same_owner and _matches(e):
                            e["transcribed_text"] = new_text
                            e.pop("refined_text", None)
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(entries, f, ensure_ascii=False)
        except Exception as e:
            print(f"[LocalHistory] Update failed: {e}")

        # In-memory cache + listeners (UI re-renders off this publish)
        with self._history_lock:
            cached = [dict(r) for r in self._history_cache.get(owner, [])]
        for row in cached:
            if _matches(row):
                row["transcribed_text"] = new_text
                row.pop("refined_text", None)
        self._publish_history(owner, cached)

        # Remote
        if self._enabled and owner:
            def _update():
                try:
                    q = (self._get_client().table(_TABLE)
                         .update({"transcribed_text": new_text,
                                  "refined_text": None})
                         .eq("user_id", owner))
                    if row_id is not None:
                        q = q.eq("id", row_id)
                    else:
                        q = (q.eq("created_at", created)
                              .eq("transcribed_text", old_text))
                    q.execute()
                except Exception as e:
                    print(f"[Supabase] Update failed (non-fatal): {e}")

            threading.Thread(target=_update, daemon=True,
                             name="supabase-history-update").start()
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

    def _filter_tombstoned(self, items: list, user_id=_CURRENT_USER) -> list:
        """Hide rows the user deleted (individually or via Clear) from any
        fetched result — remote rows survive up to 30 days after deletion."""
        stones = self._load_tombstones()
        # A tombstone only hides rows for the account that created it — a
        # Clear by one person must not blank another person's history on a
        # shared machine.
        uid = self._history_owner(user_id)
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

    def _clear_local(self, owner: Optional[str]) -> bool:
        try:
            path = _local_history_path()
            with _local_history_lock:
                if os.path.exists(path):
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            entries = json.load(f) or []
                    except Exception:
                        entries = []
                    if owner:
                        entries = [e for e in entries
                                   if e.get("user_id") != owner]
                    else:
                        entries = [e for e in entries if e.get("user_id")]
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(entries, f, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"[LocalHistory] Clear failed: {e}")
            return False

    def _append_local(self, text: str, app_name: str = "",
                      app_exe: str = "", created_at: str = "",
                      user_id=_CURRENT_USER) -> dict:
        owner = self._history_owner(user_id)
        record = {
            "transcribed_text": text,
            "created_at": created_at or datetime.datetime.now(
                datetime.timezone.utc).isoformat(),
            "app_name": app_name,
            "app_exe": app_exe,
        }
        if owner:
            record["user_id"] = owner
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
                entries.insert(0, record)
                entries = entries[:200]
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(entries, f, ensure_ascii=False)
        except Exception as e:
            print(f"[LocalHistory] Write failed: {e}")
        return record

    def _fetch_local(self, limit: int = 30, user_id=_CURRENT_USER) -> list:
        try:
            path = _local_history_path()
            if not os.path.exists(path):
                return []
            with _local_history_lock:
                with open(path, "r", encoding="utf-8") as f:
                    entries = json.load(f)
            # Strict account separation: authenticated users see only their own
            # tagged rows. Untagged legacy rows remain available only offline;
            # assigning them to an arbitrary account would be a privacy leak on
            # a shared Windows profile.
            owner = self._history_owner(user_id)
            if owner:
                entries = [e for e in entries if e.get("user_id") == owner]
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
        has_app_fields = "app_name" in payload or "app_exe" in payload
        with self._history_lock:
            app_columns_supported = self._app_columns_supported
        if has_app_fields and app_columns_supported is False:
            payload = {k: v for k, v in payload.items()
                       if k not in ("app_name", "app_exe")}
            has_app_fields = False
        try:
            self._get_client().table(_TABLE).insert(payload).execute()
            print(f"[Supabase] Logged: {list(payload.keys())}")
            if has_app_fields:
                with self._history_lock:
                    self._app_columns_supported = True
        except Exception as e:
            # A legacy table may lack the app columns; preserve the record when
            # the server explicitly reports that schema, but not for outages.
            stripped = {k: v for k, v in payload.items()
                        if k not in ("app_name", "app_exe")}
            # Only a confirmed legacy-schema response warrants a metadata-free
            # retry. A timeout/5xx/RLS error must not disable app fields forever.
            if (stripped != payload
                    and _is_missing_column_error(e, "app_name", "app_exe")):
                try:
                    self._get_client().table(_TABLE).insert(stripped).execute()
                    with self._history_lock:
                        self._app_columns_supported = False
                    print(f"[Supabase] Logged (no app cols): {list(stripped.keys())}")
                    return
                except Exception as e2:
                    e = e2
            print(f"[Supabase] Log failed (non-fatal): {e}")
