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


@dataclass
class Config:
    """Application configuration with defaults."""

    hotkey: str = "alt+v"
    mode: str = "hold"  # "hold" or "toggle"
    whisper_model: str = "base"  # tiny, base, small, medium, large-v3
    language: str = "en"
    sample_rate: int = 16000
    input_device: str = ""  # Optional input device name fragment or index
    inject_method: str = "clipboard"  # "clipboard" or "keystrokes"
    sound_feedback: bool = True
    auto_start: bool = False
    anthropic_api_key: str = ""  # Optional — enables AI text refinement
    supabase_url: str = ""  # Optional — enables transcription logging
    supabase_key: str = ""  # Publishable (anon) key
    supabase_email: str = ""  # Account email for silent background auth
    supabase_password: str = ""  # Account password for silent background auth

    # Derived / runtime fields (not persisted)
    _config_path: str = field(default="", repr=False)

    def save(self) -> None:
        """Persist current settings to disk."""
        data = asdict(self)
        data.pop("_config_path", None)
        path = self._config_path or get_config_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

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
                for key, value in data.items():
                    if hasattr(config, key) and not key.startswith("_"):
                        setattr(config, key, value)
            except (json.JSONDecodeError, IOError) as e:
                print(f"[Config] Warning: Could not load {path}: {e}. Using defaults.")
        else:
            # Create default config file
            config.save()
            print(f"[Config] Created default config at {path}")

        return config
