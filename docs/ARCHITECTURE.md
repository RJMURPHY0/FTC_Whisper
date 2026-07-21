# FTC Whisper — Architecture

Windows push-to-talk dictation. Hold a hotkey, speak, release — the transcribed text
appears in whatever app has focus. Python + tkinter, shipped as a single signed
`FTC-Whisper.exe`.

The whole design serves one goal: **stop-latency stays flat no matter how long you
talk**. Press, speak, release, text. Everything below exists to protect that.

---

## System at a glance

```mermaid
flowchart TD
    User(["User holds hotkey"]) --> HK[hotkey_manager.py<br/>Win32 RegisterHotKey]
    HK --> APP[app.py<br/>WhisperFlowApp<br/>orchestrator]

    APP --> REC[recorder.py<br/>warm mic + pre-roll ring]
    REC --> SS[stream_session.py<br/>StreamingSession]

    SS --> PK[asr_engine.py<br/>Parakeet TDT<br/>int8 ONNX]
    SS -.fallback.-> WH[transcriber.py<br/>faster-whisper]

    PK --> INJ[injector.py<br/>text injection]
    WH --> INJ
    INJ --> Target(["Focused app<br/>browser · Office · native"])

    APP --> POP[popup.py<br/>FloatingPopup<br/>never takes focus]
    APP --> AI[ai_refiner.py<br/>LLM context fix]
    APP --> UPD[updater.py<br/>auto-update]
    APP --> AUTH[auth.py + supabase_client.py<br/>optional]

    classDef core fill:#16a34a,stroke:#14532d,color:#fff
    classDef asr fill:#0ea5e9,stroke:#075985,color:#fff
    classDef edge fill:#64748b,stroke:#334155,color:#fff
    class APP,REC,SS core
    class PK,WH asr
    class POP,AI,UPD,AUTH edge
```

**Green** is the hot path — anything on it affects stop-latency.
**Blue** is speech recognition. **Grey** is off the critical path.

---

## The dictation sequence

This is the flow that matters. Note where `finalize()` sits: it transcribes only the
**tail**, because everything before the last silence boundary was already committed
while the user was still speaking.

```mermaid
sequenceDiagram
    actor User
    participant HK as hotkey_manager
    participant App as WhisperFlowApp
    participant Rec as recorder
    participant SS as StreamingSession
    participant ASR as Parakeet
    participant Inj as injector

    User->>HK: press and hold hotkey
    HK->>App: _on_start_recording
    App->>Rec: start "warm mic already open"
    Note over Rec: 1.5s pre-roll ring means<br/>the first syllable survives
    App->>SS: new StreamingSession

    loop while held
        Rec-->>SS: audio frames
        alt uncommitted audio > 10s
            SS->>ASR: transcribe to silence boundary
            ASR-->>SS: committed text
            SS->>Rec: drop_audio_before "release memory"
        end
        SS-->>App: caption update
    end

    User->>HK: release
    HK->>App: _on_stop_recording
    App->>SS: finalize
    SS->>ASR: transcribe TAIL ONLY
    ASR-->>SS: final text
    SS-->>App: full transcript
    App->>Inj: inject
    Inj-->>User: text appears "~0.3-0.8s"
```

That commit-as-you-go loop is why a 30-second dictation stops just as fast as a
3-second one.

---

## Choosing an engine

```mermaid
flowchart LR
    S([Dictation ends]) --> Q1{English?}
    Q1 -- no --> W[faster-whisper]
    Q1 -- yes --> Q2{Parakeet model<br/>downloaded?}
    Q2 -- no --> W
    Q2 -- yes --> Q3{Model loaded<br/>without error?}
    Q3 -- no --> W
    Q3 -- yes --> P[Parakeet TDT 0.6b<br/>~20x realtime on CPU<br/>punctuation built in]

    P --> CTX[context_fix<br/>LLM corrector]
    W --> UP[background upgrade pass]
    UP --> CTX
    CTX --> OUT([Injected text])

    classDef good fill:#16a34a,stroke:#14532d,color:#fff
    classDef alt fill:#f59e0b,stroke:#78350f,color:#fff
    class P good
    class W alt
```

Parakeet is primary for English because it beats whisper-large-v3 on accuracy at a
fraction of the cost. `context_fix` is **word-count guarded** — if the LLM adds or
drops words, the result is rejected. Output stays exactly what was said.

---

## Injection — three strategies, in order

The injector tries each until one works. All modifiers are released first, otherwise
Office interprets the paste as "Paste Special".

```mermaid
flowchart TD
    T([Text ready]) --> F[_focus_window<br/>restore Win32 focus]
    F --> C{Clipboard set<br/>succeeded?}
    C -- yes --> CV[Ctrl+V]
    C -- no --> SKIP[NEVER send Ctrl+V<br/>would paste stale clipboard]
    SKIP --> VK
    CV --> OK([Text in app])
    CV -. failed .-> VK[VK_PACKET SendInput<br/>browsers]
    VK -. failed .-> WM[WM_CHAR PostMessage<br/>native apps]
    VK --> OK
    WM --> OK

    classDef danger fill:#dc2626,stroke:#7f1d1d,color:#fff
    class SKIP danger
```

