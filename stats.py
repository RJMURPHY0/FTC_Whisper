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
# Columns added after the table shipped — stripped from the payload when the
# remote table predates them (see _push_day).
_REFINE_COLS = ("refines", "refine_seconds", "refine_prompt_words",
                "refine_voice_prompts")
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


# ---------------------------------------------------------------------------
# AI refine — what the same edit costs by hand
# ---------------------------------------------------------------------------
# The comparison is the round trip a person actually does without this app:
# switch to the browser, paste the text into a chat assistant, type what they
# want changed, wait, copy the answer back, select the old text and paste over
# it. Every step below is a separate, defensible number rather than one made-up
# total, so the breakdown panel can show its working.
#
# Deliberately conservative: each figure is what an experienced user does on a
# good day, and the app's own cost is MEASURED (press to applied), not assumed.
REFINE_SWITCH_TO_BROWSER_S = 4.0    # alt-tab / find the tab / new tab
REFINE_COPY_SOURCE_S       = 2.5    # select the text, Ctrl+C
REFINE_PASTE_INTO_CHAT_S   = 2.0    # click the box, Ctrl+V
REFINE_MODEL_WAIT_S        = 6.0    # a web chat answering a paragraph
REFINE_COPY_RESULT_S       = 3.0    # select the reply (no copy button on a partial), Ctrl+C
REFINE_RETURN_AND_PASTE_S  = 4.5    # switch back, reselect the old text, paste over it
# Typed instruction length when the user pressed a preset button (Fix All /
# Email) instead of writing one — by hand they would still have had to say what
# they wanted. Eight words is a short, realistic instruction.
REFINE_DEFAULT_PROMPT_WORDS = 8


def refine_saved_seconds(elapsed_seconds: float, prompt_words: int = 0,
                         prompt_spoken: bool = False) -> float:
    """Seconds saved by one APPLIED refinement (inserted or replaced).

    manual = the full copy-to-a-chat-assistant round trip, including typing the
    instruction at TYPING_WPM; app = what it actually took, measured. Floored at
    zero so a refine the user sat on for five minutes never reads as negative."""
    words = int(prompt_words or 0)
    if words <= 0:
        words = REFINE_DEFAULT_PROMPT_WORDS
    typing_prompt_s = (words / float(TYPING_WPM)) * 60.0
    manual = (REFINE_SWITCH_TO_BROWSER_S + REFINE_COPY_SOURCE_S
              + REFINE_PASTE_INTO_CHAT_S + typing_prompt_s
              + REFINE_MODEL_WAIT_S + REFINE_COPY_RESULT_S
              + REFINE_RETURN_AND_PASTE_S)
    return max(0.0, manual - max(0.0, float(elapsed_seconds or 0.0)))


def refine_manual_seconds(prompt_words: int = 0) -> float:
    """The manual round-trip total alone — used by the breakdown panel."""
    words = int(prompt_words or 0)
    if words <= 0:
        words = REFINE_DEFAULT_PROMPT_WORDS
    return (REFINE_SWITCH_TO_BROWSER_S + REFINE_COPY_SOURCE_S
            + REFINE_PASTE_INTO_CHAT_S + (words / float(TYPING_WPM)) * 60.0
            + REFINE_MODEL_WAIT_S + REFINE_COPY_RESULT_S
            + REFINE_RETURN_AND_PASTE_S)


def _refine_saved_minutes(count: int, elapsed_seconds: float,
                          prompt_words: int) -> float:
    """Minutes saved by a day's applied refinements, from the day's aggregates.

    refine_saved_seconds is linear in both inputs, so summing per refinement and
    summing the aggregates give the same answer: n × the fixed round-trip steps,
    plus the typing time for all the instruction words, minus the measured in-app
    total. Floored per day."""
    n = int(count or 0)
    if n <= 0:
        return 0.0
    words = int(prompt_words or 0)
    if words <= 0:
        words = n * REFINE_DEFAULT_PROMPT_WORDS
    fixed = (REFINE_SWITCH_TO_BROWSER_S + REFINE_COPY_SOURCE_S
             + REFINE_PASTE_INTO_CHAT_S + REFINE_MODEL_WAIT_S
             + REFINE_COPY_RESULT_S + REFINE_RETURN_AND_PASTE_S)
    manual = n * fixed + (words / float(TYPING_WPM)) * 60.0
    return max(0.0, manual - max(0.0, float(elapsed_seconds or 0.0))) / 60.0


def _is_weekend(d: datetime.date) -> bool:
    return d.weekday() >= 5  # Mon=0 … Sat=5, Sun=6


