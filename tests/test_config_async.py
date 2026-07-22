import json
import os
import tempfile
import threading
import unittest

from config import Config


class ConfigAsyncSaveTests(unittest.TestCase):
    def test_async_save_does_not_block_and_latest_snapshot_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.json")
            config = Config()
            config._config_path = path

            entered = threading.Event()
            release = threading.Event()
            original_write = config._write_snapshot

            def blocked_first_write(sequence, data, target):
                if sequence == 1:
                    entered.set()
                    release.wait(2)
                original_write(sequence, data, target)

            config._write_snapshot = blocked_first_write
            config.hotkey = "alt+1"
            config.save_async()

            try:
                self.assertTrue(entered.wait(1), "background writer did not start")
                # The UI-facing call returned even though disk I/O is still held.
                self.assertFalse(os.path.exists(path))

                config.hotkey = "alt+9"
                config.auto_enter = True
                config.save_async()
            finally:
                release.set()

            self.assertTrue(config.flush_async(2))
            with open(path, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual("alt+9", saved["hotkey"])
            self.assertTrue(saved["auto_enter"])

    def test_burst_is_coalesced_into_one_write(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Config()
            config._config_path = os.path.join(folder, "config.json")
            calls = []
            original_write = config._write_snapshot

            def counted_write(sequence, data, target):
                calls.append(sequence)
                original_write(sequence, data, target)

            config._write_snapshot = counted_write
            for index in range(12):
                config.custom_vocabulary = f"term-{index}"
                config.save_async()

            self.assertTrue(config.flush_async(2))
            self.assertEqual(1, len(calls))
            with open(config._config_path, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual("term-11", saved["custom_vocabulary"])


if __name__ == "__main__":
    unittest.main()
