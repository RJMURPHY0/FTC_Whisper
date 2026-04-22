"""
Whisper transcription engine wrapper using faster-whisper.
Loads the model once on startup and exposes a simple transcribe() method.
"""

import multiprocessing
import re
import threading
import numpy as np
from faster_whisper import WhisperModel
from typing import Optional


_MODEL_SAMPLE_RATE = 16000


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
    ):
        if model_size not in self.VALID_MODELS:
            print(
                f"[Transcriber] Invalid model '{model_size}', falling back to 'base.en'."
            )
            model_size = "base.en"

        self.model_size = model_size
        self.language = language
        self._model: Optional[WhisperModel] = None
        self._load_lock = threading.Lock()
        # Prevents streaming preview and final transcription running at the same time
        self._transcribe_lock = threading.Lock()

        if device == "auto":
            try:
                import torch

                device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                device = "cpu"

        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"

        self._device = device
        self._compute_type = compute_type
        # Use all physical CPU cores for inference
        self._cpu_threads = max(1, multiprocessing.cpu_count())

        print(
            f"[Transcriber] model={model_size!r}  device={device}  "
            f"compute={compute_type}  threads={self._cpu_threads}"
        )

    def load_model(self) -> None:
        with self._load_lock:
            if self._model is not None:
                return
            print(f"[Transcriber] Loading '{self.model_size}'…")
            self._model = WhisperModel(
                self.model_size,
                device=self._device,
                compute_type=self._compute_type,
                cpu_threads=self._cpu_threads,
                num_workers=1,
            )
            print("[Transcriber] Model ready.")

    def transcribe(
        self, audio: np.ndarray, sample_rate: int = 16000, blocking: bool = True
    ) -> str:
        """
        Transcribe audio to text.

        Args:
            blocking: If False, skip and return "" when another transcription is
                      already running (used by the streaming preview loop).
        """
        if self._model is None:
            self.load_model()

        if audio is None or len(audio) == 0:
            return ""

        if audio.ndim > 1:
            audio = audio.flatten()
        audio = audio.astype(np.float32)
        if sample_rate and sample_rate != _MODEL_SAMPLE_RATE:
            audio = self._resample_audio(audio, sample_rate, _MODEL_SAMPLE_RATE)
            sample_rate = _MODEL_SAMPLE_RATE

        acquired = self._transcribe_lock.acquire(blocking=blocking)
        if not acquired:
            return ""  # streaming preview bails out rather than queuing
        try:
            return self._run(audio, sample_rate)
        finally:
            self._transcribe_lock.release()

    def _run(self, audio: np.ndarray, _sample_rate: int) -> str:
        is_en_model = self.model_size.endswith(".en")
        segments, _ = self._model.transcribe(
            audio,
            language=None if is_en_model else self.language,
            beam_size=1,  # fastest; quality nearly identical for English
            vad_filter=True,
            vad_parameters=dict(
                min_silence_duration_ms=300,
                threshold=0.50,   # raised from 0.30 — filters headset background noise
                speech_pad_ms=100,
            ),
            no_speech_threshold=0.6,   # raised from 0.5 — fewer false positives
            condition_on_previous_text=False,
            temperature=0.0,
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

        # Remove filler words (whole-word matches only, case-insensitive)
        fillers = (
            r"\bum+\b", r"\buh+\b", r"\ber+\b", r"\bhmm+\b", r"\bmhm+\b",
            r"\byou know\b", r"\bI mean\b", r"\blike,?\b", r"\bso,?\b",
            r"\bbasically\b", r"\bliterally\b", r"\bactually\b", r"\bright\?\b",
            r"\bokay so\b", r"\bso yeah\b", r"\byeah so\b", r"\byeah\b",
        )
        for filler in fillers:
            text = re.sub(filler, "", text, flags=re.IGNORECASE)

        # Collapse multiple spaces/commas left by removal
        text = re.sub(r" {2,}", " ", text)
        text = re.sub(r",\s*,", ",", text)
        text = re.sub(r"\s+([.,!?])", r"\1", text)

        # Ensure ends with punctuation
        text = text.strip()
        if text and text[-1] not in ".!?":
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
