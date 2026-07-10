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


@dataclass
class Config:
    """Application configuration with defaults."""

    hotkey: str = "alt+v"
    refine_hotkey: str = "alt+r"
    mode: str = "hold"  # "hold" or "toggle"
    whisper_model: str = "small.en"  # accurate/upgrade model; was "base" (same size as the fast pass = no accuracy gain). tiny, base, small, medium, large-v3, large-v3-turbo
    language: str = "en"
    sample_rate: int = 16000
    input_device: str = ""  # Optional input device name fragment or index
    inject_method: str = "clipboard"  # "clipboard" or "keystrokes"
    sound_feedback: bool = True
    auto_start: bool = False
    anthropic_api_key: str = ""  # Optional — enables AI text refinement
    openrouter_api_key: str = ""  # Optional — enables AI via OpenRouter (alternative to Anthropic direct)
    openrouter_model: str = "google/gemini-2.5-flash-lite"  # OpenRouter model for refinement/context-fix
    warm_mic: bool = True  # Keep mic stream open with ~1.5s pre-roll: instant start, first syllable never lost
    auto_update: bool = True  # Silently download new releases and install them when the app is idle
    use_parakeet: bool = True  # Parakeet TDT engine (near-instant, high accuracy, English) with whisper fallback
    custom_vocabulary: str = ""  # Comma-separated terms to boost in Whisper (names, acronyms, domain words)
    auto_punctuate: bool = True   # Add trailing period when Whisper output has no terminal punctuation
    live_captions: bool = False   # Show live text of what you're saying (replaces the waveform bar while recording)
    trailing_space: bool = False  # Append a space after each injection (useful when dictating mid-sentence)
    auto_enter: bool = False      # Press Enter after injection (useful for chat/search boxes)
    toggle_timeout: int = 0       # Seconds before auto-stopping in toggle mode (0 = disabled; long dictation must not be cut off)
    max_recording_duration: int = 0  # Hard cap on recording length in seconds (0 = unlimited)
    supabase_url: str = ""  # Optional — enables transcription logging
    supabase_key: str = ""  # Publishable (anon) key
    supabase_email: str = ""  # Account email for silent background auth
    supabase_password: str = ""  # Account password for silent background auth

    # Derived / runtime fields (not persisted)
    _config_path: str = field(default="", repr=False)

    def save(self) -> None:
        """Persist current settings to disk atomically.

        Writes to a temp file in the same directory then os.replace()s it over
        the target, so a crash or concurrent save mid-write can never leave a
        truncated/corrupt config.json (which would silently reset every setting
        to defaults on the next load).
        """
        data = asdict(self)
        data.pop("_config_path", None)
        path = self._config_path or get_config_path()
        tmp = f"{path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)  # atomic on Windows + POSIX
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise

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
            except (json.JSONDecodeError, IOError) as e:
                print(f"[Config] Warning: Could not load {path}: {e}. Using defaults.")
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

        return config
