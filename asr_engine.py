"""
Parakeet ASR engine — near-instant, high-accuracy English transcription on CPU.

Wraps NVIDIA Parakeet TDT 0.6b v2 (int8 ONNX) via the pure-Python `onnx-asr`
package (deps: numpy + onnxruntime, both already shipped). On this class of
desktop CPU it transcribes ~20x realtime with punctuation and capitalisation
built in, and — unlike Whisper — its cost scales with audio length (no 30s
encoder padding), so short tails transcribe in ~0.1-0.3s.

Model files (~660 MB) are downloaded once to %LOCALAPPDATA%\\FTC Whisper\\models
using plain HTTPS downloads with atomic renames. The HuggingFace cache is NOT
used: hf_hub's symlink-based cache raises WinError 1314 on stock Windows
(no Developer Mode), which would kill the engine on most installs.

If anything fails (download, load, inference) the app silently falls back to
the faster-whisper pipeline — this engine is strictly additive.
"""

import os
import re
import threading
import urllib.request
from typing import Callable, Optional

import numpy as np

import hallucination

_MODEL_SAMPLE_RATE = 16000

# Model version: "v2" (English, shipped default) or "v3" (multilingual, lower
# WER on the Open ASR leaderboard). Same file layout on the istupakov HF repos,
# and onnx-asr >= 0.11 knows both names natively.
DEFAULT_MODEL_VERSION = "v2"


def _hf_base(version: str = DEFAULT_MODEL_VERSION) -> str:
    return (f"https://huggingface.co/istupakov/parakeet-tdt-0.6b-{version}-onnx"
            "/resolve/main")


# name -> (url path, minimum sane size in bytes)
_MODEL_FILES = {
    "encoder-model.int8.onnx": 600_000_000,
    "decoder_joint-model.int8.onnx": 5_000_000,
    "vocab.txt": 1_000,
    "config.json": 50,
}

_TOTAL_DOWNLOAD_BYTES = 680_000_000  # rough, for progress reporting


def models_dir(version: str = DEFAULT_MODEL_VERSION) -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "FTC Whisper", "models",
                        f"parakeet-tdt-0.6b-{version}-onnx")


def model_files_present(directory: Optional[str] = None,
                        version: str = DEFAULT_MODEL_VERSION) -> bool:
    d = directory or models_dir(version)
    for name, min_size in _MODEL_FILES.items():
        p = os.path.join(d, name)
        try:
            if os.path.getsize(p) < min_size:
                return False
        except OSError:
            return False
    return True


