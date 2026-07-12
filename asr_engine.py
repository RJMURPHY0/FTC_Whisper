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

_MODEL_SAMPLE_RATE = 16000

_HF_BASE = "https://huggingface.co/istupakov/parakeet-tdt-0.6b-v2-onnx/resolve/main"

# name -> (url path, minimum sane size in bytes)
_MODEL_FILES = {
    "encoder-model.int8.onnx": 600_000_000,
    "decoder_joint-model.int8.onnx": 5_000_000,
    "vocab.txt": 1_000,
    "config.json": 50,
}

_TOTAL_DOWNLOAD_BYTES = 680_000_000  # rough, for progress reporting


def models_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "FTC Whisper", "models", "parakeet-tdt-0.6b-v2-onnx")


def model_files_present(directory: Optional[str] = None) -> bool:
    d = directory or models_dir()
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
) -> bool:
    """Download the Parakeet model files. Returns True on success.

    progress(fraction 0..1, message) is called from the downloading thread.
    Partial downloads go to .part files and are atomically renamed, so an
    interrupted download never leaves a truncated file that passes the
    size check.
    """
    d = directory or models_dir()
    os.makedirs(d, exist_ok=True)
    done_bytes = 0
    try:
        for name, min_size in _MODEL_FILES.items():
            dest = os.path.join(d, name)
            if os.path.exists(dest) and os.path.getsize(dest) >= min_size:
                done_bytes += os.path.getsize(dest)
                continue
            tmp = dest + ".part"
            url = f"{_HF_BASE}/{name}"
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


class ParakeetTranscriber:
    """Drop-in fast transcriber with the same call surface as Transcriber.

    Parakeet has no initial_prompt/hotwords support, so context_words is
    ignored and hotwords_str is applied as a conservative post-pass: exact
    case-insensitive whole-word matches of custom-vocabulary terms are
    replaced with the user's canonical casing (e.g. "ftc" -> "FTC").
    """

    def __init__(self, auto_punctuate: bool = True, cpu_threads: int = 4):
        self.auto_punctuate = auto_punctuate
        self._cpu_threads = cpu_threads
        self._model = None
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
            not self._load_failed and model_files_present()
        )

    def load_model(self) -> bool:
        """Load the model if the files are present. Never downloads (call
        download_model() from a UI-aware thread for that). Returns success."""
        with self._load_lock:
            if self._model is not None:
                return True
            if self._load_failed:
                return False
            if not model_files_present():
                return False
            try:
                import onnxruntime as ort
                import onnx_asr

                # int8 transducer decode is memory-bound: ~4 threads is the
                # sweet spot on desktop CPUs (measured faster than 8 here).
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = self._cpu_threads
                opts.inter_op_num_threads = 1
                print("[ParakeetEngine] Loading parakeet-tdt-0.6b-v2 int8…")
                self._model = onnx_asr.load_model(
                    "nemo-parakeet-tdt-0.6b-v2",
                    models_dir(),
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
            text = self._recognize_long(audio)
        except Exception as e:
            print(f"[ParakeetEngine] Inference error: {e}")
            return ""
        finally:
            self._transcribe_lock.release()

        text = self._post_process(text, hotwords_str)
        return self.polish(text) if finalize_text else text

    # -- internals ----------------------------------------------------------

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
        if text[0].islower():
            text = text[0].upper() + text[1:]
        if self.auto_punctuate and text[-1] not in ".!?":
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
