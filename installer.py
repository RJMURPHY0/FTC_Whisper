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
    """Convert logo.png → logo.ico. Returns path to .ico or empty string."""
    logo_png = os.path.join(APP_DIR, "logo.png")
    if not os.path.exists(logo_png):
        print("  [WARN] logo.png not found — shortcut will use default icon.")
        return ""
    try:
        from PIL import Image
        img = Image.open(logo_png).convert("RGBA")
        img.save(LOGO_ICO, format="ICO", sizes=[(256, 256), (64, 64), (32, 32), (16, 16)])
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

    print()
    print("  ==============================================")
    print("   All done!  Double-click 'FTC Whisper'")
    print("   on your desktop to launch the app.")
    print("  ==============================================")
    print()


if __name__ == "__main__":
    main()
