"""Model residency must match the paths users actually exercise.

English dictation uses Parakeet. Eagerly loading the configured accurate
Whisper model as well can consume several GB even though that model is only
used by explicit history Retry or a rare Whisper-fallback upgrade. Those call
Transcriber.transcribe(), which already lazy-loads it.
"""
import inspect
import unittest

from app import WhisperFlowApp
from transcriber import Transcriber


class ModelPreloadTests(unittest.TestCase):
    def test_primary_startup_does_not_preload_accurate_whisper(self):
        src = inspect.getsource(WhisperFlowApp._init_core)
        self.assertNotIn("target=self.transcriber.load_model", src)
        self.assertIn("target=self.fast_transcriber.load_model", src)
        self.assertIn("target=self._init_parakeet", src)

    def test_transcribe_keeps_the_accurate_model_lazy_load_contract(self):
        src = inspect.getsource(Transcriber.transcribe)
        self.assertIn("if self._model is None", src)
        self.assertIn("self.load_model()", src)


if __name__ == "__main__":
    unittest.main()
