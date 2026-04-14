"""
Visual and audio feedback for the application.
Plays sounds on recording start/stop and manages tray icon state changes.
"""

import sys
import threading
from typing import Callable, Optional


def _play_beep(frequency: int = 800, duration_ms: int = 150) -> None:
    """Play a short beep sound (Windows only)."""
    try:
        import winsound
        winsound.Beep(frequency, duration_ms)
    except Exception:
        pass  # Silently fail on non-Windows or if audio unavailable


class Feedback:
    """
    Provides audio and visual feedback for app state changes.

    Audio:
        - High beep on recording start
        - Low beep on recording stop
        - Double beep on transcription complete

    Visual:
        - Delegates icon updates to a tray callback
    """

    def __init__(
        self,
        sound_enabled: bool = True,
        on_icon_change: Optional[Callable[[str], None]] = None,
    ):
        """
        Args:
            sound_enabled: Whether to play sound feedback
            on_icon_change: Callback to change tray icon. Called with state name:
                            "idle", "recording", "processing"
        """
        self.sound_enabled = sound_enabled
        self.on_icon_change = on_icon_change

    def recording_started(self) -> None:
        """Called when recording begins."""
        if self.on_icon_change:
            self.on_icon_change("recording")

        if self.sound_enabled:
            threading.Thread(
                target=_play_beep, args=(1000, 100), daemon=True
            ).start()

    def recording_stopped(self) -> None:
        """Called when recording ends and processing begins."""
        if self.on_icon_change:
            self.on_icon_change("processing")

        if self.sound_enabled:
            threading.Thread(
                target=_play_beep, args=(600, 100), daemon=True
            ).start()

    def transcription_complete(self, text: str) -> None:
        """Called when transcription is done and text has been injected."""
        if self.on_icon_change:
            self.on_icon_change("idle")

        if self.sound_enabled:
            def _double_beep():
                _play_beep(800, 80)
                import time
                time.sleep(0.05)
                _play_beep(1000, 80)
            threading.Thread(target=_double_beep, daemon=True).start()

    def error_occurred(self, error: str) -> None:
        """Called when an error occurs during recording/transcription."""
        if self.on_icon_change:
            self.on_icon_change("idle")

        if self.sound_enabled:
            # Low buzz for error
            threading.Thread(
                target=_play_beep, args=(300, 300), daemon=True
            ).start()

        print(f"[Feedback] Error: {error}")
