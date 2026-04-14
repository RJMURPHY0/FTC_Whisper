"""
Configuration management for the Wispr Flow clone.
Loads/saves settings from a JSON file with sensible defaults.
"""

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional


def get_config_path() -> str:
    """Get the path to config.json, handling both dev and PyInstaller contexts."""
    if getattr(sys, 'frozen', False):
        # Running as a PyInstaller bundle — config lives next to the .exe
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "config.json")


@dataclass
class Config:
    """Application configuration with defaults."""
    hotkey: str = "caps lock"
    mode: str = "hold"             # "hold" or "toggle"
    whisper_model: str = "base"    # tiny, base, small, medium, large-v3
    language: str = "en"
    sample_rate: int = 16000
    inject_method: str = "clipboard"  # "clipboard" or "keystrokes"
    sound_feedback: bool = True
    auto_start: bool = False
    anthropic_api_key: str = ""       # Optional — enables AI text refinement
    supabase_url: str = ""            # Optional — enables transcription logging
    supabase_key: str = ""            # Publishable (anon) key
    supabase_email: str = ""          # Account email for silent background auth
    supabase_password: str = ""       # Account password for silent background auth

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
