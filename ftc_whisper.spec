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
    'openai',              # OpenRouter path (openai-compatible SDK)
    'onnxruntime',         # Parakeet engine runtime
    'onnx_asr',            # Parakeet model loader (pure Python)
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
# The bundled config is SANITIZED: raw API keys must never ship inside a
# public GitHub release exe (anyone can extract them). Signed-in clients
# fetch the AI keys from Supabase app_settings at runtime instead.
import json as _json
_SECRET_KEYS = ("anthropic_api_key", "openrouter_api_key", "supabase_password")
_src_cfg = os.path.join(APP_DIR, 'config.json')
# Must keep the basename 'config.json' — the frozen bootstrap reads
# sys._MEIPASS/config.json — so sanitize into a build subfolder.
_build_cfg = os.path.join(APP_DIR, 'build', 'sanitized', 'config.json')
os.makedirs(os.path.dirname(_build_cfg), exist_ok=True)
try:
    with open(_src_cfg, encoding='utf-8') as _f:
        _cfg = _json.load(_f)
    for _k in _SECRET_KEYS:
        if _cfg.get(_k):
            print(f"[spec] Stripping secret '{_k}' from bundled config")
            _cfg[_k] = ""
    _cfg["supabase_email"] = ""
    with open(_build_cfg, 'w', encoding='utf-8') as _f:
        _json.dump(_cfg, _f, indent=2)
except Exception as _e:
    raise SystemExit(f"[spec] Could not sanitize config.json: {_e}")

datas += [
    (os.path.join(APP_DIR, 'logo.png'),     '.'),
    (os.path.join(APP_DIR, 'logo.ico'),     '.'),
    (os.path.join(APP_DIR, 'app_icon.png'), '.'),
    (_build_cfg, '.'),
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
    # Embedded exe icon = the black "FTC whisper" wordmark tile — this is what
    # Explorer and the taskbar pin show. The RUNNING window keeps the swirl
    # (set at runtime via iconbitmap(logo.ico)); logo.png (in-app header) also
    # stays the swirl. So: swirl in-app + title bar, wordmark on the exe/pin.
    icon=os.path.join(APP_DIR, 'exe_icon.ico'),
    version=os.path.join(APP_DIR, 'version_info.txt'),
)
