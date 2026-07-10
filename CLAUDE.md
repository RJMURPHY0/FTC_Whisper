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

**Release a new version (CI — this is the signed path):**
1. Bump `APP_VERSION` in `app.py` (e.g. `"1.0.7"`)
2. Update all four version fields in `version_info.txt` to match
3. Commit and push to `main`
4. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`
5. `.github/workflows/build-release.yml` builds, **code-signs via Azure Trusted
   Signing**, and publishes `FTC-Whisper.exe` to the `vX.Y.Z` release automatically.

**Do NOT build locally and upload by hand for public releases** — code signing
only runs in CI (see `docs/CODE_SIGNING.md`), so a hand-built exe ships unsigned
and gets flagged by SmartScreen/antivirus. The local PyInstaller command above is
for development/testing only. Signing requires six repo secrets and a one-time
Azure Trusted Signing setup — all documented in `docs/CODE_SIGNING.md`.

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

### Transcription pipeline (the core flow, v1.6+)
Two engines exist. **Parakeet** (`asr_engine.py`, NVIDIA Parakeet TDT 0.6b v2 int8 ONNX
via `onnx-asr`) is the primary for English: better accuracy than whisper-large-v3 at
~20x realtime on CPU, punctuation/caps built in, cost proportional to audio length.
The **whisper pipeline** (`transcriber.py`, faster-whisper) is the fallback: non-English
language configured, Parakeet model not yet downloaded (~660 MB one-time into
`%LOCALAPPDATA%\FTC Whisper\models` via plain HTTPS — NOT the hf_hub cache, whose
symlink-based layout raises WinError 1314 on stock Windows), or load failure.

**Parakeet path** (`_use_parakeet()` true): `_on_start_recording` creates a
`StreamingSession` (`stream_session.py`). While recording, its worker transcribes
incrementally: once uncommitted audio exceeds ~10s it finds a silence boundary,
transcribes up to it once, appends to a committed list, and releases that audio
(`recorder.drop_audio_before`). At hotkey-release, `session.finalize()` stops the
recorder and transcribes only the remaining tail — stop-latency stays constant
(~0.3-0.8s) regardless of dictation length. The upgrade pass is LLM `context_fix`
only (a whisper re-pass would usually be a downgrade). Live captions are pushed from
the same worker (`on_caption`) — no separate caption thread in this mode.

**Whisper path**: fast pass (`base.en`, beam=1) on the full clip → inject; a silence
energy-gate runs before the synchronous accurate fallback; background `_upgrade()`
runs the user model + `context_fix`. The whisper caption loop paces on
`_caption_stop_event` (CLEAR while running) — never wait on `_caption_loop_running`,
which is SET while running so `Event.wait()` returns immediately (busy-spin bug).

All upgrade results are stamped with `self._dictation_seq` and passed as `session=` to
`popup.set_upgrade_result()` — the popup discards stale results from an earlier dictation.

All engines share the same `transcribe(audio, rate, blocking=, context_words=, hotwords_str=)`
surface and are pre-loaded at startup in parallel daemon threads. `blocking=False`
returns `""` instead of queuing — used by caption/preview ticks.

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
`AIRefiner` in `ai_refiner.py` prefers OpenRouter (`config.openrouter_model`, default
`google/gemini-2.5-flash-lite`, with an in-request `models` fallback array) and falls
back to Anthropic Claude Haiku direct. Both clients use a 20s timeout / 1 retry. All
modes are defined in `REFINE_PROMPTS` at the top of the file. The `context_fix` mode is
special — it must NOT append `_NO_FORMAT`, uses the minimal corrector system prompt
(NOT the style prompt, which contradicts "change nothing"), and has a word-count
tolerance guard in `context_fix()`. `max_tokens` scales with input length. The
`punctuation` mode fixes punctuation, grammar, and spelling (shown in the popup as
"✨ Fix All"). All other modes are user-triggered from the popup refinement panel.

### Update flow (fully automatic since v1.6.4)
`updater.py` checks `https://api.github.com/repos/RJMURPHY0/FTC_Whisper/releases/latest` for an asset named `FTC-Whisper.exe` every 6 hours. When one is found (and `config.auto_update` is true, the default), `run_auto_update()` downloads to `%LOCALAPPDATA%\FTC Whisper\FTC-Whisper-new.exe` (3 attempts with backoff), verifies it (`verify_exe`: MZ header + ≥5 MB + Content-Length match), waits until the app is idle (`_safe_to_restart`: state IDLE and >120s since last dictation, 6 consecutive 5s polls), then `apply_update()` spawns a hidden PowerShell swap script and exits via `os._exit(0)`. The script waits for the PID to die, `Unblock-File`s the download, copies it over the installed exe with 30×2s retries, relaunches, and self-deletes. `apply_update` is guarded against double-invocation (manual button + auto worker can race). On the first launch of a new version, `_announce_update_if_any()` compares `%LOCALAPPDATA%\FTC Whisper\last-version.txt` and shows a transient "Updated to vX.Y.Z" toast (`show_toast` in `app_window.py`). The Settings banner remains as a manual "Update Now" override.

