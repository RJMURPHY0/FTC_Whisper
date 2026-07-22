import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import supabase_client


class _MissingColumnError(RuntimeError):
    code = "42703"

    def __init__(self, column="app_name"):
        self.message = f"column transcriptions.{column} does not exist"
        super().__init__(self.message)


class _FakeQuery:
    def __init__(self, client):
        self.client = client
        self.columns = ""
        self.owner = None
        self.payload = None

    def select(self, columns):
        self.columns = columns
        return self

    def insert(self, payload):
        self.payload = dict(payload)
        return self

    def eq(self, field, value):
        if field == "user_id":
            self.owner = value
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        if self.payload is not None:
            self.client.insert_calls.append(self.payload)
            if self.client.insert_errors:
                raise self.client.insert_errors.pop(0)
            if (self.client.reject_app_inserts
                    and ("app_name" in self.payload or "app_exe" in self.payload)):
                raise _MissingColumnError("app_name")
            return SimpleNamespace(data=[self.payload])

        self.client.select_calls.append((self.columns, self.owner))
        if self.client.select_errors:
            raise self.client.select_errors.pop(0)
        if self.client.reject_app_selects and "app_name" in self.columns:
            raise _MissingColumnError("app_name")
        return SimpleNamespace(data=[dict(row) for row in self.client.rows])


class _FakeClient:
    def __init__(self, rows=(), *, reject_app_selects=False,
                 reject_app_inserts=False):
        self.rows = list(rows)
        self.reject_app_selects = reject_app_selects
        self.reject_app_inserts = reject_app_inserts
        self.select_calls = []
        self.select_errors = []
        self.insert_calls = []
        self.insert_errors = []

    def table(self, _name):
        return _FakeQuery(self)


class _BlockingQuery(_FakeQuery):
    def execute(self):
        self.client.entered.set()
        if not self.client.release.wait(2):
            raise TimeoutError("test did not release query")
        return super().execute()


class _BlockingClient(_FakeClient):
    def __init__(self, rows=()):
        super().__init__(rows)
        self.entered = threading.Event()
        self.release = threading.Event()

    def table(self, _name):
        return _BlockingQuery(self)


class SupabaseHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.history_path = os.path.join(self.temp.name, "history.json")
        self.tombstone_path = os.path.join(
            self.temp.name, "history-tombstones.json")
        self.patches = (
            mock.patch.object(
                supabase_client, "_local_history_path",
                side_effect=lambda: self.history_path),
            mock.patch.object(
                supabase_client, "_tombstones_path",
                side_effect=lambda: self.tombstone_path),
        )
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()

    def _write_history(self, rows):
        with open(self.history_path, "w", encoding="utf-8") as handle:
            json.dump(rows, handle)

    def _read_history(self):
        with open(self.history_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_log_uses_one_timestamp_for_local_and_remote(self):
        logger = supabase_client.SupabaseLogger("https://example.test", "key")
        logger.set_user("user-a")
        payloads = []
        logger._run = lambda payload: payloads.append(dict(payload))

        logger.log_transcription(
            "hello", app_name="Claude", app_exe=r"C:\Apps\Claude.exe")

        local = self._read_history()[0]
        self.assertEqual(local["created_at"], payloads[0]["created_at"])
        self.assertEqual("user-a", local["user_id"])
        self.assertEqual("user-a", payloads[0]["user_id"])
        self.assertEqual("Claude", local["app_name"])
        self.assertEqual(r"C:\Apps\Claude.exe", payloads[0]["app_exe"])

    def test_refinement_payload_accepts_app_metadata(self):
        logger = supabase_client.SupabaseLogger("https://example.test", "key")
        logger.set_user("user-a")
        payloads = []
        logger._run = lambda payload: payloads.append(dict(payload))

        logger.log_refinement(
            "before", "after", "replace",
            app_name="Outlook", app_exe=r"C:\Apps\Outlook.exe",
        )

        self.assertEqual("Outlook", payloads[0]["app_name"])
        self.assertEqual(r"C:\Apps\Outlook.exe", payloads[0]["app_exe"])
        self.assertEqual("user-a", payloads[0]["user_id"])

    def test_transient_insert_failure_does_not_disable_app_metadata(self):
        client = _FakeClient()
        client.insert_errors.append(RuntimeError("temporary network timeout"))
        logger = supabase_client.SupabaseLogger("https://example.test", "key")
        logger.set_client(client)
        payload = {
            "transcribed_text": "hello",
            "app_name": "Claude",
            "app_exe": r"C:\Apps\Claude.exe",
        }

        logger._insert(payload)
        self.assertIsNone(logger._app_columns_supported)
        logger._insert(payload)

        self.assertEqual(2, len(client.insert_calls))
        self.assertTrue(all("app_name" in call for call in client.insert_calls))
        self.assertTrue(logger._app_columns_supported)

    def test_confirmed_legacy_insert_retries_without_app_metadata(self):
        client = _FakeClient(reject_app_inserts=True)
        logger = supabase_client.SupabaseLogger("https://example.test", "key")
        logger.set_client(client)

        logger._insert({
            "transcribed_text": "hello",
            "app_name": "Claude",
            "app_exe": r"C:\Apps\Claude.exe",
        })

        self.assertEqual(2, len(client.insert_calls))
        self.assertIn("app_name", client.insert_calls[0])
        self.assertNotIn("app_name", client.insert_calls[1])
        self.assertFalse(logger._app_columns_supported)

    def test_transient_select_failure_does_not_cache_legacy_shape(self):
        client = _FakeClient()
        client.select_errors.append(RuntimeError("temporary gateway failure"))
        logger = supabase_client.SupabaseLogger("https://example.test", "key")
        logger.set_client(client)
        logger.set_user("user-a")

        logger.refresh_history(user_id="user-a")

        self.assertEqual(1, len(client.select_calls))
        self.assertIsNone(logger._history_select_cols)
        self.assertIsNone(logger._app_columns_supported)

    def test_fetch_is_local_first_then_publishes_merged_remote(self):
        match_time = "2026-07-22T12:00:00+00:00"
        self._write_history([
            {
                "transcribed_text": "local only",
                "created_at": "2026-07-22T12:01:00+00:00",
                "app_name": "Outlook",
                "app_exe": r"C:\Apps\Outlook.exe",
                "user_id": "user-a",
            },
            {
                "transcribed_text": "matched",
                "created_at": match_time,
                "app_name": "Chrome",
                "app_exe": r"C:\Apps\Chrome.exe",
                "user_id": "user-a",
            },
            {
                "transcribed_text": "other account",
                "created_at": "2026-07-22T12:02:00+00:00",
                "app_name": "Secret",
                "app_exe": "secret.exe",
                "user_id": "user-b",
            },
            {
                "transcribed_text": "legacy unowned",
                "created_at": "2026-07-22T12:03:00+00:00",
                "app_name": "Legacy",
                "app_exe": "legacy.exe",
            },
        ])
        client = _FakeClient([
            {
                "id": 1,
                "transcribed_text": "matched",
                "created_at": match_time,
                "app_name": "Claude",
                "app_exe": "",
            },
            {
                "id": 2,
                "transcribed_text": "remote only",
                "created_at": "2026-07-22T11:59:00+00:00",
                "app_name": "Teams",
                "app_exe": r"C:\Apps\Teams.exe",
            },
        ])
        logger = supabase_client.SupabaseLogger("https://example.test", "key")
        logger.set_client(client)
        logger.set_user("user-a")
        refreshed = []
        ready = threading.Event()

        def listener(items):
            refreshed.append(items)
            ready.set()

        logger.add_history_listener(listener)
        immediate = logger.fetch_history(limit=100)

        self.assertEqual(
            {"matched", "local only"},
            {row["transcribed_text"] for row in immediate},
        )
        self.assertTrue(ready.wait(2), "remote refresh was not published")
        final = refreshed[-1]
        self.assertEqual(
            {"matched", "local only", "remote only"},
            {row["transcribed_text"] for row in final},
        )
        matched = next(row for row in final
                       if row["transcribed_text"] == "matched")
        self.assertEqual("Claude", matched["app_name"])
        self.assertEqual(r"C:\Apps\Chrome.exe", matched["app_exe"])

    def test_successful_legacy_select_shape_is_reused(self):
        client = _FakeClient(reject_app_selects=True)
        logger = supabase_client.SupabaseLogger("https://example.test", "key")
        logger.set_client(client)
        logger.set_user("user-a")

        logger.refresh_history(user_id="user-a")
        logger.refresh_history(user_id="user-a")

        self.assertEqual(4, len(client.select_calls))
        self.assertEqual(
            "transcribed_text, refined_text, created_at",
            client.select_calls[-1][0],
        )
        self.assertFalse(logger._app_columns_supported)

    def test_enrichment_fills_fields_independently_without_text_only_guessing(self):
        logger = supabase_client.SupabaseLogger("", "")
        local = [
            {
                "transcribed_text": "first",
                "created_at": "2026-07-22T12:00:00+00:00",
                "app_name": "Chrome",
                "app_exe": "chrome.exe",
            },
            {
                "transcribed_text": "second",
                "created_at": "2026-07-22T12:01:00+00:00",
                "app_name": "Outlook",
                "app_exe": "outlook.exe",
            },
            {
                "transcribed_text": "repeated",
                "created_at": "2026-07-22T12:02:11+00:00",
                "app_name": "Teams",
                "app_exe": "teams.exe",
            },
        ]
        remote = [
            {
                "transcribed_text": "first",
                "created_at": "2026-07-22T12:00:00+00:00",
                "app_name": "Claude",
                "app_exe": "",
            },
            {
                "transcribed_text": "second",
                "created_at": "2026-07-22T12:01:00+00:00",
                "app_name": "",
                "app_exe": "remote.exe",
            },
            {
                "transcribed_text": "repeated",
                "created_at": "2026-07-22T12:02:00+00:00",
                "app_name": "",
                "app_exe": "",
            },
        ]

        enriched = logger._enrich_from_local(remote, local)

        self.assertEqual(("Claude", "chrome.exe"),
                         (enriched[0]["app_name"], enriched[0]["app_exe"]))
        self.assertEqual(("Outlook", "remote.exe"),
                         (enriched[1]["app_name"], enriched[1]["app_exe"]))
        self.assertFalse(enriched[2].get("app_name"))
        self.assertFalse(enriched[2].get("app_exe"))
        merged = logger._merge_history(remote[2:], local[2:], 10)
        self.assertEqual(2, len(merged))

    def test_slow_old_account_refresh_never_notifies_new_account(self):
        client = _BlockingClient([{
            "id": 7,
            "transcribed_text": "account a remote",
            "created_at": "2026-07-22T12:00:00+00:00",
            "app_name": "Claude",
            "app_exe": "claude.exe",
        }])
        logger = supabase_client.SupabaseLogger("https://example.test", "key")
        logger.set_client(client)
        logger.set_user("user-a")
        snapshots = []
        logger.add_history_listener(lambda items: snapshots.append(items))

        logger.refresh_history_async()
        self.assertTrue(client.entered.wait(1))
        logger.set_user("user-b")
        snapshots.clear()
        client.release.set()

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with logger._history_lock:
                if "user-a" not in logger._history_refreshing:
                    break
            time.sleep(0.01)
        self.assertFalse(any(
            row.get("transcribed_text") == "account a remote"
            for snapshot in snapshots for row in snapshot
        ))
        self.assertEqual([], logger.get_cached_history())

    def test_clear_only_removes_current_accounts_local_rows(self):
        self._write_history([
            {"transcribed_text": "a", "created_at": "2026-07-22T12:00:00+00:00",
             "user_id": "user-a"},
            {"transcribed_text": "b", "created_at": "2026-07-22T12:00:00+00:00",
             "user_id": "user-b"},
            {"transcribed_text": "offline", "created_at": "2026-07-22T12:00:00+00:00"},
        ])
        logger = supabase_client.SupabaseLogger("", "")
        logger.set_user("user-a")

        logger.clear_history()

        remaining = self._read_history()
        self.assertEqual({"b", "offline"},
                         {row["transcribed_text"] for row in remaining})
        with open(self.tombstone_path, "r", encoding="utf-8") as handle:
            stones = json.load(handle)
        self.assertEqual("user-a", stones[-1]["user_id"])
        logger.set_user("user-b")
        self.assertEqual(["b"], [row["transcribed_text"]
                                 for row in logger.get_cached_history()])
        logger.set_user(None)
        self.assertEqual(["offline"], [row["transcribed_text"]
                                       for row in logger.get_cached_history()])


if __name__ == "__main__":
    unittest.main()