def download_model(
    progress: Optional[Callable[[float, str], None]] = None,
    directory: Optional[str] = None,
    version: str = DEFAULT_MODEL_VERSION,
) -> bool:
    """Download the Parakeet model files. Returns True on success.

    progress(fraction 0..1, message) is called from the downloading thread.
    Partial downloads go to .part files and are atomically renamed, so an
    interrupted download never leaves a truncated file that passes the
    size check.
    """
    d = directory or models_dir(version)
    os.makedirs(d, exist_ok=True)
    done_bytes = 0
    try:
        for name, min_size in _MODEL_FILES.items():
            dest = os.path.join(d, name)
            if os.path.exists(dest) and os.path.getsize(dest) >= min_size:
                done_bytes += os.path.getsize(dest)
                continue
            tmp = dest + ".part"
            url = f"{_hf_base(version)}/{name}"
            req = urllib.request.Request(url, headers={"User-Agent": "FTC-Whisper"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 512)
                    if not chunk:
                        break
                    f.write(chunk)
                    done_bytes += len(chunk)
                    if progress:
                        frac = min(0.99, done_bytes / _TOTAL_DOWNLOAD_BYTES)
                        progress(frac, f"Downloading speech model… {done_bytes // 1_000_000} MB")
            if os.path.getsize(tmp) < min_size:
                raise IOError(f"{name} download truncated ({os.path.getsize(tmp)} bytes)")
            os.replace(tmp, dest)
        if progress:
            progress(1.0, "Speech model ready")
        return True
    except Exception as e:
        print(f"[ParakeetEngine] Model download failed: {e}")
        if progress:
            progress(0.0, "Speech model download failed — using Whisper")
        return False


# Coordinating conjunctions: after a full stop in dictated speech these are
# almost always a pause artefact, not a real new sentence ("I did it. And then"
# was one thought said with a breath in the middle). "Because"/"That"/"Which"
# are deliberately excluded — they legitimately open sentences.
_PAUSE_CONJUNCTIONS = ("And", "But", "So", "Or", "Nor", "Yet")
_CONJ_AFTER_STOP = re.compile(
    r"\.\s+(" + "|".join(_PAUSE_CONJUNCTIONS) + r")\b"
)
# A full stop followed by a lowercase word: no English sentence starts
# lowercase, so the stop is a pause the model mis-heard as a boundary
# ("would actually be. inserted"). Require >=2 letters before the stop so
# single-letter abbreviations (i.e., e.g., a.m.) are left alone. A number or
# percentage is also a valid left-hand side ("set up 100%. correctly") — the
# original [a-z]{2,} could never match one, so those survived untouched.
_STOP_BEFORE_LOWER = re.compile(r"([a-z]{2,}|\d+%?)\.\s+([a-z])")

# A "sentence" that is nothing but a single -ly adverb is always a pause
# artefact, never intentional: "…is set up 100%. Correctly." is one thought the
# speaker paused in the middle of. Measured on 200 real dictations — this shape
# occurs and is wrong every time it occurs.
#
# The adverb must be the WHOLE sentence. "Obviously, we should ask" is a
# perfectly normal sentence opener, so an adverb followed by a comma or by more
# words is left alone; only the standalone case is merged back.
_LONE_ADVERB_SENTENCE = re.compile(r"\.\s+([A-Z][a-z]+ly)\s*([.!?])")


def fix_pause_punctuation(text: str) -> str:
    """Remove the sentence breaks Parakeet stamps at mid-sentence pauses.

    Only the two unambiguous cases are touched, so a real sentence boundary is
    never merged:
      * ". <lowercase>"  -> " <lowercase>"     (a stop can't precede lowercase)
      * ". And/But/So/…" -> ", and/but/so/…"   (coordinating conj. after a stop)

    Capitalised non-conjunction artefacts (". Name", ". That") are left for the
    LLM correction pass, which can read the context. Punctuation is only ever
    downgraded, never invented, and no words change."""
    if not text:
        return text
    # ". Correctly." -> " correctly.": a one-word adverb sentence is a pause the
    # model stamped as a boundary. Runs FIRST because it needs the adverb's own
    # terminator to identify the shape: the conjunction rule below rewrites
    # ". And" to ", and", which would replace that terminator with a comma and
    # leave "100%. Correctly, and then…" unfixable.
    text = _LONE_ADVERB_SENTENCE.sub(
        lambda m: " " + m.group(1).lower() + m.group(2), text)
    # ". And" -> ", and": a comma is the correct join for a clause continuation.
    text = _CONJ_AFTER_STOP.sub(lambda m: ", " + m.group(1).lower(), text)
    # ". inserted" -> " inserted": drop the spurious stop, keep the word.
    text = _STOP_BEFORE_LOWER.sub(r"\1 \2", text)
    # Tidy any artefacts a drop left behind (", ." / " ,." / doubled spaces).
    text = re.sub(r"\s+,\s*\.", ".", text)
    text = re.sub(r",\s*\.", ".", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


class ParakeetTranscriber:
    """Drop-in fast transcriber with the same call surface as Transcriber.

    Parakeet has no initial_prompt/hotwords support, so context_words is
    ignored and hotwords_str is applied as a conservative post-pass: exact
    case-insensitive whole-word matches of custom-vocabulary terms are
    replaced with the user's canonical casing (e.g. "ftc" -> "FTC").
    """

    def __init__(self, auto_punctuate: bool = True, cpu_threads: int = 4,
                 vad_gate: bool = True,
                 model_version: str = DEFAULT_MODEL_VERSION):
        self.auto_punctuate = auto_punctuate
        self._cpu_threads = cpu_threads
        self._vad_gate = vad_gate
        self._vad_failed = False
        self._model = None
        self._model_version = model_version
        self._load_lock = threading.Lock()
        self._transcribe_lock = threading.Lock()
        self._load_failed = False

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def is_available(self) -> bool:
        """True once loaded, or loadable without a download."""
        return self._model is not None or (
            not self._load_failed and model_files_present(version=self._model_version)
        )

    def load_model(self) -> bool:
        """Load the model if the files are present. Never downloads (call
        download_model() from a UI-aware thread for that). Returns success."""
        with self._load_lock:
            if self._model is not None:
                return True
            if self._load_failed:
                return False
            if not model_files_present(version=self._model_version):
                return False
            try:
                import onnxruntime as ort
                import onnx_asr

                # int8 transducer decode is memory-bound: ~4 threads is the
                # sweet spot on desktop CPUs (measured faster than 8 here).
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = self._cpu_threads
                opts.inter_op_num_threads = 1
                ver = self._model_version
                print(f"[ParakeetEngine] Loading parakeet-tdt-0.6b-{ver} int8…")
                self._model = onnx_asr.load_model(
                    f"nemo-parakeet-tdt-0.6b-{ver}",
                    models_dir(ver),
                    quantization="int8",
                    sess_options=opts,
                )
                print("[ParakeetEngine] Model ready.")
                return True
            except Exception as e:
                print(f"[ParakeetEngine] Load failed ({e}) — falling back to Whisper.")
                self._load_failed = True
                self._model = None
                return False

    def transcribe(
        self, audio: np.ndarray, sample_rate: int = 16000, blocking: bool = True,
        context_words: str = "", hotwords_str: str = "", finalize_text: bool = True,
    ) -> str:
        """finalize_text=False skips the forced leading-capital / trailing-period
        polish — used by the streaming session for mid-dictation chunks, which
        may start or end mid-sentence. The session applies polish() once on the
        fully joined text instead."""
        if self._model is None:
            if not self.load_model():
                return ""
        if audio is None or len(audio) == 0:
            return ""

        if audio.ndim > 1:
            audio = audio.flatten()
        audio = audio.astype(np.float32, copy=False)
        # Silence floor: digital zeros / dead-mic hum must never reach the
        # model — on ultra-short constant input the int8 transducer can emit a
        # confident hallucinated word ("Yeah.") instead of nothing. Real
        # speech, even whisper-quiet, peaks well above this.
        if float(np.max(np.abs(audio))) < 0.001:
            return ""
        if sample_rate and sample_rate != _MODEL_SAMPLE_RATE:
            audio = self._resample(audio, sample_rate, _MODEL_SAMPLE_RATE)

        acquired = self._transcribe_lock.acquire(blocking=blocking)
        if not acquired:
            return ""
        try:
            # VAD gate inside the lock so non-blocking caption ticks that bail
            # out above never pay for it.
            if self._vad_gate:
                audio = self._vad_clip(audio)
                if audio is None:
                    return ""  # no speech anywhere in the clip
            text = self._recognize_long(audio)
        except Exception as e:
            print(f"[ParakeetEngine] Inference error: {e}")
            return ""
        finally:
            self._transcribe_lock.release()

        text = self._post_process(text, hotwords_str)
        return self.polish(text) if finalize_text else text

    # -- internals ----------------------------------------------------------

    # VAD gate — the whisper fallback filters non-speech through Silero VAD
    # (vad_filter=True) but Parakeet received the raw capture: steady background
    # noise (TV, traffic, machinery) sails over the peak floor above and the
    # transducer decodes it into a confident, fluent sentence the user never
    # said. Reuse faster-whisper's bundled Silero model so only speech regions
    # reach the model; noise-only clips transcribe to nothing at all.
    # threshold 0.40 is more permissive than the whisper path's 0.45 — quiet
    # real speech must survive the gate; pad 200ms protects word onsets.
    _VAD_OPTS = dict(threshold=0.40, min_speech_duration_ms=50,
                     min_silence_duration_ms=400, speech_pad_ms=200)
    _VAD_JOIN_GAP = 0.15      # silence re-inserted between spliced speech chunks

    def _vad_clip(self, audio: np.ndarray) -> Optional[np.ndarray]:
        """Return only the speech regions of 16 kHz `audio`; None when Silero
        found no speech at all; the input unchanged if VAD is unavailable."""
        if self._vad_failed:
            return audio
        try:
            from faster_whisper.vad import VadOptions, get_speech_timestamps
            chunks = get_speech_timestamps(audio, VadOptions(**self._VAD_OPTS))
        except Exception as e:
            print(f"[ParakeetEngine] VAD unavailable ({e}) — gate disabled.")
            self._vad_failed = True
            return audio
        if not chunks:
            return None
        kept = sum(c["end"] - c["start"] for c in chunks)
        if kept >= 0.9 * len(audio):
            return audio  # nearly all speech — keep natural timing, skip the splice
        gap = np.zeros(int(self._VAD_JOIN_GAP * _MODEL_SAMPLE_RATE), dtype=np.float32)
        parts: list = []
        for c in chunks:
            if parts:
                parts.append(gap)
            parts.append(audio[c["start"]:c["end"]])
        return np.concatenate(parts)

    _CHUNK_SECONDS = 60.0     # split very long clips (encoder attention is O(n^2))
    _CHUNK_SEARCH = 15.0      # search window for the quietest split point

    def _recognize_long(self, audio: np.ndarray) -> str:
        rate = _MODEL_SAMPLE_RATE
        max_len = int(self._CHUNK_SECONDS * rate)
        if len(audio) <= max_len:
            return str(self._model.recognize(audio) or "")

        parts = []
        pos = 0
        while pos < len(audio):
            remaining = audio[pos:]
            if len(remaining) <= max_len:
                parts.append(str(self._model.recognize(remaining) or ""))
                break
            # Split at the quietest 200ms window inside the last _CHUNK_SEARCH
            # seconds of the chunk, so we never cut mid-word.
            search_start = max_len - int(self._CHUNK_SEARCH * rate)
            search = remaining[search_start:max_len]
            win = int(0.2 * rate)
            n_wins = max(1, len(search) // win)
            energies = [
                float(np.mean(np.abs(search[i * win:(i + 1) * win])))
                for i in range(n_wins)
            ]
            best = int(np.argmin(energies))
            split = search_start + best * win + win // 2
            parts.append(str(self._model.recognize(remaining[:split]) or ""))
            pos += split
        return " ".join(p.strip() for p in parts if p.strip())

    # An utterance that is NOTHING but hesitation fillers ("Mm-hmm.", "Hmm,
    # hmm.", "Uh-huh?") is never intentional dictation — it's what the model
    # hallucinates from breath/room noise or a fraction of a second of audio.
    # Suppress it entirely rather than injecting it into the user's document.
    _FILLER_ONLY = re.compile(
        r"^(?:[\s,.!?\-]|(?:m+-?h+m+|h+m+|m+m+|u+h+(?:-?h+u+h*)?|u+m+|mhm+)\b)+$",
        re.IGNORECASE,
    )

    def _post_process(self, text: str, hotwords_str: str = "") -> str:
        text = (text or "").strip()
        if not text:
            return ""
        if self._FILLER_ONLY.match(text):
            print(f"[ParakeetEngine] Filler-only result suppressed: '{text}'")
            return ""

        # Degenerate repetition ("no, no, no, no, …"). An RNN-T decoder can stay
        # on one audio frame and emit the same token until its budget runs out;
        # nothing above catches it, because the silence floor and the VAD gate
        # only decide whether the model RUNS, never whether its output is sane.
        # Runs here (not only in polish()) so a looped chunk is killed before it
        # reaches the streaming session's committed list or a live caption.
        text = hallucination.clean(text, source="parakeet")
        if not text:
            return ""

        # Pure non-word fillers only — never strip real words. Swallow one
        # adjacent comma so "So, um, I think" -> "So, I think" (not "So,, I").
        text = re.sub(
            r"(,\s*)?\b(?:um+|uh+|mhm+)\b\s*,?\s*", r"\1", text, flags=re.IGNORECASE
        )
        text = re.sub(r",\s*,", ",", text)
        text = re.sub(r"(^|[.!?]\s+)[,;\s]+", r"\1", text)  # no comma after sentence start
        text = re.sub(r" {2,}", " ", text)
        text = re.sub(r"\s+([.,!?])", r"\1", text)
        text = text.strip().lstrip(",;: ")

        # Custom vocabulary: canonical casing for exact whole-word matches.
        # Skip terms that collide with everyday English function words — a vocab
        # entry like "IT" (department) or "US" (country) would otherwise re-case
        # every ordinary "it is a test" / "call us today" in the dictation.
        for term in (t.strip() for t in (hotwords_str or "").split(",")):
            if len(term) >= 2 and term.lower() not in self._VOCAB_CASING_STOPLIST:
                text = re.sub(
                    rf"\b{re.escape(term)}\b", term, text, flags=re.IGNORECASE
                )
        return text

    # Common words whose casing must never be forced by the custom vocabulary —
    # the acronym reading ("IT", "US", "AM", "SO"…) is far rarer in dictation
    # than the plain English word, so re-casing corrupts correct text.
    _VOCAB_CASING_STOPLIST = frozenset({
        "it", "us", "in", "is", "at", "am", "an", "as", "be", "by", "do", "go",
        "he", "if", "me", "my", "no", "of", "on", "or", "so", "to", "up", "we",
        "was", "hi", "ok", "all", "and", "the", "for", "but", "not", "you",
    })

    def polish(self, text: str) -> str:
        """Final whole-utterance polish: leading capital + terminal punctuation.
        Applied exactly once per dictation — never per streamed chunk, where a
        forced capital/period corrupts mid-sentence commit boundaries."""
        text = (text or "").strip()
        if not text:
            return ""
        if self.auto_punctuate:
            # Kill the mid-sentence sentence-breaks Parakeet stamps at pauses
            # BEFORE fixing the leading capital / terminal stop, so a dropped
            # boundary can't leave a stray capital behind.
            text = fix_pause_punctuation(text)
        if text and text[0].islower():
            text = text[0].upper() + text[1:]
        if self.auto_punctuate:
            # A trailing comma/semicolon/colon is a pause artefact. Replace it,
            # never stack — appending after it shipped ",." endings.
            if text and text[-1] in ",;:":
                text = text[:-1].rstrip()
            if text and text[-1] not in ".!?":
                text += "."
        return text

    @staticmethod
    def _resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
        if src_rate <= 0 or src_rate == dst_rate or audio.size == 0:
            return audio.astype(np.float32, copy=False)
        dst_len = max(1, int(round(len(audio) * float(dst_rate) / float(src_rate))))
        src_x = np.linspace(0.0, 1.0, num=len(audio), endpoint=False)
        dst_x = np.linspace(0.0, 1.0, num=dst_len, endpoint=False)
        return np.interp(dst_x, src_x, audio.astype(np.float64, copy=False)).astype(
            np.float32
        )
