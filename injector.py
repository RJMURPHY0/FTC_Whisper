"""
Text injector — pastes transcribed text at the cursor position.
Uses clipboard + Ctrl+V. Simple and reliable.
"""

import threading
import time
import pyperclip
import keyboard as kb


class Injector:
    def __init__(self, method: str = "clipboard"):
        self.method = method
        self._lock  = threading.Lock()

    def inject(self, text: str) -> bool:
        if not text or not text.strip():
            return False
        with self._lock:
            return self._inject_clipboard(text)

    def _inject_clipboard(self, text: str) -> bool:
        try:
            # Save original clipboard content
            try:
                original = pyperclip.paste()
            except Exception:
                original = None

            pyperclip.copy(text)
            time.sleep(0.08)   # ensure clipboard is populated before sending paste

            kb.send("ctrl+v")

            time.sleep(0.15)   # wait for the paste to land before restoring

            # Restore original clipboard so we don't pollute it permanently
            if original is not None:
                def _restore():
                    time.sleep(0.3)
                    try:
                        pyperclip.copy(original)
                    except Exception:
                        pass
                threading.Thread(target=_restore, daemon=True).start()

            print(f"[Injector] Pasted {len(text)} chars.")
            return True

        except Exception as e:
            print(f"[Injector] Clipboard inject failed: {e} — trying keystroke fallback")
            try:
                kb.write(text, delay=0.008)
                return True
            except Exception as e2:
                print(f"[Injector] Keystroke fallback also failed: {e2}")
                return False