### Warm-mic health (v1.6.13+)
WASAPI/MME input streams die silently (callbacks stop, `stream.active` stays True)
after device changes, sleep/resume, or audio-engine restarts. The Recorder tracks a
**callback heartbeat** (`_last_callback_ts`): no callback for >1.2s = dead stream,
regardless of `.active`. `start()` verifies fresh audio arrives within 0.45s or
closes the warm stream, re-inits PortAudio (`sd._terminate()/_initialize()` —
REQUIRED for PortAudio to see device-topology changes) and cold-opens; a
`mic-watchdog` daemon recovers dead streams within ~5s and bounces the idle stream
every ~60s to follow the Windows default mic. Stale pre-roll is never used to seed
a recording. Never judge stream health by `.active` — only by heartbeat age. Never
call `_refresh_portaudio()` with any stream open (monitor open/close is serialised
under `_stream_lifecycle_lock` for exactly this reason).

### Stale-copy handoff (v1.6.15+)
Frozen builds compare their FileVersion resource against the canonical exe at
`%LOCALAPPDATA%\FTC Whisper\FTC Whisper.exe` at launch
(`_handoff_to_canonical_if_newer`, called BEFORE the single-instance mutex). If the
canonical copy is strictly newer, the stale copy spawns it and exits — double-clicking
an old download/shortcut always runs the auto-updated version.
`_repair_desktop_shortcut` retargets an existing desktop `FTC Whisper.lnk` to the
canonical path (repair-only, never creates).

### Auto-launch (boot)
The **running app** owns auto-launch, not the installer. On every launch `main()` spawns `_ensure_startup_task()` (daemon thread) which:
1. For frozen builds, calls `_ensure_installed_copy()` to keep a canonical exe at the **stable** path `%LOCALAPPDATA%\FTC Whisper\FTC Whisper.exe` (so the logon task never points at the volatile location the user double-clicked from, e.g. Downloads).
2. Registers a Task Scheduler logon task `FTC Whisper` pointing at that stable path, with a domain-qualified `UserId` and `RestartOnFailure`. The "already registered" check is strict — it re-registers if the task is missing or points anywhere other than the stable path.
3. Calls `_reconcile_legacy_launchers()` to delete competing launchers (the `HKCU\...\Run` fallback value and stale Startup-folder shortcuts `FTC Whisper.lnk` / `FTC Transcribe.lnk`) so exactly one launcher exists — no boot-time double-launch race.

`_ensure_startup_registry_fallback()` writes the `HKCU\Run` value only if `schtasks` is unavailable. Any uncaught exception during startup is written to `%LOCALAPPDATA%\FTC Whisper\startup-error.log` (via `_log_startup_error` and a global `threading.excepthook`), so a launch-then-crash in the `console=False` build is diagnosable instead of silent.

### Auth and Supabase
`AuthManager` in `auth.py` handles Supabase email auth. Session tokens are encrypted on disk using Windows DPAPI — only readable by the same Windows user. `SupabaseLogger` in `supabase_client.py` does all DB writes fire-and-forget. Both are optional; the app works fully offline without them.

### Warm mic and monitoring
By default (`config.warm_mic`) the Recorder keeps a persistent input stream open,
feeding a ~1.5s pre-roll ring buffer. `start()` is then instant: it flips a flag and
seeds the recording with ~0.35s of pre-roll, so the first syllable is never lost to
stream-open latency — and the go-beep fires AFTER capture is flowing. `stop()` keeps
the warm stream open. Cold open/close per recording remains as the fallback. The
recorder tracks absolute sample positions (`total_recorded_samples`, `get_audio_range`,
`drop_audio_before`, `dropped_samples`) for the streaming session.

`Recorder.start_monitor(device_name)` / `stop_monitor()` (Test Mic in Settings) use a
DEDICATED level-only callback — never the recording callback — so a recording started
during a mic test can't get interleaved chunks from two streams. `get_live_levels()`
returns the monitor's levels while a monitor is active.

## Key invariants

- `_update_context()` is called with the **best available** result only (Parakeet final / whisper accurate / LLM-fixed), never the whisper fast-model result — so the rolling context always reflects the highest-quality transcription
- `context_fix()` validates word count (with small tolerance) before accepting the LLM result — rejecting additions/deletions keeps output exactly what the user said
- The `_transcribe_lock` in `Transcriber`/`ParakeetTranscriber` serialises all transcription calls on a single model instance — do not call `transcribe()` concurrently on the same object
- Popup widget mutations always happen via `root.after(0, ...)` from background threads
- `popup.set_upgrade_result()` must always be called with the `session=` stamp of the dictation the result belongs to
- `_clipboard_paste` must NEVER send Ctrl+V when `_clipboard_set` reported failure — that pastes stale clipboard content (possibly a password) and reports success
- The updater's swap script must be spawned with `CREATE_NO_WINDOW` (+ `CREATE_NEW_PROCESS_GROUP`, DEVNULL std handles) — NEVER add `DETACHED_PROCESS`, which conflicts with `CREATE_NO_WINDOW` and makes powershell.exe exit 0 without running the script (this exact bug silently broke every in-app update ≤ v1.6.3)
- The bundled config in `ftc_whisper.spec` is sanitized at build time — API keys must never ship inside the public release exe
- `APP_VERSION` in `app.py`, `filevers`/`prodvers` tuples, and `FileVersion`/`ProductVersion` strings in `version_info.txt` must all be kept in sync before every build
