"""
FTC Whisper — auto-update helpers.

All network errors are swallowed silently; updating is best-effort and
should never crash or block the main application.
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from typing import Callable, Optional

_GITHUB_API = "https://api.github.com/repos/RJMURPHY0/FTC_Whisper/releases/latest"
_DOWNLOAD_FILENAME = "FTC-Whisper.exe"


def _version_tuple(v: str):
    return tuple(int(x) for x in v.lstrip("v").split("."))


def is_newer(latest: str, current: str) -> bool:
    """Return True if *latest* is strictly newer than *current*."""
    try:
        return _version_tuple(latest) > _version_tuple(current)
    except Exception:
        return False


def get_latest_release() -> Optional[dict]:
    """
    Query GitHub Releases API and return {"version": str, "download_url": str},
    or None on any error.
    """
    try:
        req = urllib.request.Request(
            _GITHUB_API,
            headers={"User-Agent": "FTC-Whisper-Updater/1.0"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())

        tag = data.get("tag_name", "")
        assets = data.get("assets", [])
        url = next(
            (a["browser_download_url"] for a in assets
             if a.get("name") == _DOWNLOAD_FILENAME),
            None,
        )
        if tag and url:
            return {"version": tag, "download_url": url}
    except Exception:
        pass
    return None


def check_for_update(current_version: str, callback: Callable[[str, str], None]) -> None:
    """
    Check for a newer release in a background thread.
    Calls callback(version, download_url) on the calling thread if an update
    is found — the caller is responsible for routing this onto the UI thread.
    """
    import threading

    def _worker():
        info = get_latest_release()
        if info and is_newer(info["version"], current_version):
            callback(info["version"], info["download_url"])

    threading.Thread(target=_worker, daemon=True, name="update-check").start()


def download_update(
    url: str,
    dest_path: str,
    progress_cb: Callable[[int, int], None],
) -> None:
    """
    Download *url* to *dest_path*, calling progress_cb(bytes_done, total_bytes)
    after each chunk. Raises on network/IO errors.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "FTC-Whisper-Updater/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(dest_path, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                progress_cb(done, total)


def apply_update(new_exe: str, current_exe: str) -> None:
    """
    Write a detached batch script that waits for this process to exit,
    replaces the EXE, relaunches it, then self-deletes. Exits the app.
    """
    bat = os.path.join(tempfile.gettempdir(), "ftc_whisper_update.bat")
    script = (
        "@echo off\n"
        "timeout /t 3 /nobreak > nul\n"
        f'copy /y "{new_exe}" "{current_exe}"\n'
        f'start "" "{current_exe}"\n'
        'del "%~f0"\n'
    )
    with open(bat, "w") as f:
        f.write(script)

    subprocess.Popen(
        ["cmd", "/c", bat],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )
    sys.exit(0)


def current_exe_path() -> Optional[str]:
    """Return the path to the running EXE, or None when running from source."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return None
