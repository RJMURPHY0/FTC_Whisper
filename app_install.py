"""
Windows application registration for FTC Whisper.

Makes the installed copy look like a real application to Windows instead of a
loose exe someone left in Downloads:

  * a Start-menu entry, so it appears in the Start list and in Windows Search
  * a desktop shortcut, created ONCE on first install
  * an "Installed apps" (Add/Remove Programs) entry with publisher, version,
    size, icon and a working uninstaller
  * an App Paths registration, so Win+R "FTC Whisper" launches it

Everything is per-user (HKCU + %LOCALAPPDATA%/%APPDATA%), so nothing needs
elevation and nothing touches another account. This is the same shape Slack,
Discord, Teams and VS Code (user setup) use for their per-user installs, and it
is why those apps show up in Start and in Installed apps while still updating
themselves in place.

AUTO-UPDATE IS UNTOUCHED. Every shortcut and every registry value points at the
CANONICAL exe (%LOCALAPPDATA%\\FTC Whisper\\FTC Whisper.exe), the path the
updater swaps in place, so no link can ever go stale after an update.
register() re-runs on every launch and refreshes DisplayVersion, so the entry in
Installed apps tracks the version the app actually is.

Nothing here is on the dictation path: registration runs on a daemon thread at
startup and every step is individually wrapped, so a failure degrades to "no
Start-menu entry", never to a broken app.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

APP_NAME = "FTC Whisper"
PUBLISHER = "FTC Safety Solutions"
APP_URL = "https://github.com/RJMURPHY0/FTC_Whisper"
SHORTCUT_DESC = "Push-to-talk dictation for Windows"

# Per-user Add/Remove Programs entry. HKCU (not HKLM): no elevation, and it
# only ever appears for the user who actually installed it.
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\FTCWhisper"
APP_PATHS_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\App Paths\FTC Whisper.exe"
)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
URL_PROTOCOL_KEY = r"Software\Classes\ftcwhisper"
TASK_NAME = "FTC Whisper"

STATE_FILE = "install-state.json"

# MessageBoxW
_MB_YESNO = 0x00000004
_MB_ICONQUESTION = 0x00000020
_MB_ICONINFO = 0x00000040
_MB_SETFOREGROUND = 0x00010000
_MB_TOPMOST = 0x00040000
_IDYES = 6

_NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ── Paths ────────────────────────────────────────────────────────────────────


def _install_dir() -> str:
    """%LOCALAPPDATA%\\FTC Whisper. Deliberately duplicated from app.py rather
    than imported, so the uninstaller never drags the whole app in."""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local"
    )
    return os.path.join(base, APP_NAME)


def _user_data_dir() -> str:
    """%APPDATA%\\FTC Whisper: config, encrypted session, local audio."""
    base = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming"
    )
    return os.path.join(base, APP_NAME)


def _shell_folder(name: str, fallback: str) -> str:
    """Resolve a shell folder from the registry, not from a guessed path. The
    Desktop and Start-menu folders are routinely redirected (OneDrive, roaming
    profiles, group policy) and a hardcoded %USERPROFILE%\\Desktop writes the
    shortcut somewhere the user never looks."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as k:
            value, _ = winreg.QueryValueEx(k, name)
            if value and os.path.isdir(value):
                return value
    except Exception:
        pass
    return fallback


def start_menu_dir() -> str:
    return _shell_folder(
        "Programs",
        os.path.join(
            os.environ.get("APPDATA", ""),
            r"Microsoft\Windows\Start Menu\Programs",
        ),
    )


def desktop_dir() -> str:
    return _shell_folder(
        "Desktop", os.path.join(os.path.expanduser("~"), "Desktop")
    )


def startup_dir() -> str:
    return _shell_folder(
        "Startup",
        os.path.join(
            os.environ.get("APPDATA", ""),
            r"Microsoft\Windows\Start Menu\Programs\Startup",
        ),
    )


def start_menu_link() -> str:
    return os.path.join(start_menu_dir(), f"{APP_NAME}.lnk")


def desktop_link() -> str:
    return os.path.join(desktop_dir(), f"{APP_NAME}.lnk")


# ── Install state ────────────────────────────────────────────────────────────


def _state_path(install_dir: str) -> str:
    return os.path.join(install_dir, STATE_FILE)


