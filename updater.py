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

_cached_release: Optional[dict] = None


def cached_release() -> Optional[dict]:
    """Return the last successful result from get_latest_release(), or None."""
    return _cached_release


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
    or None on any error. Result is cached in _cached_release.
    """
    global _cached_release
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
            _cached_release = {"version": tag, "download_url": url}
            return _cached_release
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
    Launch a detached PowerShell script that waits for this process to exit,
    copies the new exe over the current one (with retries for AV file locks),
    relaunches, then self-deletes. Falls back to launching from temp if the
    copy keeps failing. Exits the current process immediately via os._exit.
    """
    import os as _os
    pid = _os.getpid()
    # Escape single quotes in paths for PowerShell single-quoted strings
    new_ps  = new_exe.replace("'", "''")
    cur_ps  = current_exe.replace("'", "''")
    ps_file = os.path.join(tempfile.gettempdir(), "ftc_whisper_update.ps1")
    log_file = os.path.join(tempfile.gettempdir(), "ftc_whisper_update.log").replace("'", "''")
    script = f"""
$NewExe = '{new_ps}'
$CurExe = '{cur_ps}'
$Log    = '{log_file}'
function Log($msg) {{ "[$(Get-Date -Format 'HH:mm:ss')] $msg" | Add-Content -Path $Log -Encoding UTF8 }}
Log "Update started. PID={pid}"
Log "New: $NewExe"
Log "Cur: $CurExe"
# Wait for the old process to fully exit
while (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{
    Start-Sleep -Milliseconds 500
}}
Log "Old process exited."
# Remove Mark-of-the-Web so Defender releases its scan lock on the download
Unblock-File -Path $NewExe -ErrorAction SilentlyContinue
Log "Unblocked download. Waiting 8 s for Defender to finish scanning..."
Start-Sleep -Seconds 8
# Try to overwrite (up to 30 retries, 2 s apart = 60 s total)
$ok = $false
for ($i = 0; $i -lt 30; $i++) {{
    try {{
        Copy-Item -Path $NewExe -Destination $CurExe -Force -ErrorAction Stop
        $ok = $true
        Log "Copy succeeded on attempt $($i + 1)."
        break
    }} catch {{
        Log "Copy attempt $($i + 1) failed: $_"
        Start-Sleep -Seconds 2
    }}
}}
# Launch from installed location if copy succeeded, else run from temp as fallback
$launch = if ($ok) {{ $CurExe }} else {{ $NewExe }}
Unblock-File -Path $launch -ErrorAction SilentlyContinue
Log "Launching: $launch (copy ok=$ok)"
Start-Process -FilePath $launch
Remove-Item -Path $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
if ($ok) {{ Remove-Item -Path $NewExe -Force -ErrorAction SilentlyContinue }}
Log "Done."
"""
    with open(ps_file, "w", encoding="utf-8") as f:
        f.write(script)

    subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden",
            "-File", ps_file,
        ],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
    )
    # os._exit guarantees the process dies immediately so the PS script unblocks
    _os._exit(0)


def current_exe_path() -> Optional[str]:
    """Return the path to the running EXE, or None when running from source."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return None
