"""SupabaseLogger.log_error_event contract.

Fire-and-forget insert into error_events with the fields the super-admin
error log filters on. Pinned here: dict detail passes through as-is (jsonb),
non-dict detail is wrapped, empty context fields are omitted, the signed-in
user is stamped, 'local' is not, and a disabled logger never inserts.
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase_client import SupabaseLogger


class _FakeQuery:
    def __init__(self, sink, table):
        self._sink = sink
        self._table = table

    def insert(self, payload):
        self._payload = payload
        return self

    def execute(self):
        self._sink.append((self._table, self._payload))
        self._sink_event.set()
        return self


class _FakeClient:
    def __init__(self):
        self.inserts = []
        self.event = threading.Event()

    def table(self, name):
        q = _FakeQuery(self.inserts, name)
        q._sink_event = self.event
        return q


class LogErrorEventTests(unittest.TestCase):
    def _logger(self):
        logger = SupabaseLogger("https://example.supabase.co", "anon-key")
        fake = _FakeClient()
        logger.set_client(fake)
        return logger, fake

    def _wait(self, fake):
        self.assertTrue(fake.event.wait(timeout=3.0),
                        "insert thread never ran")
        return fake.inserts

    def test_full_payload(self):
        logger, fake = self._logger()
        logger.set_user("uid-123")
        logger.log_error_event(
            "inject_failed",
            detail={"fg_exe": "chrome.exe", "elevated_target": False},
            app_name="Chrome", app_exe="chrome.exe",
            window_class="Chrome_WidgetWin_1", app_version="1.6.34",
            transcription_created_at="2026-07-28T10:00:00+00:00")
        inserts = self._wait(fake)
        table, payload = inserts[0]
        self.assertEqual(table, "error_events")
        self.assertEqual(payload["event_type"], "inject_failed")
        self.assertEqual(payload["detail"]["fg_exe"], "chrome.exe")
        self.assertEqual(payload["app_name"], "Chrome")
        self.assertEqual(payload["window_class"], "Chrome_WidgetWin_1")
        self.assertEqual(payload["app_version"], "1.6.34")
        self.assertEqual(payload["transcription_created_at"],
                         "2026-07-28T10:00:00+00:00")
        self.assertEqual(payload["user_id"], "uid-123")
        self.assertIn("created_at", payload)

    def test_empty_fields_omitted_and_local_user_unstamped(self):
        logger, fake = self._logger()
        logger.set_user("local")
        logger.log_error_event("mic_silent", detail={"device": "Headset"})
        _, payload = self._wait(fake)[0]
        for absent in ("app_name", "app_exe", "window_class", "app_version",
                       "transcription_created_at", "user_id"):
            self.assertNotIn(absent, payload)

    def test_non_dict_detail_wrapped(self):
        logger, fake = self._logger()
        logger.log_error_event("inject_failed", detail="boom " * 200)
        _, payload = self._wait(fake)[0]
        self.assertIn("message", payload["detail"])
        self.assertLessEqual(len(payload["detail"]["message"]), 500)

    def test_disabled_logger_never_inserts(self):
        logger = SupabaseLogger("", "")
        fake = _FakeClient()
        logger.set_client(fake)
        logger.log_error_event("inject_failed", detail={"x": 1})
        time.sleep(0.2)
        self.assertEqual(fake.inserts, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