def load_state(install_dir: str) -> dict:
    try:
        with open(_state_path(install_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(install_dir: str, state: dict) -> None:
    try:
        os.makedirs(install_dir, exist_ok=True)
        with open(_state_path(install_dir), "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[Install] Could not save install state: {e}")


def shortcuts_needed(state: dict, exe: str, start_lnk: str, desktop_lnk: str,
                     exists=os.path.exists) -> list:
    """Which shortcuts to (re)write this launch.

    The Start-menu entry is maintained: recreated if it is missing or if the
    recorded exe changed. The desktop shortcut is created ONCE. If the user
    deletes it we leave it deleted, because silently putting back a shortcut
    somebody removed is what makes software feel like adware. (app.py's
    _repair_desktop_shortcut still retargets one that IS there.)
    """
    wanted = []
    if not exists(start_lnk) or state.get("exe") != exe:
        wanted.append(start_lnk)
    if not state.get("desktop_shortcut") and not exists(desktop_lnk):
        wanted.append(desktop_lnk)
    return wanted


# ── Shortcuts ────────────────────────────────────────────────────────────────


def _ps_quote(value: str) -> str:
    return str(value).replace("'", "''")


def shortcut_script(paths: list, exe: str) -> str:
    """PowerShell that writes each .lnk pointing at the canonical exe.

    WScript.Shell rather than a COM shell-link binding: it is the same call the
    rest of this app already uses for shortcuts, and it cannot half-write a
    .lnk. IconLocation is the exe itself so the shortcut always shows the
    embedded product icon, with no dependency on a loose .ico surviving.
    """
    quoted = ", ".join(f"'{_ps_quote(p)}'" for p in paths)
    return (
        "$ErrorActionPreference = 'Stop'; "
        "$sh = New-Object -ComObject WScript.Shell; "
        f"$t = '{_ps_quote(exe)}'; "
        f"$w = '{_ps_quote(os.path.dirname(exe))}'; "
        f"foreach ($p in @({quoted})) {{ "
        "$l = $sh.CreateShortcut($p); "
        "$l.TargetPath = $t; "
        "$l.Arguments = ''; "
        "$l.WorkingDirectory = $w; "
        # Single quotes only, never "$t,0": powershell.exe -Command strips
        # double quotes out of the reconstructed command line, leaving the
        # comma operator to build an ARRAY, which IShellLink refuses to save.
        "$l.IconLocation = $t + ',0'; "
        f"$l.Description = '{_ps_quote(SHORTCUT_DESC)}'; "
        "$l.Save() }; "
        "Write-Output 'ok'"
    )


def _write_shortcuts(paths: list, exe: str) -> bool:
    if not paths:
        return True
    try:
        for p in paths:
            os.makedirs(os.path.dirname(p), exist_ok=True)
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", shortcut_script(paths, exe)],
            capture_output=True, text=True, timeout=25, creationflags=_NO_WIN,
        )
        if "ok" in (r.stdout or ""):
            for p in paths:
                print(f"[Install] Shortcut created: {p}")
            return True
        print(f"[Install] Shortcut creation failed: {(r.stderr or '').strip()}")
    except Exception as e:
        print(f"[Install] Shortcut creation failed: {e}")
    return False


# ── Add/Remove Programs entry ────────────────────────────────────────────────


def _dir_size_kb(path: str) -> int:
    """Size of the install folder in KB, skipping the transient onefile unpack
    dir. Windows shows this in Installed apps; a missing size reads as a
    half-registered app."""
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d.lower() != "runtime"]
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except Exception:
        return 0
    return min(total // 1024, 0xFFFFFFF)


def uninstall_values(exe: str, version: str, install_dir: str,
                     size_kb: int, install_date: str) -> list:
    """The Add/Remove Programs value set, as (name, is_dword, value).

    Kept as data so the shape is testable without touching the registry.
    """
    parts = (version.split(".") + ["0", "0"])[:2]
    try:
        major, minor = int(parts[0]), int(parts[1])
    except ValueError:
        major, minor = 0, 0
    return [
        ("DisplayName", False, APP_NAME),
        ("DisplayVersion", False, version),
        ("DisplayIcon", False, f"{exe},0"),
        ("Publisher", False, PUBLISHER),
        ("InstallLocation", False, install_dir),
        ("InstallDate", False, install_date),
        # Windows runs these verbatim. Quoted because the path has a space.
        ("UninstallString", False, f'"{exe}" --uninstall'),
        ("QuietUninstallString", False, f'"{exe}" --uninstall /S'),
        ("URLInfoAbout", False, APP_URL),
        ("HelpLink", False, APP_URL),
        ("EstimatedSize", True, size_kb),
        ("VersionMajor", True, major),
        ("VersionMinor", True, minor),
        # There is nothing to modify or repair; without these Windows offers
        # buttons that do nothing.
        ("NoModify", True, 1),
        ("NoRepair", True, 1),
    ]


def _registered_version() -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            value, _ = winreg.QueryValueEx(k, "DisplayVersion")
            return str(value)
    except Exception:
        return ""


def _write_uninstall_entry(exe: str, version: str, install_dir: str) -> None:
    import winreg

    existing_date = ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            existing_date, _ = winreg.QueryValueEx(k, "InstallDate")
    except Exception:
        pass
    install_date = str(existing_date or time.strftime("%Y%m%d"))

    values = uninstall_values(
        exe, version, install_dir, _dir_size_kb(install_dir), install_date
    )
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        for name, is_dword, value in values:
            winreg.SetValueEx(
                k, name, 0,
                winreg.REG_DWORD if is_dword else winreg.REG_SZ,
                value,
            )
    print(f"[Install] Registered in Installed apps (v{version}).")


def _write_app_paths(exe: str) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APP_PATHS_KEY) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, exe)
        winreg.SetValueEx(k, "Path", 0, winreg.REG_SZ, os.path.dirname(exe))