> The red box is a shipped-bug guard. Sending `Ctrl+V` after a failed clipboard write
> pastes whatever was there before — possibly a password — and reports success.

---

## Module dependency map

Generated from the actual import graph, not hand-drawn. `app.py` is the hub: it
imports every subsystem and owns the lifecycle.

```mermaid
flowchart LR
    app[app.py] --> updater
    app --> injector
    app --> hotkey_manager
    app --> asr_engine
    app --> transcriber
    app --> stream_session
    app --> recorder
    app --> popup
    app --> ai_refiner
    app --> auth
    app --> supabase_client
    app --> config
    app --> tray
    app --> feedback
    app --> app_window
    app --> error_reporter

    app_window --> login_window
    app_window --> app_icons
    app_window --> logo_cache
    app_window --> updater
    popup --> app_window
    popup --> logo_cache
    login_window --> logo_cache
    feedback --> error_reporter
    error_reporter --> config

    classDef hub fill:#16a34a,stroke:#14532d,color:#fff
    class app hub
```

---

## Threading model

tkinter owns the main thread. Everything else is a daemon thread — which is why the
UI rules below are absolute.

```mermaid
flowchart TD
    MAIN[Main thread<br/>AppWindow.run → mainloop] 
    MAIN -.spawns.-> T1[hotkey listener]
    MAIN -.spawns.-> T2[recording]
    MAIN -.spawns.-> T3[transcription]
    MAIN -.spawns.-> T4[upgrade pass]
    MAIN -.spawns.-> T5[Supabase logging]
    MAIN -.spawns.-> T6[mic-watchdog]

    T1 & T2 & T3 & T4 & T5 & T6 -->|root.after 0| MAIN

    classDef main fill:#16a34a,stroke:#14532d,color:#fff
    class MAIN main
```

**Never call a tkinter widget from a background thread.** Always marshal back with
`self._root.after(0, lambda: ...)`, wrapped in try/except — `TclError` and
`RuntimeError` both fire during shutdown.

---

## Update flow

Fully automatic, because users never checked manually.

```mermaid
flowchart TD
    CHK[6-hourly check<br/>GitHub releases/latest] --> A{Asset named<br/>exactly FTC-Whisper.exe?}
    A -- no --> STOP([No update])
    A -- yes --> N{is_newer<br/>tuple compare}
    N -- no --> STOP
    N -- yes --> DL[Download to LOCALAPPDATA<br/>3 attempts, backoff]
    DL --> V[verify_exe<br/>MZ header + ≥5MB + Content-Length]
    V --> IDLE{_safe_to_restart<br/>IDLE, >120s since dictation<br/>6 x 5s polls}
    IDLE -- no --> IDLE
    IDLE -- yes --> SWAP[Hidden PowerShell swap script<br/>CREATE_NO_WINDOW]
    SWAP --> DONE([Restarted on new version])

    classDef warn fill:#f59e0b,stroke:#78350f,color:#fff
    class SWAP warn
```

> The swap script must spawn with `CREATE_NO_WINDOW` and **never** `DETACHED_PROCESS`
> — the two conflict, powershell exits 0 without running, and every in-app update
> silently breaks. This shipped as a real bug through v1.6.3.

---

## Key files

| File | Role |
|---|---|
| `app.py` | Orchestrator — `WhisperFlowApp`, `APP_VERSION` |
| `app_window.py` | Dashboard and settings UI |
| `popup.py` | `FloatingPopup` — borderless, topmost, never takes focus |
| `recorder.py` | Warm mic, pre-roll ring, watchdog |
| `stream_session.py` | Incremental Parakeet, commit-as-you-go |
| `asr_engine.py` | Parakeet TDT int8 ONNX (primary) |
| `transcriber.py` | faster-whisper (fallback) |
| `injector.py` | Three-strategy text injection |
| `hotkey_manager.py` | Win32 `RegisterHotKey` + `keyboard` fallback |
| `ai_refiner.py` | OpenRouter → Anthropic refine |
| `updater.py` | Download, verify, idle-wait, swap |
| `config.py` | `Config` dataclass → `config.json` |
| `ftc_whisper.spec` | PyInstaller build (keys sanitised at build time) |

---

## Explore it yourself

This repo has a queryable knowledge graph built from the AST — 793 nodes, 1469 edges,
no LLM involved.

```bash
graphify explain "StreamingSession"       # what it connects to
graphify path "WhisperFlowApp" "Injector" # how two things relate
graphify query "how does stop latency stay flat?"
```

Open `graphify-out/graph.html` in a browser for the interactive version.
Rebuild after big changes with `/graphify .` — it is free and fully local for code.

---

## Where the rules live

`CLAUDE.md` in the repo root carries the **invariants** — each one encodes a bug that
already shipped. Read it before changing the audio, injection, or update paths. This
document is the map; that one is the minefield.
