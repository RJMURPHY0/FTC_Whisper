# FTC Whisper

> **[⬇ Install FTC Whisper](https://github.com/RJMURPHY0/FTC---Whisper/releases/latest/download/FTC-Whisper-Setup.exe)**

---

Hold a hotkey, speak, release — your words appear wherever your cursor is.

Built for Windows. Transcription runs **fully locally** using [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — no audio ever leaves your machine. Cloud features (AI refinement, history sync) are optional.

---

## Features

- **Hold-to-talk or toggle mode** — hold your hotkey while speaking, release to transcribe
- **Local Whisper transcription** — runs on CPU or GPU, no internet required
- **AI text refinement** — rewrite as email, formal, casual, or fix punctuation (requires Anthropic API key)
- **Floating action badge** — appears directly below your inserted text after each transcription; click to open AI refinement
- **Live waveform** — animated bars react to your voice in real time (orange when speaking, like iPhone Voice Memos)
- **System tray** — runs quietly in the background with status icons
- **Transcription history** — optional sync via Supabase
- **Works fully offline** — Supabase and Claude API are both optional

---

## Installation

### Option 1 — Single exe (recommended, no Python needed)

1. [**Download FTC-Whisper-Setup.exe**](https://github.com/RJMURPHY0/FTC---Whisper/releases/latest/download/FTC-Whisper-Setup.exe)
2. Double-click it — if Windows shows a SmartScreen warning, click **More info → Run anyway**
3. The app starts immediately in your system tray

### Option 2 — Run from source (developers)

Requires [Python 3.10+](https://www.python.org/downloads/) with **Add to PATH** ticked.

1. Download or clone this repo
2. Double-click **`install.bat`**
3. Double-click the **FTC Whisper** shortcut on your desktop

---

## First Use

The app works out of the box — no sign-in or API keys required.

1. The app starts minimised to the system tray (look for the microphone icon)
2. Click the tray icon → **Open FTC Whisper** to see the dashboard
3. Click inside any text field anywhere on your PC
4. Hold **Alt+V** and speak → release to transcribe

A small badge appears near your cursor after each transcription. Click it to open the AI refinement panel.

---

## Configuration

Edit **`config.json`** (in the same folder as the app) to customise settings:

| Key | Default | Description |
|-----|---------|-------------|
| `hotkey` | `alt+v` | Trigger key — e.g. `caps lock`, `f9`, `ctrl+shift+space` |
| `mode` | `hold` | `hold` (hold while speaking) or `toggle` (press to start/stop) |
| `whisper_model` | `base.en` | Model size: `tiny.en`, `base.en`, `small.en`, `medium.en`, `large-v3` |
| `language` | `en` | Whisper language code |
| `input_device` | *(empty)* | Optional microphone override (device name fragment or numeric index); empty = auto-default + fallback |
| `sound_feedback` | `true` | Beep sounds on start/stop |
| `anthropic_api_key` | *(empty)* | [Anthropic API key](https://console.anthropic.com/) — enables AI refinement |
| `supabase_url` | *(empty)* | Supabase project URL — enables history & sync |
| `supabase_key` | *(empty)* | Supabase anon/publishable key |
| `supabase_email` | *(empty)* | Auto sign-in email on startup |
| `supabase_password` | *(empty)* | Auto sign-in password on startup |

> **Tip:** Larger Whisper models are more accurate but slower to load.  
> `base.en` is the best balance for most users on CPU.

### Model Sizes

| Model | Size | Speed | Accuracy |
|-------|------|-------|----------|
| `tiny.en` | ~75 MB | Fastest | ★★☆☆☆ |
| `base.en` | ~150 MB | Fast | ★★★☆☆ — **recommended** |
| `small.en` | ~500 MB | Medium | ★★★★☆ |
| `medium.en` | ~1.5 GB | Slow | ★★★★☆ |
| `large-v3` | ~3 GB | Slowest | ★★★★★ — GPU recommended |

---

## Supabase Auth Setup (Per-user history)

If you want account creation/login and user-isolated history:

1. In Supabase dashboard, enable Email auth and turn on **Confirm sign up**.
2. Set **URL Configuration** (`Site URL` and allowed redirect URLs).
3. Run `supabase_setup.sql` in Supabase SQL Editor.
4. Set your owner account as admin:
   `update public.profiles set role = 'admin' where email = 'your-email@example.com';`
5. Restart the app. If no saved session exists, it now opens the login/sign-up screen.

The app will then create users via Supabase Auth, auto-create a profile row, and enforce
row-level policies so users only see their own `transcriptions` history.

---

## AI Refinement Modes

After a transcription, click the badge near your cursor to open the refinement panel:

| Button | What it does |
|--------|-------------|
| ✉ Email | Rewrites as a professional email body |
| 🎩 Formal | Formal, polished tone |
| 💬 Casual | Friendly, conversational tone |
| ✨ Fix | Fixes punctuation and capitalisation only |
| ✂ Short | Makes it as concise as possible |
| ⚡ Optimise | Rewrites as an optimised AI prompt |

Requires `anthropic_api_key` in `config.json`.

---

## Privacy & Security

- **Audio never leaves your device** — Whisper runs entirely locally
- **Session tokens** are encrypted on disk using Windows DPAPI (only readable by your Windows account)
- **`config.json` is excluded from git** — your API keys are never committed to the repository
- Transcriptions are only uploaded if you configure Supabase credentials

---

## Troubleshooting

**Hotkey doesn't work in some apps (e.g. Task Manager)**  
Some elevated (admin) windows block hotkeys from non-admin processes. Try running the app as administrator.

**First transcription is slow**  
The Whisper model starts loading in the background as soon as the app opens. The first transcription may still take a few seconds while the model finishes loading; all subsequent ones are instant.

**App doesn't appear after double-clicking the shortcut**  
Check the system tray — the app runs minimised by default. Click the microphone icon → Open FTC Whisper.

**`install.bat` fails with "Python not found"**  
Re-install Python from [python.org](https://www.python.org/downloads/) and tick **Add Python to PATH**.

**Text not appearing after transcription**  
Make sure a text field is focused before releasing the hotkey. Some apps (e.g. games) block clipboard paste — try switching `inject_method` to `keystrokes` in `config.json`.

**A stray letter appears before the transcribed text (e.g. `vHello world`)**  
This happens in hold mode when you release the modifier key (Alt) before the base key (V). FTC Whisper now suppresses the bare base key during recording so no stray character reaches the target window.

**Waveform bars are flat / not reacting to voice**  
The bars always animate gently even at silence. If they aren't responding to speech, check that Windows has microphone access enabled (**Settings → Privacy → Microphone**) and that the correct input device is selected as your default in Windows Sound settings. You can also force a specific mic by setting `input_device` in `config.json`.

**Wrong microphone used**  
FTC Whisper uses the current Windows default input and automatically falls back across available microphones if one fails. To force a specific mic, set `input_device` in `config.json` to part of the microphone name (e.g. `"USB"`) or a device index from `Recorder.get_input_devices()`.

**Floating badge appears in the wrong place**  
The badge anchors to the caret position after injection. In apps (like Chrome's address bar) that don't expose caret position via the accessibility API, it falls back to where your cursor was when you started recording. Make sure your cursor is near the text field before pressing the hotkey.

**Outlook shows ribbon key tips when using Alt+V**  
FTC Whisper normalises modifier key state on hotkey release to prevent stuck Alt/menu mode in Outlook and other Office editors.

---

## Licence

Internal tool — FTC Safety Solutions. All rights reserved.
