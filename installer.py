"""
FTC Whisper post-install script.
Called by install.bat after the venv and pip dependencies are ready.
  - Creates config.json from template if it does not exist
  - Generates logo.ico from logo.png
  - Creates / updates the desktop shortcut
"""

import os
import shutil
import subprocess
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON  = os.path.join(APP_DIR, "venv", "Scripts", "pythonw.exe")
APP_PY  = os.path.join(APP_DIR, "app.py")
LOGO_ICO = os.path.join(APP_DIR, "logo.ico")


def _banner(msg: str) -> None:
    print(f"\n  {msg}")


def setup_config() -> None:
    config  = os.path.join(APP_DIR, "config.json")
    example = os.path.join(APP_DIR, "config.example.json")
    if not os.path.exists(config):
        if os.path.exists(example):
            shutil.copy(example, config)
            print("  [OK] config.json created from template.")
            print()
            print("  *** Optional: open config.json to add your API keys ***")
            print("    anthropic_api_key  — enables AI text refinement")
            print("    supabase_url/key   — enables transcription history & sync")
        else:
            print("  [WARN] config.example.json not found — skipping config setup.")
    else:
        print("  [OK] config.json already present.")


def create_icon() -> str:
    """Build logo.ico (the FTC swirl) as a square app icon: the logo on a dark
    rounded tile so it's visible on any taskbar. Source: app_icon.png (the square
    FTC logo), falling back to logo.png."""
    src_png = os.path.join(APP_DIR, "app_icon.png")
    if not os.path.exists(src_png):
        src_png = os.path.join(APP_DIR, "logo.png")
    if not os.path.exists(src_png):
        print("  [WARN] app_icon.png / logo.png not found — shortcut will use default icon.")
        return ""
    try:
        from PIL import Image, ImageDraw
        src = Image.open(src_png).convert("RGBA")
        # Trim transparent margins so the logo fills the tile
        bbox = src.split()[3].getbbox()
        if bbox:
            src = src.crop(bbox)

        BG = (26, 26, 26, 255)  # app dark background (#1a1a1a)
        frames = []
        for size in (256, 64, 48, 32, 16):
            tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            radius = int(size * 0.20) if size >= 48 else 0  # rounded on big sizes, crisp square when tiny
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
            tile.paste(Image.new("RGBA", (size, size), BG), (0, 0), mask)
            pad = int(size * 0.12)
            inner = max(1, size - pad * 2)
            swirl = src.copy()
            swirl.thumbnail((inner, inner), Image.LANCZOS)
            off = ((size - swirl.width) // 2, (size - swirl.height) // 2)
            tile.paste(swirl, off, swirl)
            frames.append(tile)

        frames[0].save(
            LOGO_ICO, format="ICO",
            append_images=frames[1:],
            sizes=[(f.width, f.height) for f in frames],
        )
        print("  [OK] logo.ico created.")
        return LOGO_ICO
    except Exception as e:
        print(f"  [WARN] Icon creation failed: {e}")
        return ""


def create_shortcut(icon_path: str) -> None:
    """Create (or replace) the desktop shortcut via PowerShell."""
    icon_loc = f"{icon_path},0" if icon_path else f"{PYTHON},0"

    ps = (
        "$sh = New-Object -ComObject WScript.Shell; "
        "$d  = $sh.SpecialFolders('Desktop'); "
        "$lnk = $sh.CreateShortcut($d + '\\FTC Whisper.lnk'); "
        f"$lnk.TargetPath       = '{PYTHON}'; "
        f"$lnk.Arguments        = '\"{APP_PY}\"'; "
        f"$lnk.WorkingDirectory = '{APP_DIR}'; "
        f"$lnk.IconLocation     = '{icon_loc}'; "
        "$lnk.Description      = 'FTC Whisper - Voice-to-Text'; "
        "$lnk.Save()"
    )

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode == 0:
            print("  [OK] Desktop shortcut created: 'FTC Whisper'")
        else:
            print(f"  [WARN] Shortcut creation failed:\n{result.stderr.strip()}")
    except Exception as e:
        print(f"  [WARN] Shortcut creation failed: {e}")


def add_to_startup() -> None:
    """Add FTC Whisper to Windows startup via the user Startup folder."""
    try:
        import winreg
        # Resolve %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion"
                            r"\Explorer\Shell Folders") as k:
            startup_dir, _ = winreg.QueryValueEx(k, "Startup")
    except Exception:
        startup_dir = os.path.join(os.environ.get("APPDATA", ""),
                                   r"Microsoft\Windows\Start Menu\Programs\Startup")

    icon_loc = f"{LOGO_ICO},0" if os.path.exists(LOGO_ICO) else f"{PYTHON},0"
    ps = (
        "$sh = New-Object -ComObject WScript.Shell; "
        f"$lnk = $sh.CreateShortcut('{startup_dir}\\FTC Whisper.lnk'); "
        f"$lnk.TargetPath       = '{PYTHON}'; "
        f"$lnk.Arguments        = '\"{APP_PY}\"'; "
        f"$lnk.WorkingDirectory = '{APP_DIR}'; "
        f"$lnk.IconLocation     = '{icon_loc}'; "
        "$lnk.Description      = 'FTC Whisper - Voice-to-Text'; "
        "$lnk.Save()"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode == 0:
            print("  [OK] Added to Windows startup folder.")
        else:
            print(f"  [WARN] Startup shortcut failed:\n{result.stderr.strip()}")
    except Exception as e:
        print(f"  [WARN] Startup shortcut failed: {e}")


def register_url_protocol() -> None:
    """Register ftcwhisper:// URL protocol so browsers can launch the app."""
    try:
        import winreg
        cmd = f'"{PYTHON}" "{APP_PY}" "%1"'
        base = r"Software\Classes\ftcwhisper"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
            winreg.SetValueEx(k, "",             0, winreg.REG_SZ, "URL:FTC Whisper")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, cmd)
        print("  [OK] Registered ftcwhisper:// URL protocol.")
    except Exception as e:
        print(f"  [WARN] URL protocol registration failed: {e}")


def main() -> None:
    print()
    print("  ==============================================")
    print("   FTC Whisper  |  Post-install setup")
    print("  ==============================================")

    _banner("Setting up config...")
    setup_config()

    _banner("Creating application icon...")
    icon = create_icon()

    _banner("Creating desktop shortcut...")
    create_shortcut(icon)

    # Auto-launch is now owned entirely by the running app (app.py
    # _ensure_startup_task → Task Scheduler, with stable %LOCALAPPDATA% exe
    # path and legacy-launcher reconciliation). The old Startup-folder shortcut
    # pointed at venv\Scripts\pythonw.exe + app.py, which never exists for a
    # user running the built exe, and competed with the task at boot.
    # add_to_startup() intentionally no longer called.

    _banner("Registering browser launch protocol...")
    register_url_protocol()

    print()
    print("  ==============================================")
    print("   All done!  Double-click 'FTC Whisper'")
    print("   on your desktop to launch the app.")
    print("  ==============================================")
    print()


if __name__ == "__main__":
    main()
