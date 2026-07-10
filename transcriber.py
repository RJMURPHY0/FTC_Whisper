"""
Whisper transcription engine wrapper using faster-whisper.
Loads the model once on startup and exposes a simple transcribe() method.
"""

import multiprocessing
import re
import threading
import numpy as np
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

_MODEL_SAMPLE_RATE = 16000

# Cached once — avoids repeated failed `import torch` on CPU-only machines
_DEVICE_CACHE: str = ""


def _detect_device() -> str:
    global _DEVICE_CACHE
    if _DEVICE_CACHE:
        return _DEVICE_CACHE
    try:
        import torch
        _DEVICE_CACHE = "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        _DEVICE_CACHE = "cpu"
    return _DEVICE_CACHE


class Transcriber:
    VALID_MODELS = [
        "tiny",
        "tiny.en",
        "base",
        "base.en",
        "small",
        "small.en",
        "medium",
        "medium.en",
        "large-v3",
        "large-v3-turbo",
    ]

    def __init__(
        self,
        model_size: str = "base.en",
        language: str = "en",
        device: str = "auto",
        compute_type: str = "auto",
        beam_size: int = 5,
        vad_speech_pad_ms: int = 60,
        min_silence_duration_ms: int = 200,
        cpu_threads: int = 0,
        num_workers: int = 1,
        auto_punctuate: bool = True,
    ):
        if model_size not in self.VALID_MODELS:
            print(
                f"[Transcriber] Invalid model '{model_size}', falling back to 'base.en'."
            )
            model_size = "base.en"

        self.model_size = model_size
        self.language = language
        self._model: Optional["WhisperModel"] = None
        self._load_lock = threading.Lock()
        # Prevents streaming preview and final transcription running at the same time
        self._transcribe_lock = threading.Lock()

        if device == "auto":
            device = _detect_device()

        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"

        self._device = device
        self._compute_type = compute_type
        self._beam_size = beam_size
        self._vad_speech_pad_ms = vad_speech_pad_ms
        self._min_silence_duration_ms = min_silence_duration_ms
        self._num_workers = num_workers
        self._cpu_threads = cpu_threads or max(1, multiprocessing.cpu_count())
        self.auto_punctuate = auto_punctuate

        print(
            f"[Transcriber] model={model_size!r}  device={device}  "
            f"compute={compute_type}  threads={self._cpu_threads}"
        )

    def load_model(self) -> None:
        with self._load_lock:
            if self._model is not None:
                return
            from faster_whisper import WhisperModel  # lazy — called from daemon thread
            print(f"[Transcriber] Loading '{self.model_size}'…")
            self._model = WhisperModel(
                self.model_size,
                device=self._device,
                compute_type=self._compute_type,
                cpu_threads=self._cpu_threads,
                num_workers=self._num_workers,
            )
            print("[Transcriber] Model ready.")

    def transcribe(
        self, audio: np.ndarray, sample_rate: int = 16000, blocking: bool = True,
        context_words: str = "", hotwords_str: str = "",
    ) -> str:
        """
        Transcribe audio to text.

        Args:
            blocking: If False, skip and return "" when another transcription is
                      already running (used by the streaming preview loop).
            context_words: Recent transcription words to include in initial_prompt.
            hotwords_str: Comma-separated terms to boost via Whisper hotwords param.
        """
        if self._model is None:
            self.load_model()

        if audio is None or len(audio) == 0:
            return ""

        if audio.ndim > 1:
            audio = audio.flatten()
        audio = audio.astype(np.float32, copy=False)
        if sample_rate and sample_rate != _MODEL_SAMPLE_RATE:
            audio = self._resample_audio(audio, sample_rate, _MODEL_SAMPLE_RATE)
            sample_rate = _MODEL_SAMPLE_RATE

        acquired = self._transcribe_lock.acquire(blocking=blocking)
        if not acquired:
            return ""  # streaming preview bails out rather than queuing
        try:
            return self._run(audio, sample_rate, context_words=context_words, hotwords_str=hotwords_str)
        finally:
            self._transcribe_lock.release()

    def _run(self, audio: np.ndarray, _sample_rate: int,
             context_words: str = "", hotwords_str: str = "") -> str:
        is_en_model = self.model_size.endswith(".en")
        base_prompt = "Professional business conversation. Clear dictation."
        # Custom vocabulary is passed via the dedicated `hotwords=` param below;
        # do NOT also flatten it into initial_prompt — that polluted the prompt
        # with comma-stripped term soup on every call and hurt accuracy.
        parts = [p for p in [context_words, base_prompt] if p]
        prompt = " ".join(parts)
        segments, _ = self._model.transcribe(
            audio,
            language=None if is_en_model else self.language,
            beam_size=self._beam_size,
            vad_filter=True,
            vad_parameters=dict(
                min_silence_duration_ms=self._min_silence_duration_ms,
                threshold=0.45,
                speech_pad_ms=self._vad_speech_pad_ms,
            ),
            no_speech_threshold=0.7,
            condition_on_previous_text=False,
            temperature=[0.0],        # list disables temperature fallback retries entirely
            repetition_penalty=1.1,   # discourages looping/repeated phrases
            initial_prompt=prompt,
            hotwords=hotwords_str or None,
        )

        text = "".join(s.text for s in segments)
        text = self._post_process(text)
        print(f"[Transcriber] '{text}'")
        return text

    def _post_process(self, text: str) -> str:
        text = text.strip()
        for artifact in (
            "[BLANK_AUDIO]",
            "[MUSIC]",
            "[SOUND]",
            "[NOISE]",
            "[INAUDIBLE]",
            "(music)",
            "(silence)",
            "(Silence)",
            "(applause)",
            "...",
            "♪",
            "Thank you for watching.",
            "Thank you for watching!",
            "Thanks for watching.",
            "Thanks for watching!",
            "Please subscribe.",
            "Subtitles by",
            "Transcribed by",
        ):
            text = text.replace(artifact, "")
        # Strip lines that are just whitespace/punctuation after artifact removal
        text = text.strip(" \t\n.,!")
        # Whisper sometimes outputs a lone full stop on silence — discard it
        if text in {".", "!", "?", ","}:
            text = ""
        text = text.strip()
        if text and text[0].islower():
            text = text[0].upper() + text[1:]

        # Remove only pure non-word fillers (sounds with no semantic meaning).
        # Do NOT strip words like "like", "so", "actually", "yeah" — the user
        # may have said them intentionally and removing them corrupts the text.
        # "er" alone is a hesitation; "err" is a real verb ("to err is human"),
        # so only the single-r form is stripped.
        fillers = (
            r"\bum+\b", r"\buh+\b", r"\ber\b", r"\berm+\b", r"\bhmm+\b", r"\bmhm+\b",
        )
        for filler in fillers:
            text = re.sub(filler, "", text, flags=re.IGNORECASE)

        # Collapse multiple spaces/commas left by removal
        text = re.sub(r" {2,}", " ", text)
        text = re.sub(r",\s*,", ",", text)
        text = re.sub(r"\s+([.,!?])", r"\1", text)

        # Ensure ends with punctuation (only when auto_punctuate is enabled)
        text = text.strip()
        if self.auto_punctuate and text and text[-1] not in ".!?":
            text += "."

        # Re-capitalise first letter after cleanup
        if text and text[0].islower():
            text = text[0].upper() + text[1:]

        return text

    @staticmethod
    def _resample_audio(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
        if src_rate <= 0 or dst_rate <= 0 or audio.size == 0:
            return audio.astype(np.float32, copy=False)
        if src_rate == dst_rate:
            return audio.astype(np.float32, copy=False)

        src_len = int(audio.shape[0])
        dst_len = max(1, int(round(src_len * (float(dst_rate) / float(src_rate)))))
        src_x = np.linspace(0.0, 1.0, num=src_len, endpoint=False, dtype=np.float64)
        dst_x = np.linspace(0.0, 1.0, num=dst_len, endpoint=False, dtype=np.float64)
        resampled = np.interp(dst_x, src_x, audio.astype(np.float64, copy=False))
        print(f"[Transcriber] Resampled audio {src_rate} Hz -> {dst_rate} Hz")
        return resampled.astype(np.float32, copy=False)

    @property
    def is_loaded(self) -> bool:
        return self._model is not None
