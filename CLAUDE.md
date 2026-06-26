# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Run from source (development):**
```
venv\Scripts\python.exe app.py
```
or double-click `run.bat`.

**First-time setup:**
```
install.bat
```
Creates `venv\`, installs `requirements.txt`, runs `installer.py` (creates config, desktop shortcut, registers the `ftcwhisper://` URL protocol). Note: auto-launch is **not** set up by the installer — the running app registers it itself (see Auto-launch below).

**Build the distributable exe:**
```
venv\Scripts\pyinstaller ftc_whisper.spec --noconfirm
```
Output: `dist\FTC Whisper.exe`. Before building, bump `APP_VERSION` in `app.py` and both version tuples and string values in `version_info.txt`. After building, upload `dist\FTC Whisper.exe` as `FTC-Whisper.exe` to a GitHub release — the update checker (`updater.py`) fetches `/releases/latest` and looks for an asset with exactly that name.

**Release a new version:**
1. Bump `APP_VERSION` in `app.py` (e.g. `"1.0.7"`)
2. Update all four version fields in `version_info.txt` to match
3. Commit and push to `main`
4. Build with PyInstaller
5. Create a GitHub release tagged `vX.Y.Z` and upload `dist\FTC Whisper.exe` as `FTC-Whisper.exe`

The GitHub release tag must be strictly greater than all existing release tags, because `is_newer()` in `updater.py` does a tuple comparison. Check existing releases before picking a version number.

## Architecture

### Thread model
The app is intentionally multi-threaded. tkinter runs on the **main thread** (`AppWindow.run()` → `mainloop()`). Everything else is a **daemon thread**:

- `HotkeyManager` spawns a Win32 message-pump thread for `RegisterHotKey`
- Recording starts in a daemon thread via `_on_start_recording`
- Transcription (both fast and accurate paths) runs in daemon threads
- The accurate-model upgrade and optional LLM context-fix both run in the same `_upgrade()` closure thread
- Supabase logging is always fire-and-forget daemon threads

Consequence: **never call tkinter widgets from a background thread directly** — always use `self._root.after(0, lambda: ...)`.

### Transcription pipeline (the core flow)
`_on_stop_recording()` in `app.py` orchestrates everything:

1. **Fast pass** — `fast_transcriber` (always `base.en`) transcribes and injects immediately
2. **Fallback** — if fast returns empty, `transcriber` (user-configured model) runs synchronously
3. **Background upgrade** — if fast succeeded, `_upgrade()` thread runs `transcriber` + optional LLM `context_fix`, then calls `popup.set_upgrade_result()` so the user can accept it

Both transcribers are pre-loaded at startup in parallel daemon threads. `Transcriber.transcribe()` has a `blocking=False` mode that returns `""` instead of queuing — used to avoid stacking calls.

### Accuracy pipeline (added in v1.0.7)
Three layers that cost zero latency on the injection path:

- **Rolling context window** — `_context_deque` (maxlen=150 words) in `WhisperFlowApp` is passed as part of `initial_prompt` to each transcription call. Updated only after the highest-quality result (upgrade or fallback accurate), never after the fast-model result.
- **Custom vocabulary / hotwords** — `config.custom_vocabulary` is passed as both `hotwords=` and part of `initial_prompt` to faster-whisper on every call.
- **LLM context-fix** — `AIRefiner.context_fix()` runs after the accurate model in the `_upgrade()` thread. It uses a strict "fix misheard words only" prompt and rejects the result if word count changes.

### Text injection
`Injector` in `injector.py` tries three strategies in order: clipboard (`Ctrl+V`), Unicode `VK_PACKET` SendInput (browsers), or `WM_CHAR` PostMessage (native apps). Before injection, `_focus_window()` restores Win32 focus; for browsers a synthetic mouse click is needed to restore DOM/JS focus. All modifier keys are released before the final paste to prevent `Paste Special` dialogs in Office.

### Popup lifecycle
`FloatingPopup` in `popup.py` has two display modes:
- **Status pill** — shown during recording/transcribing; contains the animated waveform and status text; follows the cursor
- **Cursor icon** — shown after injection; contains Insert / Replace / Undo buttons and the optional Upgrade button; positioned near the caret using the Accessibility API (`IAccessible`), falling back to the pre-recording cursor position

The popup is a borderless `tk.Toplevel` that stays `topmost` and never takes focus (`WS_EX_NOACTIVATE` via `wm_attributes`).