# ── Entry point ──────────────────────────────────────────────────────────────


def register(exe: str, version: str) -> None:
    """Make Windows treat this install as an application. Idempotent, cheap on
    every launch after the first (a registry read and two os.path.exists), and
    never raises: each step degrades independently."""
    if sys.platform != "win32" or not exe:
        return

    install_dir = os.path.dirname(exe)
    state = load_state(install_dir)

    try:
        start_lnk, desk_lnk = start_menu_link(), desktop_link()
        wanted = shortcuts_needed(state, exe, start_lnk, desk_lnk)
        if not wanted or _write_shortcuts(wanted, exe):
            new_state = dict(state)
            new_state["exe"] = exe
            # Latch on a shortcut that is merely PRESENT too (an upgrading user
            # already has one from the old installer). Without this the flag
            # never sticks for them, and the day they delete the shortcut we
            # put it straight back.
            new_state["desktop_shortcut"] = bool(
                state.get("desktop_shortcut")
                or desk_lnk in wanted
                or os.path.exists(desk_lnk)
            )
            if new_state != state:
                save_state(install_dir, new_state)
    except Exception as e:
        print(f"[Install] Shortcut registration skipped: {e}")

    try:
        if _registered_version() != version:
            _write_uninstall_entry(exe, version, install_dir)
    except Exception as e:
        print(f"[Install] Installed-apps registration skipped: {e}")

    try:
        _write_app_paths(exe)
    except Exception as e:
        print(f"[Install] App Paths registration skipped: {e}")


# ── Uninstall ────────────────────────────────────────────────────────────────


def _message_box(text: str, title: str, flags: int) -> int:
    try:
        import ctypes

        return int(ctypes.windll.user32.MessageBoxW(0, text, title, flags))
    except Exception:
        return 0


def _delete_key_tree(root, path: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _delete_key_tree(root, path + "\\" + sub)
    except FileNotFoundError:
        return
    except Exception:
        return
    try:
        winreg.DeleteKey(root, path)
    except Exception:
        pass


def _kill_other_instances() -> None:
    """Stop the resident copy so its loaded image stops locking the exe. The
    filter excludes our own PID; we still have MessageBoxes to show."""
    for image in (f"{APP_NAME}.exe", "FTC-Whisper.exe"):
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", image, "/FI", f"PID ne {os.getpid()}"],
                capture_output=True, text=True, timeout=20, creationflags=_NO_WIN,
            )
        except Exception:
            pass


def _remove_launchers() -> None:
    import winreg

    try:
        subprocess.run(
            ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
            capture_output=True, text=True, timeout=20, creationflags=_NO_WIN,
        )
    except Exception:
        pass
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as k:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass
    except Exception:
        pass


def _remove_registry_entries() -> None:
    import winreg

    for path in (UNINSTALL_KEY, APP_PATHS_KEY, URL_PROTOCOL_KEY):
        _delete_key_tree(winreg.HKEY_CURRENT_USER, path)