def _best_streak(active_isos) -> int:
    """Longest run ever, under the same rule as the live streak: weekdays are
    required, weekend days bridge a gap without breaking it and count when
    used. Single forward pass over the recorded days."""
    dates = []
    for iso in active_isos or ():
        try:
            dates.append(datetime.date.fromisoformat(iso))
        except Exception:
            continue
    if not dates:
        return 0
    dates.sort()
    used = set(dates)
    one = datetime.timedelta(days=1)
    best = run = 0
    d = dates[0]
    end = dates[-1]
    guard = 0
    while d <= end and guard < 3660 * 2:
        if d in used:
            run += 1
            best = max(best, run)
        elif not _is_weekend(d):
            run = 0
        d += one
        guard += 1
    return best


def _compute_streak(used, today: datetime.date) -> int:
    """Weekend-aware streak. `used` is a callable(date)->bool (did the user
    dictate that day). Weekdays are required; weekend days bridge (a missed
    Sat/Sun never breaks the run, a used one counts). Today and any trailing
    weekend that hasn't been used yet are a grace period, not a break.
    Returns the number of used days in the current unbroken run."""
    one = datetime.timedelta(days=1)
    d = today
    # Phase 1 — walk back to the most recent used day, skipping excused
    # (today, or weekend) non-used days. A *past weekday* with no use ends it.
    while not used(d):
        if d == today or _is_weekend(d):
            d -= one
            continue
        return 0
    # Phase 2 — count the unbroken run backwards, bridging weekend gaps.
    streak = 0
    guard = 0
    while (used(d) or _is_weekend(d)) and guard < 3660:
        if used(d):
            streak += 1
        d -= one
        guard += 1
    return streak


