# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for FTC Whisper.
Bundles Python + all dependencies into a single Windows exe.
The Whisper model is NOT bundled — it downloads once on first use (~150 MB).
"""

import os
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs

APP_DIR = os.path.dirname(os.path.abspath(SPEC))

datas    = []
binaries = []
hiddenimports = []

# ── Collect all data / binaries / hidden imports for complex packages ────────
for pkg in [
    'faster_whisper',
    'ctranslate2',
    'tokenizers',
    'sounddevice',
    '_sounddevice_data',   # PortAudio DLL — sounddevice won't work without this
    'pystray',
    'PIL',
    'anthropic',
    'httpx',
    'httpcore',
    'anyio',
    'supabase',
    'gotrue',
    'postgrest',
    'storage3',
    'realtime',
    'huggingface_hub',
    'filelock',
    'packaging',
    'tqdm',
]:
    try:
        d, b, h = collect_all(pkg)
        datas    += d
        binaries += b
        hiddenimports += h
    except Exception as e:
        print(f"[spec] Warning: could not collect {pkg}: {e}")

# ── App data files ────────────────────────────────────────────────────────────
datas += [
    (os.path.join(APP_DIR, 'logo.png'),    '.'),
    (os.path.join(APP_DIR, 'logo.ico'),    '.'),
    (os.path.join(APP_DIR, 'config.json'), '.'),   # bundled with API keys
]

# ── Extra hidden imports that PyInstaller often misses ───────────────────────
hiddenimports += [
    'tkinter', 'tkinter.ttk', 'tkinter.messagebox', 'tkinter.simpledialog',
    'PIL._tkinter_finder',
    'numpy', 'numpy.core._multiarray_umath',
    'ctypes', 'ctypes.wintypes',
    'winsound',
    'keyboard',
    'pyperclip',
    'pystray._win32',
    'sounddevice',
    '_sounddevice',
]

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    [os.path.join(APP_DIR, 'app.py')],
    pathex=[APP_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(APP_DIR, 'rthook_sounddevice.py')],
    # Exclude heavy packages not needed at runtime
    excludes=['torch', 'torchvision', 'torchaudio',
              'matplotlib', 'scipy', 'pandas', 'jupyter',
              'IPython', 'pytest', 'setuptools', 'pip'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='FTC Whisper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # UPX disabled — packed exes trigger SmartScreen/AV false positives
    runtime_tmpdir=None,
    console=False,          # no black console window
    disable_windowed_traceback=False,
    icon=os.path.join(APP_DIR, 'logo.ico'),
    version=os.path.join(APP_DIR, 'version_info.txt'),
)