def _remove_shortcuts() -> None:
    for path in (start_menu_link(), desktop_link(),
                 os.path.join(startup_dir(), f"{APP_NAME}.lnk")):
        try:
            if os.path.exists(path):
                os.remove(path)
                print(f"[Install] Removed shortcut: {path}")
        except Exception as e:
            print(f"[Install] Could not remove {path}: {e}")


def safe_to_delete(path: str) -> bool:
    """Hard guard on the deferred delete. The cleanup script runs
    Remove-Item -Recurse -Force, so a path that is anything other than our own
    folder directly under %LOCALAPPDATA% or %APPDATA% must never reach it."""
    if not path:
        return False
    p = os.path.normcase(os.path.abspath(path))
    if os.path.basename(p) != os.path.normcase(APP_NAME):
        return False
    for var in ("LOCALAPPDATA", "APPDATA"):
        root = os.environ.get(var)
        if not root:
            continue
        root = os.path.normcase(os.path.abspath(root))
        if os.path.dirname(p) == root:
            return True
    return False


def cleanup_script(pid: int, dirs: list, script_path: str) -> str:
    """PowerShell that waits for this process to exit, then removes the install
    folders. Deferred because a running exe cannot delete itself."""
    targets = ", ".join(f"'{_ps_quote(d)}'" for d in dirs)
    return f"""$ErrorActionPreference = 'SilentlyContinue'
while (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{
    Start-Sleep -Milliseconds 400
}}
Start-Sleep -Milliseconds 800
foreach ($d in @({targets})) {{
    for ($i = 0; $i -lt 20; $i++) {{
        if (-not (Test-Path -LiteralPath $d)) {{ break }}
        Remove-Item -LiteralPath $d -Recurse -Force -ErrorAction SilentlyContinue
        if (-not (Test-Path -LiteralPath $d)) {{ break }}
        Start-Sleep -Seconds 1
    }}
}}
Remove-Item -LiteralPath '{_ps_quote(script_path)}' -Force -ErrorAction SilentlyContinue
"""


def _spawn_cleanup(dirs: list) -> None:
    dirs = [d for d in dirs if safe_to_delete(d) and os.path.isdir(d)]
    if not dirs:
        return
    import tempfile

    script_path = os.path.join(
        tempfile.gettempdir(), f"ftc_whisper_uninstall_{os.getpid()}.ps1"
    )
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(cleanup_script(os.getpid(), dirs, script_path))

    # Same launch contract as the updater's swap script: CREATE_NO_WINDOW and
    # NEVER DETACHED_PROCESS (conflicting console modes make powershell.exe exit
    # 0 without running -File), plus a job breakaway so the Task Scheduler job
    # object can't reap the child when this process dies.
    flags = (_NO_WIN
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
             | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-File", script_path],
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"[Install] Deferred cleanup could not start: {e}")


def run_uninstall(silent: bool = False) -> int:
    """Handle `FTC Whisper.exe --uninstall [/S]`, the UninstallString Windows
    runs from Installed apps. Returns 0 when the uninstall ran, 1 if the user
    backed out."""
    if not silent:
        answer = _message_box(
            "Remove FTC Whisper from this PC?\n\n"
            "Dictation will stop working until you install it again.",
            "Uninstall FTC Whisper",
            _MB_YESNO | _MB_ICONQUESTION | _MB_SETFOREGROUND | _MB_TOPMOST,
        )
        if answer != _IDYES:
            return 1

    remove_data = False
    if not silent:
        remove_data = _message_box(
            "Also delete your settings, dictation history and saved "
            "recordings?\n\nChoose No to keep them for a future reinstall.",
            "Uninstall FTC Whisper",
            _MB_YESNO | _MB_ICONQUESTION | _MB_SETFOREGROUND | _MB_TOPMOST,
        ) == _IDYES

    _kill_other_instances()
    _remove_launchers()
    _remove_registry_entries()
    _remove_shortcuts()

    dirs = [_install_dir()]
    if remove_data:
        dirs.append(_user_data_dir())

    if not silent:
        _message_box(
            "FTC Whisper has been removed.",
            "FTC Whisper",
            _MB_ICONINFO | _MB_SETFOREGROUND | _MB_TOPMOST,
        )

    # Only a frozen build owns the install folder; from source there is nothing
    # of ours under %LOCALAPPDATA% to delete but the model cache, and blowing
    # that away from a dev checkout would be a surprise.
    if getattr(sys, "frozen", False):
        _spawn_cleanup(dirs)
    return 0
