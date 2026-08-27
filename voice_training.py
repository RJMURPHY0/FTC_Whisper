"""
Opt-in voice training: send short dictation snippets to FTC Transcribe so it
learns what the signed-in user sounds like.

WHAT THIS DOES NOT DO BY DEFAULT
--------------------------------
Nothing. Dictation audio stays on this machine exactly as audio_store promises,
until the user switches voice training on. There is no silent upload path: every
send is gated on consent that this module fetches from the server, not on a
local flag a stale build could get wrong.

WHY IT UPLOADS AUDIO RATHER THAN A VOICEPRINT
--------------------------------------------
The embedding could be computed here and only the numbers sent. That would keep
audio local, but it would also bundle a ~90 MB ONNX model into this app, pin the
model version to whatever release the user happens to be running, and leave the
web app with nothing to play back when the user asks "what exactly did you learn
me from?". Uploading the clip keeps one model in one place and makes a future
model change a re-embed on the server rather than a request that every user
re-records. Transcribe's earlier enrolments are unrecoverable for precisely the
opposite reason: it kept the vector and threw the audio away.

WHERE THE CONSENT LIVES
-----------------------
One row in Transcribe, read and written by both apps. That is what makes the
toggle here and the toggle on Transcribe's voice page the same switch instead of
two settings that quietly disagree.
"""

import os
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave

import audio_store

# Transcribe's public origin. Overridable so a dev build can point at localhost.
DEFAULT_BASE_URL = "https://ftctranscribe-phi.vercel.app"

# A clip has to be long enough to carry a voice and short enough to stay a
# sample rather than a recording. Mirrors the server's own bounds, so a clip
# that would be rejected never leaves the machine in the first place.
MIN_CLIP_S = 3.0
MAX_CLIP_S = 45.0

# Ceiling per backfill run, so importing history is a minute, not an evening.
BACKFILL_LIMIT = 12

_HTTP_TIMEOUT = 30.0


