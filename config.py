"""
Configuration management for FTC Whisper.
Loads/saves settings from a JSON file with sensible defaults.

When running as a PyInstaller bundle:
  - Bundled defaults live in sys._MEIPASS/config.json
  - User config (writable, persists settings changes) lives next to the .exe
  - On first run the bundled defaults are copied to the user location
"""

import json
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Optional


def get_config_path() -> str:
    """Return the writable config path (next to .exe when frozen, next to script otherwise)."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "config.json")


def _bootstrap_config() -> None:
    """Frozen builds only: copy bundled defaults to the writable location on first run."""
    if not getattr(sys, "frozen", False):
        return
    user_cfg = get_config_path()
    if not os.path.exists(user_cfg):
        bundled = os.path.join(sys._MEIPASS, "config.json")
        if os.path.exists(bundled):
            shutil.copy(bundled, user_cfg)
            print(f"[Config] Extracted default config to {user_cfg}")


# Canonical shared auth backend. This is the SAME Supabase project as FTC Contacts,
# so a single account signs in to both apps. Older whisper builds shipped a separate
# project (below) whose user pool did NOT contain FTC Contacts accounts — so the same
# login could not sign in. Installs still pointing at a legacy project are migrated to
# the shared one on next launch (bootstrap only seeds *fresh* installs).
_SHARED_SUPABASE_URL = "https://ijeeghdxokfvlfarojlm.supabase.co"
_SHARED_SUPABASE_KEY = "sb_publishable_G96rQQGq8BEtutrsdusjlQ_R0WxNzFw"
_LEGACY_SUPABASE_URLS = ("https://mbxwqtsesxgpcfyicphs.supabase.co",)

# User-driven settings often arrive in a short burst (the Settings save button,
# for example, updates several fields back-to-back). Give those calls one tiny
# quiet window so they collapse into a single atomic disk flush.
_ASYNC_SAVE_DEBOUNCE_S = 0.03


@dataclass
class Config:
    """Application configuration with defaults."""

    hotkey: str = "alt+v"
    refine_hotkey: str = "alt+r"
    mode: str = "toggle"  # "hold" or "toggle" — toggle is the default (press to start/stop)
    ptt_hotkey: str = "alt+c"  # Second bind: hold to dictate, release to insert ("" = off)
    whisper_model: str = "small.en"  # accurate/upgrade model; was "base" (same size as the fast pass = no accuracy gain). tiny, base, small, medium, large-v3, large-v3-turbo
    language: str = "en"
    sample_rate: int = 16000
    input_device: str = "auto"  # "auto" = pick the best available mic (name-ranked, follows device changes); or a device name fragment / index. "" behaves as auto.
    inject_method: str = "clipboard"  # "clipboard" or "keystrokes"
    sound_feedback: bool = True
    auto_start: bool = False
    anthropic_api_key: str = ""  # Optional — enables AI text refinement
    openrouter_api_key: str = ""  # Optional — enables AI via OpenRouter (alternative to Anthropic direct)
    openrouter_model: str = "google/gemini-2.5-flash-lite"  # OpenRouter model for refinement/context-fix
    warm_mic: bool = True  # Keep mic stream open with ~1.5s pre-roll: instant start, first syllable never lost
    auto_update: bool = True  # Silently download new releases and install them when the app is idle
    use_parakeet: bool = True  # Parakeet TDT engine (near-instant, high accuracy, English) with whisper fallback
    noise_gate: bool = True  # Silero VAD gate on the Parakeet engine — background noise never reaches the model (whisper path has its own)
    spoken_punctuation: bool = True  # Convert dictated symbol names ("slash", "underscore", "hashtag") into the characters themselves
    custom_vocabulary: str = ""  # Comma-separated terms to boost in Whisper (names, acronyms, domain words)
    auto_punctuate: bool = True   # Add trailing period when Whisper output has no terminal punctuation
    live_captions: bool = False   # Show live text of what you're saying (replaces the waveform bar while recording)
    live_inject: bool = False     # Type words into the app live as you speak (Parakeet mode); corrects at hotkey-release
    auto_paragraphs: bool = True  # Paragraph break after a clear pause (2s+ silence following a finished sentence); Parakeet path, off during Live Typing
    parakeet_version: str = "v2"  # Parakeet model: "v2" (English) or "v3" (multilingual, lower WER); switching triggers a one-time ~660 MB download
    trailing_space: bool = False  # Append a space after each injection (useful when dictating mid-sentence)
    auto_enter: bool = False      # Press Enter after injection (useful for chat/search boxes)
    toggle_timeout: int = 0       # Seconds before auto-stopping in toggle mode (0 = disabled; long dictation must not be cut off)
    max_recording_duration: int = 0  # Hard cap on recording length in seconds (0 = unlimited)
    # Dashboard window size per signed-in account: {email_lower: "WxH"}.
    # "_default" holds the install-time default (super admin's pushed size,
    # read once from app_settings on a fresh install; never refetched).
    window_sizes: dict = field(default_factory=dict)
    supabase_url: str = ""  # Optional — enables transcription logging
    supabase_key: str = ""  # Publishable (anon) key
    supabase_email: str = ""  # Account email for silent background auth
    supabase_password: str = ""  # Account password for silent background auth

    # Derived / runtime fields (not persisted)
    _config_path: str = field(default="", repr=False)
    # Set when config.json was corrupt and settings fell back to defaults —
    # app.py surfaces it as a visible notification once the tray/UI is up.
    _load_warning: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        # Runtime-only synchronisation state. These are deliberately not
        # dataclass fields, so asdict() never tries to serialise/copy locks or a
        # live Thread into config.json.
        self._save_state_lock = threading.Lock()
        self._save_condition = threading.Condition(self._save_state_lock)
        self._save_io_lock = threading.Lock()
        self._save_pending = None
        self._save_thread = None
        self._save_sequence = 0
        self._save_last_written_sequence = 0
        self._save_last_error = None

    def _save_snapshot(self) -> tuple[dict, str]:
        """Capture the public configuration values for one disk write."""
        data = asdict(self)
        data.pop("_config_path", None)
        data.pop("_load_warning", None)
        return data, self._config_path or get_config_path()

    def _write_snapshot(self, sequence: int, data: dict, path: str) -> None:
        """Serialise one snapshot atomically, skipping an already-stale write."""
        # A synchronous save and the coalescing worker may overlap. Serialising
        # the complete temp-write/fsync/replace transaction prevents them from
        # sharing or replacing each other's temp file.
        with self._save_io_lock:
            with self._save_condition:
                if sequence < self._save_last_written_sequence:
                    return

            # Include the request sequence in the temp name. This also avoids
            # colliding with a stale temp file left by an interrupted older save.
            tmp = f"{path}.{os.getpid()}.{sequence}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)  # atomic on Windows + POSIX
                with self._save_condition:
                    self._save_last_written_sequence = max(
                        self._save_last_written_sequence, sequence
                    )
                    self._save_last_error = None
            except Exception:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                raise

    def save(self) -> None:
        """Persist current settings to disk atomically.

        Writes to a temp file in the same directory then os.replace()s it over
        the target, so a crash or concurrent save mid-write can never leave a
        truncated/corrupt config.json (which would silently reset every setting
        to defaults on the next load).
        """
        with self._save_condition:
            data, path = self._save_snapshot()
            self._save_sequence += 1
            sequence = self._save_sequence
        self._write_snapshot(sequence, data, path)

    def save_async(self) -> None:
        """Coalesce and persist user-driven changes without blocking the caller.

        Only the latest snapshot in a burst is kept. The short-lived writer is
        intentionally non-daemon: normal interpreter shutdown waits for its
        final fsync/replace, so a setting acknowledged by the UI is not silently
        lost if the app closes immediately afterwards.
        """
        with self._save_condition:
            data, path = self._save_snapshot()
            self._save_sequence += 1
            sequence = self._save_sequence
            self._save_pending = (sequence, data, path)
            self._save_pending_at = time.monotonic()

            worker = self._save_thread
            if worker is None or not worker.is_alive():
                worker = threading.Thread(
                    target=self._async_save_worker,
                    daemon=False,
                    name="config-save",
                )
                self._save_thread = worker
                worker.start()
            self._save_condition.notify_all()

    def _async_save_worker(self) -> None:
        while True:
            with self._save_condition:
                if self._save_pending is None:
                    self._save_thread = None
                    self._save_condition.notify_all()
                    return

                # Restart the quiet window whenever another save_async() call
                # replaces the pending snapshot.
                remaining = (
                    self._save_pending_at + _ASYNC_SAVE_DEBOUNCE_S
                    - time.monotonic()
                )
                if remaining > 0:
                    self._save_condition.wait(remaining)
                    continue

                request = self._save_pending
                self._save_pending = None

            sequence, data, path = request
            try:
                self._write_snapshot(sequence, data, path)
            except Exception as e:
                with self._save_condition:
                    self._save_last_error = e
                print(f"[Config] Async save failed: {e}")

    def flush_async(self, timeout: Optional[float] = None) -> bool:
        """Wait for queued async saves; useful for orderly shutdown and tests."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            with self._save_condition:
                worker = self._save_thread
            if worker is None:
                return True
            if worker is threading.current_thread():
                return False
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            worker.join(remaining)
            if worker.is_alive():
                return False
            # Normally the worker clears this reference itself. Also recover
            # from an unexpected worker-level failure so an unbounded flush can
            # never spin forever joining the same dead Thread.
            with self._save_condition:
                if self._save_thread is worker:
                    self._save_thread = None

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        """Load config from JSON, falling back to defaults for missing keys."""
        _bootstrap_config()
        path = path or get_config_path()
        config = cls()
        config._config_path = path

        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise json.JSONDecodeError("config root is not an object", "", 0)
                for key, value in data.items():
                    if hasattr(config, key) and not key.startswith("_"):
                        setattr(config, key, value)
                # Instant mic start is no longer user-configurable: warm mode
                # is how the first word survives stream-open latency, so a
                # config that disabled it via the old toggle migrates back on.
                config.warm_mic = True
            except (json.JSONDecodeError, IOError) as e:
                # Reset-to-defaults fallback (kept) — but never silently:
                # preserve the bad file and flag the reset so app.py can show
                # a visible notification once the tray/UI is up.
                backup = ""
                try:
                    backup = os.path.join(
                        os.path.dirname(path) or ".", "config.corrupt.bak"
                    )
                    shutil.copy(path, backup)
                    print(f"[Config] Backed up unreadable config to {backup}")
                except Exception as be:
                    backup = ""
                    print(f"[Config] Could not back up corrupt config (non-fatal): {be}")
                print(f"[Config] Warning: Could not load {path}: {e}. Using defaults.")
                config._load_warning = (
                    "Settings file was corrupt — settings were reset to defaults."
                    + (f" Old file saved as {os.path.basename(backup)}." if backup else "")
                )
        else:
            # Create default config file
            config.save()
            print(f"[Config] Created default config at {path}")

        # One-time migration: repoint installs still on a legacy Supabase project at
        # the shared FTC backend so the FTC Contacts login works here too. The stale
        # silent-auth creds (for the old project) are dropped — the user re-signs in
        # once with their FTC Contacts account, then the saved session persists.
        if config.supabase_url in _LEGACY_SUPABASE_URLS:
            config.supabase_url = _SHARED_SUPABASE_URL
            config.supabase_key = _SHARED_SUPABASE_KEY
            config.supabase_email = ""
            config.supabase_password = ""
            try:
                config.save()
                print("[Config] Migrated Supabase backend to shared FTC project.")
            except Exception as e:
                print(f"[Config] Backend migration save failed (non-fatal): {e}")

        # One-time migration: the hold/toggle mode UI is gone. Hands-free
        # (toggle) belongs to the main bind and hold-to-talk to the
        # push-to-talk bind, so a legacy "hold" main bind is coerced to
        # toggle whenever a PTT bind exists to carry hold semantics. A
        # hold-mode config with NO PTT bind is left alone: hold is that
        # user's only way to dictate.
        if config.mode == "hold" and config.ptt_hotkey:
            config.mode = "toggle"
            try:
                config.save()
                print("[Config] Migrated main bind to hands-free (toggle); "
                      "hold lives on the push-to-talk bind.")
            except Exception as e:
                print(f"[Config] Mode migration save failed (non-fatal): {e}")

        return config
