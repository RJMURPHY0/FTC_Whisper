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
# The bundled config is GENERATED from the Config dataclass defaults — it is
# never a copy of the developer's local config.json. That file accumulates
# machine- and account-specific state (a pinned `input_device` naming a mic
# nobody else owns, `window_sizes` keyed by the dev's email, an experimental
# `whisper_model`, raw API keys). Copying it shipped every one of those to
# every new install; a pinned headset that doesn't exist on the user's machine
# is a broken mic out of the box. Only the shared backend URL/key are layered
# on top, and they are read from config.py's constants, not from disk.
import json as _json
import sys as _sys
_sys.path.insert(0, APP_DIR)
from dataclasses import asdict as _asdict
from config import (Config as _Config,
                    _SHARED_SUPABASE_URL as _SB_URL,
                    _SHARED_SUPABASE_KEY as _SB_KEY)

# Must keep the basename 'config.json' — the frozen bootstrap reads
# sys._MEIPASS/config.json — so write into a build subfolder.
_build_cfg = os.path.join(APP_DIR, 'build', 'sanitized', 'config.json')
os.makedirs(os.path.dirname(_build_cfg), exist_ok=True)
try:
    _cfg = _asdict(_Config())
    _cfg.pop('_config_path', None)
    _cfg.pop('_load_warning', None)
    _cfg['supabase_url'] = _SB_URL
    _cfg['supabase_key'] = _SB_KEY
    # Secrets are never in the dataclass defaults, but assert it: a key that
    # ships inside a public exe is extractable by anyone who downloads it.
    for _k in ('anthropic_api_key', 'openrouter_api_key',
               'supabase_password', 'supabase_email'):
        if _cfg.get(_k):
            raise SystemExit(f"[spec] Refusing to bundle non-empty '{_k}'")
    with open(_build_cfg, 'w', encoding='utf-8') as _f:
        _json.dump(_cfg, _f, indent=2)
    print(f"[spec] Bundled config generated from Config defaults "
          f"(input_device={_cfg.get('input_device')!r}, "
          f"ptt_hotkey={_cfg.get('ptt_hotkey')!r})")
except SystemExit:
    raise
except Exception as _e:
    raise SystemExit(f"[spec] Could not generate bundled config: {_e}")

datas += [
    (os.path.join(APP_DIR, 'logo.png'),     '.'),
    (os.path.join(APP_DIR, 'logo.ico'),     '.'),
    (os.path.join(APP_DIR, 'app_icon.png'), '.'),
    (_build_cfg, '.'),
]

# ── Brand icon pack (real app/service logos for the History list) ────────────
# Bundled under assets/brand_icons/ — app_icons.get_brand_icon() resolves them
# via sys._MEIPASS/assets/brand_icons at runtime.
import glob as _glob
_brand_dir = os.path.join(APP_DIR, 'assets', 'brand_icons')
_brand_pngs = _glob.glob(os.path.join(_brand_dir, '*.png'))
for _p in _brand_pngs:
    datas.append((_p, os.path.join('assets', 'brand_icons')))
print(f"[spec] Bundling {len(_brand_pngs)} brand icons from assets/brand_icons")

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