def base_url() -> str:
    return (os.environ.get("FTC_TRANSCRIBE_URL") or DEFAULT_BASE_URL).rstrip("/")


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _request(method: str, path: str, token: str, *, json_body=None,
             body: bytes = b"", content_type: str = ""):
    """One authenticated call. Returns (status, decoded_json_or_None)."""
    import json as _json

    data = body
    headers = {"Authorization": f"Bearer {token}"}
    if json_body is not None:
        data = _json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif content_type:
        headers["Content-Type"] = content_type

    req = urllib.request.Request(f"{base_url()}{path}", data=data or None,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            raw = r.read()
            try:
                return r.status, _json.loads(raw.decode("utf-8"))
            except Exception:
                return r.status, None
    except urllib.error.HTTPError as e:
        try:
            return e.code, _json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, None
    except Exception as exc:
        print(f"[VoiceTraining] {method} {path} failed: {exc}")
        return 0, None


def fetch_consent(token: str):
    """True / False as the server has it, or None if it could not be reached.

    None is deliberately distinct from False: 'we do not know' must never be
    rendered as 'the user said no', or a network blip would silently show the
    wrong answer in the settings panel.
    """
    if not token:
        return None
    status, payload = _request("GET", "/api/voice-training/consent", token)
    if status == 200 and isinstance(payload, dict):
        return bool(payload.get("enabled"))
    return None


def push_consent(token: str, enabled: bool) -> bool:
    """Write consent so the web app reflects a change made here."""
    if not token:
        return False
    status, payload = _request("PUT", "/api/voice-training/consent", token,
                               json_body={"enabled": bool(enabled)})
    return status == 200 and isinstance(payload, dict) \
        and bool(payload.get("enabled")) == bool(enabled)


# ── Clip selection ────────────────────────────────────────────────────────────

def clip_duration(path: str) -> float:
    try:
        with wave.open(path, "rb") as wf:
            rate = wf.getframerate() or 1
            return wf.getnframes() / float(rate)
    except Exception:
        return 0.0


def is_usable(path: str) -> bool:
    """Cheap local screen so an unusable clip never costs an upload."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < 8192:
            return False
    except OSError:
        return False
    return MIN_CLIP_S <= clip_duration(path) <= MAX_CLIP_S


def _multipart(path: str, device_label: str, excerpt: str) -> tuple:
    """Build a multipart/form-data body for one WAV clip."""
    boundary = f"----ftcw{uuid.uuid4().hex}"
    sep = f"--{boundary}\r\n".encode()
    parts = []

    def field(name: str, value: str):
        parts.append(sep)
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(value.encode("utf-8"))
        parts.append(b"\r\n")

    if device_label:
        field("deviceLabel", device_label[:40])
    if excerpt:
        field("excerpt", excerpt[:300])

    with open(path, "rb") as f:
        blob = f.read()
    parts.append(sep)
    parts.append(
        f'Content-Disposition: form-data; name="clip"; '
        f'filename="{os.path.basename(path)}"\r\n'
        f"Content-Type: audio/wav\r\n\r\n".encode()
    )
    parts.append(blob)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())

    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def upload_clip(token: str, path: str, *, device_label: str = "",
                excerpt: str = "") -> tuple:
    """Send one clip. Returns (ok, reason).

    reason is 'stored', 'duplicate', 'enough_samples', 'consent_off' or an
    error code, so the caller can stop a backfill early rather than firing
    every remaining clip at a server that has already said no.
    """
    if not token or not is_usable(path):
        return False, "unusable"

    body, content_type = _multipart(path, device_label, excerpt)
    status, payload = _request("POST", "/api/voice-training/sample", token,
                               body=body, content_type=content_type)

    if status == 200 and isinstance(payload, dict):
        if payload.get("stored"):
            return True, "stored"
        return True, str(payload.get("reason") or "skipped")
    if status == 403:
        return False, "consent_off"
    if isinstance(payload, dict) and payload.get("code"):
        return False, str(payload["code"])
    return False, f"http_{status}"


# ── Background sender ─────────────────────────────────────────────────────────

class VoiceTrainer:
    """Owns the consent flag and every upload, off the UI thread.

    Consent is cached after a fetch so the hot dictation path never blocks on
    the network, but the cache starts as 'unknown' and an unknown cache never
    uploads. Failing closed is the only safe direction here.
    """

    def __init__(self, token_provider, device_label: str = "desktop"):
        # A callable, not a token: the desktop session refreshes, and a token
        # captured at construction would be stale within the hour.
        self._token_provider = token_provider
        self._device_label = device_label
        self._consent = None          # None = not yet known
        self._lock = threading.Lock()
        self._backfilling = False

    # -- consent -----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._consent is True

    @property
    def known(self) -> bool:
        with self._lock:
            return self._consent is not None

    def _token(self) -> str:
        try:
            return self._token_provider() or ""
        except Exception:
            return ""

    def refresh_consent(self, callback=None) -> None:
        """Pull the server's answer, so a change made in Transcribe lands here."""
        def _work():
            value = fetch_consent(self._token())
            if value is not None:
                with self._lock:
                    self._consent = value
            if callback:
                try:
                    callback(value)
                except Exception:
                    pass
        threading.Thread(target=_work, daemon=True, name="voice-consent-fetch").start()

    def set_consent(self, enabled: bool, callback=None) -> None:
        """Write the user's choice, so Transcribe reflects a change made here."""
        with self._lock:
            self._consent = bool(enabled)
        def _work():
            ok = push_consent(self._token(), enabled)
            if not ok:
                # The switch did not actually move. Drop back to unknown rather
                # than showing a state the server does not hold.
                with self._lock:
                    self._consent = None
            if callback:
                try:
                    callback(ok)
                except Exception:
                    pass
        threading.Thread(target=_work, daemon=True, name="voice-consent-push").start()

    # -- uploads -----------------------------------------------------------
    def offer(self, created_at: str, excerpt: str = "") -> None:
        """Offer a just-finished dictation. Silent no-op unless opted in."""
        if not self.enabled:
            return
        path = audio_store.find(created_at)
        if not path or not is_usable(path):
            return

        def _work():
            ok, reason = upload_clip(self._token(), path,
                                     device_label=self._device_label,
                                     excerpt=excerpt)
            if reason == "consent_off":
                # The server is the authority. Trust it over the local cache.
                with self._lock:
                    self._consent = False
            elif ok and reason == "stored":
                print("[VoiceTraining] sample accepted")
        threading.Thread(target=_work, daemon=True, name="voice-sample-upload").start()

    def backfill(self, progress=None, limit: int = BACKFILL_LIMIT) -> None:
        """Teach Transcribe from dictations already on this machine.

        The point of this: a voice model change leaves every existing enrolment
        unusable, and re-recording enrolment phrases is a chore that produces
        read-aloud speech nobody actually talks like. The clips already sitting
        in audio_store are the real thing.

        Longest first: a 20-second clip carries far more voice than a 4-second
        one, and the per-person sample ceiling is small.
        """
        with self._lock:
            if self._backfilling:
                return
            self._backfilling = True

        def _report(msg: str):
            if progress:
                try:
                    progress(msg)
                except Exception:
                    pass

        def _work():
            try:
                if not self.enabled:
                    _report("Switch voice training on first.")
                    return
                token = self._token()
                if not token:
                    _report("Sign in first.")
                    return

                try:
                    folder = audio_store.audio_dir()
                    paths = [os.path.join(folder, n) for n in os.listdir(folder)
                             if n.lower().endswith(".wav")]
                except Exception as exc:
                    _report(f"Could not read saved audio: {exc}")
                    return

                usable = [(p, clip_duration(p)) for p in paths]
                usable = [(p, d) for p, d in usable if MIN_CLIP_S <= d <= MAX_CLIP_S]
                usable.sort(key=lambda pair: pair[1], reverse=True)
                if not usable:
                    _report(f"No dictations between {int(MIN_CLIP_S)}s and "
                            f"{int(MAX_CLIP_S)}s to learn from yet.")
                    return

                sent = 0
                for i, (path, _dur) in enumerate(usable[:limit], start=1):
                    _report(f"Sending {i} of {min(len(usable), limit)}…")
                    ok, reason = upload_clip(token, path,
                                             device_label=self._device_label)
                    if reason == "consent_off":
                        with self._lock:
                            self._consent = False
                        _report("Voice training is switched off on the account.")
                        return
                    if reason == "enough_samples":
                        _report(f"Done. {sent} sent, that is plenty to learn from.")
                        return
                    if ok and reason == "stored":
                        sent += 1
                    time.sleep(0.4)  # be a good citizen, this is not a race

                _report(f"Done. {sent} clip{'' if sent == 1 else 's'} sent."
                        if sent else "Nothing new to send, your voice is already learned.")
            finally:
                with self._lock:
                    self._backfilling = False

        threading.Thread(target=_work, daemon=True, name="voice-backfill").start()
