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
import threading
import time
import urllib.request
from typing import Callable, Optional

_GITHUB_API = "https://api.github.com/repos/RJMURPHY0/FTC_Whisper/releases/latest"
_DOWNLOAD_FILENAME = "FTC-Whisper.exe"

_cached_release: Optional[dict] = None

# A release exe bundles Python + models glue; anything smaller than this is a
# GitHub error page or a truncated download, never a real build.
_MIN_EXE_BYTES = 5 * 1024 * 1024

_APPLY_LOCK = threading.Lock()
_apply_started = False


def _app_data_dir() -> str:
    """Per-user data dir shared with app.py (%LOCALAPPDATA%\\FTC Whisper)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local"
    )
    d = os.path.join(base, "FTC Whisper")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _last_version_path() -> str:
    return os.path.join(_app_data_dir(), "last-version.txt")


def read_last_run_version() -> str:
    """Version the app last ran as, or "" (fresh install / file missing)."""
    try:
        with open(_last_version_path(), "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def write_last_run_version(version: str) -> None:
    try:
        with open(_last_version_path(), "w", encoding="utf-8") as f:
            f.write(version)
    except Exception:
        pass


def cached_release() -> Optional[dict]:
    """Return the last successful result from get_latest_release(), or None."""
    return _cached_release


def _version_tuple(v: str):
    """Tolerant parse: 'v1.6.0-beta' / '1.6.0rc1' -> (1, 6, 0). A non-numeric
    tag must not make is_newer fail closed and hide real updates."""
    parts = []
    for seg in v.lstrip("vV").split("."):
        digits = ""
        for ch in seg:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


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
    if total and done != total:
        raise IOError(f"Truncated download: got {done} of {total} bytes")


def verify_exe(path: str, min_bytes: int = _MIN_EXE_BYTES) -> None:
    """Sanity-check a downloaded update before it is allowed to replace the
    installed exe. Raises IOError on anything suspicious — installing a
    corrupt/HTML-error-page 'exe' bricks the install until manual reinstall."""
    size = os.path.getsize(path)
    if size < min_bytes:
        raise IOError(f"Downloaded file too small ({size} bytes) — not a release exe")
    with open(path, "rb") as f:
        if f.read(2) != b"MZ":
            raise IOError("Downloaded file is not a Windows executable (no MZ header)")


def apply_update(new_exe: str, current_exe: str) -> None:
    """
    Launch a detached PowerShell script that waits for this process to exit,
    copies the new exe over the current one (with retries for AV file locks),
    relaunches, then self-deletes. Falls back to launching from temp if the
    copy keeps failing. Exits the current process immediately via os._exit.

    Idempotent per process: the manual "Update Now" button and the automatic
    updater can both reach this — only the first caller wins, the rest no-op
    (two swap scripts racing over the same exe corrupts the install).
    """
    global _apply_started
    with _APPLY_LOCK:
        if _apply_started:
            return
        _apply_started = True
    spawn_swap_script(new_exe, current_exe, os.getpid())
    # os._exit guarantees the process dies immediately so the PS script unblocks
    os._exit(0)


def spawn_swap_script(new_exe: str, current_exe: str, pid: int) -> str:
    """Write + launch the detached PowerShell swap script. Waits for *pid* to
    exit, then copies new_exe over current_exe and relaunches. Returns the log
    file path. Split from apply_update so it can be exercised against a dummy
    process in tests without killing the caller."""
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
# Kill any OTHER instances still holding the installed exe open. A double-launch
# leaves a second process whose loaded image locks $CurExe, so Copy-Item fails
# every retry and the update silently never replaces the exe. Match by full path
# first (precise), then by leaf name as a backstop.
try {{
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object {{ $_.Path -eq $CurExe }} |
        Stop-Process -Force -ErrorAction SilentlyContinue
}} catch {{}}
try {{
    $leaf = [System.IO.Path]::GetFileNameWithoutExtension($CurExe)
    Get-Process -Name $leaf -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
}} catch {{}}
Start-Sleep -Milliseconds 800
Log "Cleared any sibling instances holding the exe."
# Remove Mark-of-the-Web so Defender releases its scan lock on the download
Unblock-File -Path $NewExe -ErrorAction SilentlyContinue
Log "Unblocked download. Brief settle before copy (retry loop handles any remaining scan lock)..."
Start-Sleep -Seconds 3
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

    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden",
        "-File", ps_file,
    ]
    # NEVER combine DETACHED_PROCESS with CREATE_NO_WINDOW: they are conflicting
    # console modes and powershell.exe exits 0 without ever running -File. That
    # exact combination shipped in every build ≤1.6.3 — the app killed itself
    # via os._exit and the swap script silently never started, which is why
    # updates never applied. CREATE_NO_WINDOW alone gives the child its own
    # hidden console, and children outlive their parent by default on Windows.
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    std = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
               stderr=subprocess.DEVNULL)
    try:
        # Break out of any job object (Task Scheduler wraps the logon-task app
        # in one) so the swap script can't be reaped when the task's process
        # tree is torn down at os._exit.
        subprocess.Popen(
            cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **std
        )
    except OSError:
        subprocess.Popen(cmd, creationflags=flags, **std)
    return log_file


def current_exe_path() -> Optional[str]:
    """Return the path to the running EXE, or None when running from source."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return None


def run_auto_update(
    version: str,
    url: str,
    current_exe: str,
    is_idle: Callable[[], bool],
    on_status: Callable[[str], None] = lambda _msg: None,
    apply_fn: Callable[[str, str], None] = None,
    poll_interval: float = 5.0,
    idle_samples: int = 6,
) -> None:
    """
    Fully automatic update: download → verify → wait for the app to be idle →
    apply (restart). Blocking — run in a daemon thread.

    is_idle() must be cheap and thread-safe; the update is only applied after
    *idle_samples* consecutive polls report idle, so a restart never lands in
    the middle of a dictation.

    apply_fn is injectable for tests; the default (apply_update) exits the
    process and never returns.
    """
    dest = os.path.join(_app_data_dir(), "FTC-Whisper-new.exe")

    # Download with retries — transient network errors must not strand the
    # user on an old build until the next 6-hour check.
    last_exc = None
    for attempt, backoff in enumerate((0, 20, 60), start=1):
        if backoff:
            time.sleep(backoff)
        try:
            on_status(f"Downloading update v{version.lstrip('v')}…")
            download_update(url, dest, lambda *_: None)
            verify_exe(dest)
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            print(f"[Updater] Download attempt {attempt} failed: {exc}")
    if last_exc is not None:
        on_status("")  # clear status — next 6-hour check retries from scratch
        try:
            os.remove(dest)
        except OSError:
            pass
        return

    on_status("Update downloaded — installing when idle…")
    consecutive = 0
    while consecutive < idle_samples:
        time.sleep(poll_interval)
        consecutive = consecutive + 1 if is_idle() else 0

    on_status(f"Installing v{version.lstrip('v')} — restarting…")
    (apply_fn or apply_update)(dest, current_exe)
