"""
Fire-and-forget error reporter: sends errors to FTC Contacts Feedback & Reports page.
Reads ftc_contacts_api_url and ftc_contacts_secret from config.json.
Never raises — any failure is silently ignored.
"""

import json
import os
import threading
import urllib.request


def _load_contacts_config() -> tuple[str, str]:
    """Returns (api_url, secret) from config.json, or empty strings if missing."""
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("ftc_contacts_api_url", ""), cfg.get("ftc_contacts_secret", "")
    except Exception:
        return "", ""


def report_error(
    message: str,
    context: dict | None = None,
    user_email: str | None = None,
) -> None:
    """
    Report an error to FTC Contacts asynchronously.
    Safe to call from any thread; always returns immediately.
    """
    def _send() -> None:
        api_url, secret = _load_contacts_config()
        if not api_url or not secret:
            return
        payload: dict = {
            "message": message,
            "source": "whisper",
            "context": context or {},
        }
        if user_email:
            payload["user_email"] = user_email
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{api_url}/api/log-error",
            data=data,
            headers={
                "Content-Type": "application/json",
                "x-app-secret": secret,
            },
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()