def _words_by_range(days: dict, carry_words: int, today: datetime.date) -> dict:
    wk_start = today - datetime.timedelta(days=today.weekday())  # Monday
    mo_start = today.replace(day=1)
    yr_start = today.replace(month=1, day=1)
    totals = {"today": 0, "week": 0, "month": 0, "year": 0, "all": int(carry_words)}
    for iso, rec in days.items():
        try:
            d = datetime.date.fromisoformat(iso)
        except Exception:
            continue
        w = int(rec.get("w", 0))
        totals["all"] += w
        if d > today:
            continue  # never count a future-dated row in the windows
        if d == today:
            totals["today"] += w
        if d >= wk_start:
            totals["week"] += w
        if d >= mo_start:
            totals["month"] += w
        if d >= yr_start:
            totals["year"] += w
    return totals


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

    def record_dictation(self, words: int, audio_seconds: float,
                         voiced_seconds: float = 0.0) -> None:
        """Add one finished dictation to today's bucket. Synchronous local
        write (instant UI); remote push is debounced + fire-and-forget.

        voiced_seconds is the speech-only portion of the recording (energy
        above the mic's noise floor) — it feeds the real words-per-minute
        stat. Dictations whose implied speed falls outside plausible human
        speech are excluded from the wpm aggregate (a mic left running or a
        misfiring gate must not poison the average) but still count words."""
        if words <= 0:
            return
        v = max(0.0, float(voiced_seconds or 0.0))
        v_wpm = (words / (v / 60.0)) if v >= 1.0 else 0.0
        plausible = 20.0 <= v_wpm <= 350.0
        day = _today().isoformat()
        with self._lock:
            u = self._user_blob()
            rec = u["days"].setdefault(day, {"w": 0, "s": 0.0})
            rec["w"] = int(rec.get("w", 0)) + int(words)
            rec["s"] = float(rec.get("s", 0.0)) + max(0.0, float(audio_seconds or 0.0))
            if plausible:
                rec["v"] = round(float(rec.get("v", 0.0)) + v, 2)
                rec["vw"] = int(rec.get("vw", 0)) + int(words)
            self._trim_locked(u)
            self._save_locked()
        self._notify()
        self._schedule_push()

    def record_refine(self, elapsed_seconds: float, prompt_words: int = 0,
                      prompt_spoken: bool = False) -> None:
        """Add one APPLIED refinement (the user inserted or replaced with the
        result) to today's bucket. A refinement that was generated and then
        discarded saved nothing and is never recorded.

        Stores the count, the measured in-app seconds and the instruction word
        count, so the saving can be recomputed from raw numbers rather than
        being frozen at whatever the model said on the day."""
        day = _today().isoformat()
        words = int(prompt_words or 0)
        if words <= 0:
            words = REFINE_DEFAULT_PROMPT_WORDS
        with self._lock:
            u = self._user_blob()
            rec = u["days"].setdefault(day, {"w": 0, "s": 0.0})
            rec["r"] = int(rec.get("r", 0)) + 1
            rec["rs"] = round(float(rec.get("rs", 0.0))
                              + max(0.0, float(elapsed_seconds or 0.0)), 2)
            rec["rp"] = int(rec.get("rp", 0)) + words
            if prompt_spoken:
                rec["rv"] = int(rec.get("rv", 0)) + 1
            self._trim_locked(u)
            self._save_locked()
        self._notify()
        self._schedule_push()

    def snapshot(self) -> dict:
        """Current metrics for the signed-in (or local) account."""
        with self._lock:
            u = self._user_blob()
            days = dict(u["days"])
            carry_words = int(u.get("carry_words", 0))

        today = _today()
        today_words = int(days.get(today.isoformat(), {}).get("w", 0))

        voiced_s = 0.0
        voiced_w = 0
        total_words = carry_words
        total_audio_s = 0.0
        refine_n = 0
        refine_s = 0.0
        refine_pw = 0
        refine_voice_n = 0
        refine_min = 0.0
        for rec in days.values():
            voiced_s += float(rec.get("v", 0.0))
            voiced_w += int(rec.get("vw", 0))
            total_words += int(rec.get("w", 0))
            total_audio_s += float(rec.get("s", 0.0))
            n = int(rec.get("r", 0))
            if n:
                refine_n += n
                refine_s += float(rec.get("rs", 0.0))
                refine_pw += int(rec.get("rp", 0))
                refine_voice_n += int(rec.get("rv", 0))
                refine_min += _refine_saved_minutes(
                    n, float(rec.get("rs", 0.0)), int(rec.get("rp", 0)))

        # Real dictation speed: words per minute of actual speech. Shown only
        # once there's enough data to be a measurement rather than noise;
        # before that the maths uses the nominal 160 default.
        avg_wpm = 0
        if voiced_w >= 50 and voiced_s >= 30.0:
            avg_wpm = int(round(voiced_w / (voiced_s / 60.0)))

        # Time saved is a like-for-like speech-vs-typing comparison: both are
        # pure production rates with thinking pauses left OUT. The typing figure
        # (40 wpm) is a sustained rate that already excludes thinking, so
        # charging dictation its full wall-clock — every pause counted against
        # it — would be an asymmetric, pessimistic comparison. Dictation time is
        # therefore modelled from the user's own measured speech rate (nominal
        # 160 until it unlocks), applied to every word. This yields ONE multiple,
        # effective_wpm / TYPING_WPM, that holds across every surface: the speed
        # panel, the headline, and the time panel's "time you spent" all agree,
        # with no 5.6×-vs-3.4× reconciliation. Floored at zero so a rate slower
        # than typing (or a mic left running) never shows negative savings.
        effective_wpm = avg_wpm if avg_wpm > 0 else DICTATION_WPM
        dictation_time_min = (total_words / float(effective_wpm)
                              if total_words > 0 else 0.0)
        typing_all_min = (total_words / float(TYPING_WPM)
                          if total_words > 0 else 0.0)
        dictation_min = max(0.0, typing_all_min - dictation_time_min)
        saved_min = dictation_min + refine_min

        # Streak: weekend-aware. Weekdays are required; Saturday/Sunday bridge
        # the streak (missing one never breaks it, using one counts). Today —
        # and any trailing weekend not yet used — is a grace period, not a
        # break, matching the old "streak alive if it ended yesterday" rule.
        def _used(dd, _days=days):
            return int(_days.get(dd.isoformat(), {}).get("w", 0)) > 0
        active_today = today_words > 0
        streak = _compute_streak(_used, today)
        # True when the streak is being held safe over a weekend the user
        # hasn't needed to use yet — drives a reassuring sub-label instead
        # of a warning.
        streak_weekend_grace = (_is_weekend(today) and not active_today and streak > 0)

        active_days = sorted(iso for iso, rec in days.items()
                             if int(rec.get("w", 0)) > 0)

        return {
            "today_words": today_words,
            "streak_days": streak,
            "streak_active_today": active_today,
            "streak_weekend_grace": streak_weekend_grace,
            "saved_minutes": saved_min,
            "avg_wpm": avg_wpm,
            "words": _words_by_range(days, carry_words, today),
            # ── breakdown data (drives the click-through panels) ──────────
            "dictation_saved_minutes": dictation_min,
            "dictation_time_minutes": dictation_time_min,
            "effective_wpm": effective_wpm,
            "refine_saved_minutes": refine_min,
            "refine_count": refine_n,
            "refine_seconds": refine_s,
            "refine_prompt_words": refine_pw,
            "refine_voice_count": refine_voice_n,
            "voiced_seconds": voiced_s,
            "voiced_words": voiced_w,
            "total_words": total_words,
            "total_audio_seconds": total_audio_s,
            "carry_words": carry_words,
            "active_days": active_days,
            "best_streak": _best_streak(active_days),
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
        u.setdefault("carry_words", 0)
        return u

    def _trim_locked(self, u: dict) -> None:
        days = u["days"]
        if len(days) <= _MAX_DAYS_KEPT:
            return
        for day in sorted(days.keys())[: len(days) - _MAX_DAYS_KEPT]:
            rec = days.pop(day)
            u["carry_saved_min"] = float(u.get("carry_saved_min", 0.0)) + _saved_minutes(
                int(rec.get("w", 0)), float(rec.get("s", 0.0)))
            u["carry_words"] = int(u.get("carry_words", 0)) + int(rec.get("w", 0))

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
        payload = {
            "user_id": uid,
            "day": day,
            "words": int(rec.get("w", 0)),
            "audio_seconds": round(float(rec.get("s", 0.0)), 2),
            "voiced_seconds": round(float(rec.get("v", 0.0)), 2),
            "voiced_words": int(rec.get("vw", 0)),
            "refines": int(rec.get("r", 0)),
            "refine_seconds": round(float(rec.get("rs", 0.0)), 2),
            "refine_prompt_words": int(rec.get("rp", 0)),
            "refine_voice_prompts": int(rec.get("rv", 0)),
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        # Degrade one column group at a time so an older table still syncs
        # everything it does have (refine columns landed after the voiced ones).
        for drop in ((),
                     _REFINE_COLS,
                     _REFINE_COLS + ("voiced_seconds", "voiced_words")):
            for k in drop:
                payload.pop(k, None)
            try:
                client.table(_STATS_TABLE).upsert(
                    payload, on_conflict="user_id,day").execute()
                return
            except Exception as e:
                last = e
        print(f"[Stats] Push failed (non-fatal): {last}")

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
            rows = None
            for cols in ("day,words,audio_seconds,voiced_seconds,voiced_words,"
                         "refines,refine_seconds,refine_prompt_words,refine_voice_prompts",
                         "day,words,audio_seconds,voiced_seconds,voiced_words",
                         "day,words,audio_seconds"):
                rows = self._execute_timeout(
                    client.table(_STATS_TABLE)
                    .select(cols)
                    .eq("user_id", uid)
                    .order("day", desc=True)
                    .limit(_PULL_DAYS_LIMIT), 15.0)
                if rows is not None:
                    break
            if rows is not None:
                table_ok = True
                for r in rows:
                    day = str(r.get("day") or "")[:10]
                    if day:
                        remote[day] = {"w": int(r.get("words") or 0),
                                       "s": float(r.get("audio_seconds") or 0.0),
                                       "v": float(r.get("voiced_seconds") or 0.0),
                                       "vw": int(r.get("voiced_words") or 0),
                                       "r": int(r.get("refines") or 0),
                                       "rs": float(r.get("refine_seconds") or 0.0),
                                       "rp": int(r.get("refine_prompt_words") or 0),
                                       "rv": int(r.get("refine_voice_prompts") or 0)}
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
                            if float(rec.get("v", 0.0)) > float(cur.get("v", 0.0)):
                                cur["v"] = float(rec["v"])
                                changed = True
                            if int(rec.get("vw", 0)) > int(cur.get("vw", 0)):
                                cur["vw"] = int(rec["vw"])
                                changed = True
                            for k, cast in (("r", int), ("rs", float),
                                            ("rp", int), ("rv", int)):
                                if cast(rec.get(k, 0)) > cast(cur.get(k, 0)):
                                    cur[k] = cast(rec[k])
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
                                or float(rec.get("s", 0.0)) > rem["s"]
                                or float(rec.get("v", 0.0)) > float(rem.get("v", 0.0))
                                or int(rec.get("vw", 0)) > int(rem.get("vw", 0))):
                            to_push.append({
                                "user_id": uid,
                                "day": day,
                                "words": int(rec.get("w", 0)),
                                "audio_seconds": round(float(rec.get("s", 0.0)), 2),
                                "voiced_seconds": round(float(rec.get("v", 0.0)), 2),
                                "voiced_words": int(rec.get("vw", 0)),
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
                except Exception:
                    # Table predating the voiced columns — sync the core
                    # counters anyway.
                    slim = [{k: v for k, v in row.items()
                             if k not in ("voiced_seconds", "voiced_words")}
                            for row in to_push]
                    try:
                        client.table(_STATS_TABLE).upsert(
                            slim, on_conflict="user_id,day").execute()
                        print(f"[Stats] Synced {len(slim)} day(s) to Supabase "
                              "(voiced columns missing — run the updated "
                              "user_daily_stats.sql).")
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
