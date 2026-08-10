import json
import os
import tempfile
import unittest

from config import Config, _SHARED_SUPABASE_KEY, _SHARED_SUPABASE_URL


class ConfigBackendMigrationTests(unittest.TestCase):
    def _write_config(self, folder, **values):
        path = os.path.join(folder, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(values, handle)
        return path

    def test_blank_backend_is_repaired_so_saved_session_can_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write_config(folder, supabase_url="", supabase_key="")

            loaded = Config.load(path)

            self.assertEqual(_SHARED_SUPABASE_URL, loaded.supabase_url)
            self.assertEqual(_SHARED_SUPABASE_KEY, loaded.supabase_key)
            with open(path, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(_SHARED_SUPABASE_URL, saved["supabase_url"])
            self.assertEqual(_SHARED_SUPABASE_KEY, saved["supabase_key"])

    def test_shared_backend_with_missing_key_is_repaired(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write_config(
                folder,
                supabase_url=_SHARED_SUPABASE_URL,
                supabase_key="",
            )

            loaded = Config.load(path)

            self.assertEqual(_SHARED_SUPABASE_KEY, loaded.supabase_key)

    def test_custom_backend_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write_config(
                folder,
                supabase_url="https://custom.example",
                supabase_key="custom-publishable-key",
            )

            loaded = Config.load(path)

            self.assertEqual("https://custom.example", loaded.supabase_url)
            self.assertEqual("custom-publishable-key", loaded.supabase_key)


if __name__ == "__main__":
    unittest.main()
