"""
Per-account dictation impact stats for FTC Whisper.

Powers the Home tab "Your impact" cards: time saved, day streak and
today's word count. Local-first — every dictation lands in a per-user
daily aggregate in %APPDATA%\\FTC Whisper\\stats.json synchronously, so
the cards update instantly and work fully offline. When signed in, the
same aggregates sync to the `user_daily_stats` table (one tiny row per
user per day, RLS-scoped to the account) so the numbers follow the
account across machines. Every remote call is fire-and-forget and
user-scoped with a hard row limit — a Supabase outage, missing table or
huge history can never block the app or fan out into a big query.
"""

import datetime
import json
import os
import threading

# Reading speed assumptions for the impact maths. Dictation speed is the
# product-wide constant shown on the card; typing speed is the average the
# "time saved" comparison is made against.
DICTATION_WPM = 160
TYPING_WPM = 40

_STATS_TABLE = "user_daily_stats"
# Bounded remote reads: ~13 months of daily rows covers every card.
_PULL_DAYS_LIMIT = 400
# One-time backfill from the transcriptions log (rows, not days).
_SEED_ROWS_LIMIT = 500
# Cap stored days per user; older days collapse into carry totals so the
# lifetime "time saved" number never loses accuracy.
_MAX_DAYS_KEPT = 1100


def _stats_path() -> str:
    app_data = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(app_data, "FTC Whisper")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "stats.json")


def _today() -> datetime.date:
    return datetime.datetime.now().date()


def _utc_iso_to_local_date(iso: str):
    try:
        dt = datetime.datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone().date()
    except Exception:
        return None


def _saved_minutes(words: int, audio_seconds: float) -> float:
    """Minutes saved vs typing the same words. Uses the real dictation
    duration when known; falls back to the nominal dictation speed for
    seeded rows that predate duration capture. Floored at zero so a mic
    left running never shows negative savings."""
    if words <= 0:
        return 0.0
    typing_min = words / float(TYPING_WPM)
    if audio_seconds and audio_seconds > 0:
        dictating_min = audio_seconds / 60.0
    else:
        dictating_min = words / float(DICTATION_WPM)
    return max(0.0, typing_min - dictating_min)