### Hotkey handling
`HotkeyManager` uses Win32 `RegisterHotKey` for modifier+key combos (Alt+V, Ctrl+J, etc.) — this suppresses the combo at OS level without a low-level keyboard hook. Single keys (CapsLock, F-keys) fall back to the `keyboard` library. When a combo hotkey is used in hold mode, a suppressor hook prevents the bare base key (e.g. `v`) from leaking into the target window if Alt is released before V.

`TriggerHotkeyManager` (for Alt+R "refine selection") is a simpler one-shot variant that reads the clipboard after triggering.

### Settings and config
`Config` is a `@dataclass` in `config.py`. Saved to `config.json` next to the exe (or next to `app.py` when running from source). PyInstaller frozen builds bootstrap the config from `sys._MEIPASS/config.json` on first run. `_on_settings_change()` in `app.py` handles live updates — most fields take effect immediately; `input_device` and `whisper_model` require a restart.

### AI refinement
`AIRefiner` in `ai_refiner.py` wraps Claude Haiku (`claude-haiku-4-5-20251001`). All modes are defined in `REFINE_PROMPTS` at the top of the file. The `context_fix` mode is special — it must NOT append `_NO_FORMAT` and has a strict "fix misheard words only" prompt with a word-count validation guard in `context_fix()`. The `punctuation` mode fixes punctuation, grammar, and spelling (shown in the popup as "✨ Fix All"). All other modes are user-triggered from the popup refinement panel.

### Update flow
`updater.py` checks `https://api.github.com/repos/RJMURPHY0/FTC_Whisper/releases/latest` for an asset named `FTC-Whisper.exe`. If a newer version is found, `apply_update()` writes a detached batch script that waits for the running process to exit, copies the downloaded exe over itself, relaunches, then self-deletes.

### Auto-launch (boot)
The **running app** owns auto-launch, not the installer. On every launch `main()` spawns `_ensure_startup_task()` (daemon thread) which:
1. For frozen builds, calls `_ensure_installed_copy()` to keep a canonical exe at the **stable** path `%LOCALAPPDATA%\FTC Whisper\FTC Whisper.exe` (so the logon task never points at the volatile location the user double-clicked from, e.g. Downloads).
2. Registers a Task Scheduler logon task `FTC Whisper` pointing at that stable path, with a domain-qualified `UserId` and `RestartOnFailure`. The "already registered" check is strict — it re-registers if the task is missing or points anywhere other than the stable path.
3. Calls `_reconcile_legacy_launchers()` to delete competing launchers (the `HKCU\...\Run` fallback value and stale Startup-folder shortcuts `FTC Whisper.lnk` / `FTC Transcribe.lnk`) so exactly one launcher exists — no boot-time double-launch race.

`_ensure_startup_registry_fallback()` writes the `HKCU\Run` value only if `schtasks` is unavailable. Any uncaught exception during startup is written to `%LOCALAPPDATA%\FTC Whisper\startup-error.log` (via `_log_startup_error` and a global `threading.excepthook`), so a launch-then-crash in the `console=False` build is diagnosable instead of silent.

### Auth and Supabase
`AuthManager` in `auth.py` handles Supabase email auth. Session tokens are encrypted on disk using Windows DPAPI — only readable by the same Windows user. `SupabaseLogger` in `supabase_client.py` does all DB writes fire-and-forget. Both are optional; the app works fully offline without them.

### Mic monitoring
`Recorder.start_monitor(device_name)` and `stop_monitor()` open a lightweight stream purely for level-reading (used by the Test Mic feature in Settings) — they share the same `_audio_callback` but do not set `_recording`, so no audio is stored. These are separate from `start()`/`stop()` which control the actual recording stream.

## Key invariants

- `_update_context()` is called with the **accurate** result only, never the fast-model result — so the rolling context always reflects the highest-quality transcription
- `context_fix()` validates word count matches before accepting the LLM result — rejecting additions/deletions keeps output exactly what the user said
- The `_transcribe_lock` in `Transcriber` serialises all transcription calls on a single model instance — do not call `transcribe()` concurrently on the same object
- Popup widget mutations always happen via `root.after(0, ...)` from background threads
- `APP_VERSION` in `app.py`, `filevers`/`prodvers` tuples, and `FileVersion`/`ProductVersion` strings in `version_info.txt` must all be kept in sync before every build
