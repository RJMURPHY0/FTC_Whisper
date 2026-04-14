"""
System tray icon and menu using pystray.
Provides visual state indication and right-click menu for the app.
"""

import os
import sys
from typing import Callable, Optional

import pystray
from pystray import MenuItem as item
from PIL import Image, ImageDraw


def _resource_path(relative_path: str) -> str:
    """Get absolute path to a resource, works for dev and PyInstaller."""
    try:
        base = sys._MEIPASS
    except AttributeError:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative_path)


def _create_icon_image(color: str = "#4A9EFF", ring_color: str = "#FFFFFF") -> Image.Image:
    """
    Programmatically create a microphone-style tray icon.

    Colors by state:
        idle:       blue circle (#4A9EFF)
        recording:  red circle (#FF4444)
        processing: amber circle (#FFB347)
    """
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    padding = 4
    draw.ellipse(
        [padding, padding, size - padding, size - padding],
        fill=color, outline=ring_color, width=2,
    )

    mic_color = "#FFFFFF"
    cx, cy = size // 2, size // 2

    mic_w, mic_h = 10, 18
    draw.rounded_rectangle(
        [cx - mic_w // 2, cy - mic_h // 2 - 4, cx + mic_w // 2, cy + mic_h // 2 - 4],
        radius=5, fill=mic_color,
    )

    arc_w = 16
    draw.arc(
        [cx - arc_w // 2, cy - 10, cx + arc_w // 2, cy + 8],
        start=0, end=180, fill=mic_color, width=2,
    )

    draw.line([cx, cy + 8,  cx, cy + 14],       fill=mic_color, width=2)
    draw.line([cx - 6, cy + 14, cx + 6, cy + 14], fill=mic_color, width=2)

    return img


# Pre-generate icons for each state
ICON_IMAGES = {
    "idle":       _create_icon_image(color="#4A9EFF"),
    "recording":  _create_icon_image(color="#FF4444"),
    "processing": _create_icon_image(color="#FFB347"),
}


class TrayApp:
    """
    System tray application with dynamic icon and right-click menu.

    States:
        idle       → blue microphone icon
        recording  → red microphone icon
        processing → amber microphone icon
    """

    APP_NAME = "FTC Whisper"
    TOOLTIP_STATES = {
        "idle":       "FTC Whisper — Ready",
        "recording":  "FTC Whisper — Recording…",
        "processing": "FTC Whisper — Transcribing…",
    }

    def __init__(
        self,
        on_quit: Optional[Callable] = None,
        on_open_config: Optional[Callable] = None,
        on_sign_out: Optional[Callable] = None,
        on_open: Optional[Callable] = None,
    ):
        self.on_quit        = on_quit
        self.on_open_config = on_open_config
        self.on_sign_out    = on_sign_out
        self.on_open        = on_open
        self._icon: Optional[pystray.Icon] = None
        self._current_state = "idle"
        self._user_email: str = ""

    def set_user_email(self, email: str) -> None:
        self._user_email = email
        if self._icon:
            self._icon.update_menu()

    def _build_menu(self) -> pystray.Menu:
        items = []

        if self.on_open:
            items.append(item("Open FTC Whisper", self._on_open, default=True))
            items.append(pystray.Menu.SEPARATOR)

        if self._user_email:
            items.append(item(lambda _: self._user_email, None, enabled=False))
            items.append(pystray.Menu.SEPARATOR)

        items.append(item(
            lambda _: self.TOOLTIP_STATES.get(self._current_state, "Ready"),
            None, enabled=False,
        ))
        items.append(pystray.Menu.SEPARATOR)
        items.append(item("Open Settings", self._on_open_config))

        if self.on_sign_out:
            items.append(item("Sign Out", self._on_sign_out))

        items.append(item("Quit", self._on_quit))
        return pystray.Menu(*items)

    def _on_open(self, _icon, _item) -> None:
        if self.on_open:
            self.on_open()

    def _on_sign_out(self, _icon, _item) -> None:
        if self.on_sign_out:
            self.on_sign_out()

    def _on_quit(self, icon, _item) -> None:
        print("[Tray] Shutting down...")
        if self.on_quit:
            self.on_quit()
        icon.stop()

    def _on_open_config(self, _icon, _item) -> None:
        if self.on_open_config:
            self.on_open_config()
        else:
            config_path = _resource_path("config.json")
            if os.path.exists(config_path):
                os.startfile(config_path)

    def update_icon(self, state: str) -> None:
        self._current_state = state
        if self._icon is not None:
            self._icon.icon  = ICON_IMAGES.get(state, ICON_IMAGES["idle"])
            self._icon.title = self.TOOLTIP_STATES.get(state, self.APP_NAME)
            self._icon.update_menu()

    def run(self) -> None:
        """Start the system tray icon. Can run on a background thread on Windows."""
        self._icon = pystray.Icon(
            self.APP_NAME,
            ICON_IMAGES["idle"],
            self.TOOLTIP_STATES["idle"],
            menu=self._build_menu(),
        )
        print(f"[Tray] {self.APP_NAME} is running in the system tray.")
        self._icon.run()

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.stop()