class StatsStore:
    """Thread-safe per-user dictation aggregates.

    Listeners are called (on the calling/background thread) after every
    change — UI code must marshal to the tkinter thread itself.
    """

    def __init__(self, db=None):
        self._db = db                  # SupabaseLogger (optional)
        self._lock = threading.Lock()
        self._data = None              # lazy-loaded file contents
        self._user_key = "local"
        self._listeners = []
        self._push_timer = None
        self._sync_running = set()     # user ids with a sync thread in flight

    # ── Public API ────────────────────────────────────────────────────

    def add_listener(self, fn) -> None:
        self._listeners.append(fn)

    def set_user(self, user_id) -> None:
        """Switch the active account (None/'local' = signed out)."""
        key = user_id if (user_id and user_id != "local") else "local"
        changed = key != self._user_key
        self._user_key = key
        if changed:
            self._notify()
        if key != "local":
            self._start_sync(key)

    def record_dictation(self, words: int, audio_seconds: float) -> None:
        """Add one finished dictation to today's bucket. Synchronous local
        write (instant UI); remote push is debounced + fire-and-forget."""
        if words <= 0:
            return
        day = _today().isoformat()
        with self._lock:
            u = self._user_blob()
            rec = u["days"].setdefault(day, {"w": 0, "s": 0.0})
            rec["w"] = int(rec.get("w", 0)) + int(words)
            rec["s"] = float(rec.get("s", 0.0)) + max(0.0, float(audio_seconds or 0.0))
            self._trim_locked(u)
            self._save_locked()
        self._notify()
        self._schedule_push()

    def snapshot(self) -> dict:
        """Current metrics for the signed-in (or local) account."""
        with self._lock:
            u = self._user_blob()
            days = dict(u["days"])
            carry_saved = float(u.get("carry_saved_min", 0.0))

        today = _today()
        today_words = int(days.get(today.isoformat(), {}).get("w", 0))

        saved_min = carry_saved
        for rec in days.values():
            saved_min += _saved_minutes(int(rec.get("w", 0)), float(rec.get("s", 0.0)))

        # Streak: consecutive days with at least one dictation, ending today —
        # or ending yesterday (streak alive, but needs a dictation today).
        active_today = today_words > 0
        d = today if active_today else today - datetime.timedelta(days=1)
        streak = 0
        while streak < 3650 and int(days.get(d.isoformat(), {}).get("w", 0)) > 0:
            streak += 1
            d -= datetime.timedelta(days=1)

        return {
            "today_words": today_words,
            "streak_days": streak,
            "streak_active_today": active_today,
            "saved_minutes": saved_min,
        }

    # ── Local persistence ─────────────────────────────────────────────

    def _load_locked(self) -> None:
        if self._data is not None:
            return
        try:
            with open(_stats_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
                raise ValueError("bad shape")
            self._data = data
        except Exception:
            self._data = {"version": 1, "users": {}}

    def _user_blob(self) -> dict:
        """Caller must hold self._lock."""
        self._load_locked()
        u = self._data["users"].setdefault(self._user_key, {})
        u.setdefault("days", {})
        u.setdefault("seeded", False)
        u.setdefault("carry_saved_min", 0.0)
        return u

    def _trim_locked(self, u: dict) -> None:
        days = u["days"]
        if len(days) <= _MAX_DAYS_KEPT:
            return
        for day in sorted(days.keys())[: len(days) - _MAX_DAYS_KEPT]:
            rec = days.pop(day)
            u["carry_saved_min"] = float(u.get("carry_saved_min", 0.0)) + _saved_minutes(
                int(rec.get("w", 0)), float(rec.get("s", 0.0)))

    def _save_locked(self) -> None:
        try:
            path = _stats_path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:
            print(f"[Stats] Save failed: {e}")

    def _notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception as e:
                print(f"[Stats] Listener error: {e}")

    # ── Remote sync (all best-effort, never blocks the app) ───────────

    @staticmethod
    def _execute_timeout(query, seconds: float):
        """Run query.execute() with a hard timeout. Supabase calls can hang
        far past any useful window on a bad connection (same reason
        fetch_history join()s its worker) — a wedged read must not pin the
        sync thread for minutes. Returns row data or None on timeout/error."""
        result = [None]

        def _run():
            try:
                result[0] = query.execute().data or []
            except Exception as e:
                print(f"[Stats] Query failed: {e}")

        t = threading.Thread(target=_run, daemon=True, name="stats-query")
        t.start()
        t.join(timeout=seconds)
        if t.is_alive():
            print("[Stats] Query timed out.")
            return None
        return result[0]

    def _client_or_none(self):
        db = self._db
        try:
            if db is None or not db.is_enabled:
                return None
            return db._get_client()
        except Exception:
            return None

    def _schedule_push(self) -> None:
        """Debounce: one upsert of today's row ~2s after the last dictation."""
        if self._user_key == "local":
            return
        uid, day = self._user_key, _today().isoformat()
        t = self._push_timer
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass
        t = threading.Timer(2.0, self._push_day, args=(uid, day))
        t.daemon = True
        self._push_timer = t
        t.start()

    def _push_day(self, uid: str, day: str) -> None:
        client = self._client_or_none()
        if client is None:
            return
        with self._lock:
            u = self._data["users"].get(uid) if self._data else None
            rec = dict((u or {}).get("days", {}).get(day) or {})
        if not rec:
            return
        try:
            client.table(_STATS_TABLE).upsert({
                "user_id": uid,
                "day": day,
                "words": int(rec.get("w", 0)),
                "audio_seconds": round(float(rec.get("s", 0.0)), 2),
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }, on_conflict="user_id,day").execute()
        except Exception as e:
            print(f"[Stats] Push failed (non-fatal): {e}")

    def _start_sync(self, uid: str) -> None:
        if uid in self._sync_running:
            return
        self._sync_running.add(uid)
        threading.Thread(target=self._sync_remote, args=(uid,),
                         daemon=True, name="stats-sync").start()

    def _sync_remote(self, uid: str) -> None:
        """Pull the account's daily rows, backfill once from the
        transcriptions log, and heal the remote with any local days it is
        missing. Merge rule is per-day max — days represent the same
        dictations wherever they were counted, so max never double-counts
        and multiple devices converge."""
        try:
            client = self._client_or_none()
            if client is None:
                return

            remote = {}
            table_ok = False
            rows = self._execute_timeout(
                client.table(_STATS_TABLE)
                .select("day,words,audio_seconds")
                .eq("user_id", uid)
                .order("day", desc=True)
                .limit(_PULL_DAYS_LIMIT), 15.0)
            if rows is not None:
                table_ok = True
                for r in rows:
                    day = str(r.get("day") or "")[:10]
                    if day:
                        remote[day] = {"w": int(r.get("words") or 0),
                                       "s": float(r.get("audio_seconds") or 0.0)}
            else:
                print("[Stats] Pull unavailable (table missing or offline).")

            with self._lock:
                self._load_locked()
                u = self._data["users"].setdefault(uid, {})
                u.setdefault("days", {})
                u.setdefault("seeded", False)
                u.setdefault("carry_saved_min", 0.0)
                seeded = bool(u["seeded"])

            seed = None
            if not seeded:
                seed = self._seed_from_transcriptions(client, uid)

            changed = False
            with self._lock:
                u = self._data["users"][uid]
                days = u["days"]
                for src in (remote, seed or {}):
                    for day, rec in src.items():
                        cur = days.get(day)
                        if cur is None:
                            days[day] = dict(rec)
                            changed = True
                        else:
                            if rec["w"] > int(cur.get("w", 0)):
                                cur["w"] = rec["w"]
                                changed = True
                            if rec["s"] > float(cur.get("s", 0.0)):
                                cur["s"] = rec["s"]
                                changed = True
                if seed is not None and not u["seeded"]:
                    u["seeded"] = True
                    changed = True
                self._trim_locked(u)
                if changed:
                    self._save_locked()
                # Days the remote is missing or behind on (bounded).
                to_push = []
                if table_ok:
                    for day in sorted(days.keys(), reverse=True)[:60]:
                        rec = days[day]
                        rem = remote.get(day)
                        if (rem is None or int(rec.get("w", 0)) > rem["w"]
                                or float(rec.get("s", 0.0)) > rem["s"]):
                            to_push.append({
                                "user_id": uid,
                                "day": day,
                                "words": int(rec.get("w", 0)),
                                "audio_seconds": round(float(rec.get("s", 0.0)), 2),
                                "updated_at": datetime.datetime.now(
                                    datetime.timezone.utc).isoformat(),
                            })

            if changed:
                self._notify()
            if to_push:
                try:
                    client.table(_STATS_TABLE).upsert(
                        to_push, on_conflict="user_id,day").execute()
                    print(f"[Stats] Synced {len(to_push)} day(s) to Supabase.")
                except Exception as e:
                    print(f"[Stats] Sync push failed (non-fatal): {e}")
        except Exception as e:
            print(f"[Stats] Sync error (non-fatal): {e}")
        finally:
            self._sync_running.discard(uid)

    def _seed_from_transcriptions(self, client, uid: str):
        """One-time backfill: bucket the account's recent transcription log
        into daily word counts so streak/today start accurate on first run.
        Refinement rows are not dictations and are skipped. Bounded to the
        newest _SEED_ROWS_LIMIT rows — user-scoped and cheap at any scale.
        Returns None when every query attempt failed (retry next launch);
        a dict (possibly empty) marks seeding done."""
        for cols in ("created_at,transcribed_text,refined_text,refinement_mode",
                     "created_at,transcribed_text"):
            rows = self._execute_timeout(
                client.table("transcriptions")
                .select(cols)
                .eq("user_id", uid)
                .order("created_at", desc=True)
                .limit(_SEED_ROWS_LIMIT), 30.0)
            if rows is None:
                continue
            seed = {}
            for r in rows:
                if r.get("refined_text") or r.get("refinement_mode"):
                    continue
                day = _utc_iso_to_local_date(r.get("created_at") or "")
                text = r.get("transcribed_text") or ""
                if day is None or not text:
                    continue
                rec = seed.setdefault(day.isoformat(), {"w": 0, "s": 0.0})
                rec["w"] += len(text.split())
            print(f"[Stats] Seeded {len(seed)} day(s) from transcription history.")
            return seed
        return None
