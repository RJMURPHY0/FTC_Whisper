"""Per-account vocabulary/snippet storage, sync merge, and legacy migration."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vocab_store as vs  # noqa: E402


class FakeConfig:
    """Just enough Config: the two dict fields, the legacy string, and a
    save_async that records rather than touching disk."""

    def __init__(self, custom_vocabulary=""):
        self.vocabulary = {}
        self.snippets = {}
        self.custom_vocabulary = custom_vocabulary
        self.saves = 0

    def save_async(self):
        self.saves += 1


RYAN = "Ryan.Murphy@ftc.co.uk"
JAY = "jay@ftc.co.uk"


class AccountScopingTests(unittest.TestCase):

    def test_entries_are_scoped_per_account(self):
        cfg = FakeConfig()
        vs.upsert(cfg, vs.VOCABULARY, vs.new_entry(term="Pipedrive"), RYAN)
        vs.upsert(cfg, vs.VOCABULARY, vs.new_entry(term="Xero"), JAY)

        self.assertEqual(["Pipedrive"],
                         [e["term"] for e in vs.load(cfg, vs.VOCABULARY, RYAN)])
        self.assertEqual(["Xero"],
                         [e["term"] for e in vs.load(cfg, vs.VOCABULARY, JAY)])

    def test_the_account_key_is_case_and_space_insensitive(self):
        cfg = FakeConfig()
        vs.upsert(cfg, vs.VOCABULARY, vs.new_entry(term="Pipedrive"), RYAN)
        for variant in ("ryan.murphy@ftc.co.uk", "  RYAN.MURPHY@FTC.CO.UK  "):
            self.assertEqual(1, len(vs.load(cfg, vs.VOCABULARY, variant)))

    def test_signed_out_entries_land_under_the_local_key(self):
        cfg = FakeConfig()
        vs.upsert(cfg, vs.VOCABULARY, vs.new_entry(term="Pipedrive"), None)
        self.assertIn(vs.LOCAL_KEY, cfg.vocabulary)
        self.assertEqual(1, len(vs.load(cfg, vs.VOCABULARY, None)))

    def test_signing_in_adopts_entries_added_while_signed_out(self):
        cfg = FakeConfig()
        vs.upsert(cfg, vs.VOCABULARY, vs.new_entry(term="Pipedrive"), None)
        vs.upsert(cfg, vs.SNIPPETS,
                  vs.new_entry(trigger="my email", body="r@ftc.co.uk"), None)

        moved = vs.adopt_local_entries(cfg, RYAN)

        self.assertEqual(2, moved)
        self.assertEqual(1, len(vs.load(cfg, vs.VOCABULARY, RYAN)))
        self.assertEqual(1, len(vs.load(cfg, vs.SNIPPETS, RYAN)))
        self.assertEqual([], vs.load(cfg, vs.VOCABULARY, None))

    def test_adopting_into_the_local_key_is_a_no_op(self):
        cfg = FakeConfig()
        vs.upsert(cfg, vs.VOCABULARY, vs.new_entry(term="Pipedrive"), None)
        self.assertEqual(0, vs.adopt_local_entries(cfg, None))
        self.assertEqual(1, len(vs.load(cfg, vs.VOCABULARY, None)))

    def test_the_two_kinds_do_not_share_a_bucket(self):
        cfg = FakeConfig()
        vs.upsert(cfg, vs.VOCABULARY, vs.new_entry(term="Pipedrive"), RYAN)
        self.assertEqual([], vs.load(cfg, vs.SNIPPETS, RYAN))

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            vs.load(FakeConfig(), "passwords", RYAN)

    def test_a_corrupt_bucket_reads_as_empty_rather_than_throwing(self):
        cfg = FakeConfig()
        cfg.vocabulary = "not a dict"
        self.assertEqual([], vs.load(cfg, vs.VOCABULARY, RYAN))
        cfg.vocabulary = {vs.account_key(RYAN): ["junk", None, {"term": "OK"}]}
        self.assertEqual(["OK"],
                         [e["term"] for e in vs.load(cfg, vs.VOCABULARY, RYAN)])


class UpsertDeleteTests(unittest.TestCase):

    def test_upsert_replaces_by_id_rather_than_appending(self):
        cfg = FakeConfig()
        entry = vs.upsert(cfg, vs.VOCABULARY, vs.new_entry(term="Pipdrive"), RYAN)
        entry["term"] = "Pipedrive"
        vs.upsert(cfg, vs.VOCABULARY, entry, RYAN)

        rows = vs.load(cfg, vs.VOCABULARY, RYAN)
        self.assertEqual(1, len(rows))
        self.assertEqual("Pipedrive", rows[0]["term"])

    def test_delete_leaves_a_tombstone_not_a_hole(self):
        cfg = FakeConfig()
        entry = vs.upsert(cfg, vs.VOCABULARY, vs.new_entry(term="Pipedrive"), RYAN)

        self.assertTrue(vs.soft_delete(cfg, vs.VOCABULARY, entry["id"], RYAN))

        self.assertEqual([], vs.load(cfg, vs.VOCABULARY, RYAN))
        # The tombstone must survive for sync, or the row returns from the
        # other machine's copy.
        allrows = vs.load_all(cfg, vs.VOCABULARY, RYAN)
        self.assertEqual(1, len(allrows))
        self.assertTrue(allrows[0]["deleted"])

    def test_deleting_an_unknown_id_reports_no_change(self):
        cfg = FakeConfig()
        self.assertFalse(vs.soft_delete(cfg, vs.VOCABULARY, "nope", RYAN))

    def test_every_write_persists(self):
        cfg = FakeConfig()
        entry = vs.upsert(cfg, vs.VOCABULARY, vs.new_entry(term="A"), RYAN)
        vs.soft_delete(cfg, vs.VOCABULARY, entry["id"], RYAN)
        self.assertGreaterEqual(cfg.saves, 2)

    def test_a_failing_disk_write_never_propagates(self):
        # The user's edit is already in memory and on screen; a disk hiccup
        # must not surface as a traceback out of a button click.
        class Boom(FakeConfig):
            def save_async(self):
                raise OSError("disk full")

        cfg = Boom()
        vs.upsert(cfg, vs.VOCABULARY, vs.new_entry(term="A"), RYAN)
        self.assertEqual(1, len(vs.load(cfg, vs.VOCABULARY, RYAN)))

    def test_new_entry_stamps_an_id_and_a_time(self):
        a, b = vs.new_entry(term="A"), vs.new_entry(term="B")
        self.assertTrue(a["id"] and b["id"])
        self.assertNotEqual(a["id"], b["id"])
        self.assertTrue(a["updated_at"])
        self.assertFalse(a["deleted"])


class MergeTests(unittest.TestCase):

    def test_newer_edit_wins(self):
        local = [{"id": "1", "term": "Pipdrive", "updated_at": "2026-08-01T00:00:00+00:00"}]
        remote = [{"id": "1", "term": "Pipedrive", "updated_at": "2026-08-09T00:00:00+00:00"}]
        self.assertEqual("Pipedrive", vs.merge(local, remote)[0]["term"])
        self.assertEqual("Pipedrive", vs.merge(remote, local)[0]["term"])

    def test_a_newer_delete_beats_an_older_edit(self):
        local = [{"id": "1", "term": "X", "updated_at": "2026-08-01T00:00:00+00:00"}]
        remote = [{"id": "1", "term": "X", "deleted": True,
                   "updated_at": "2026-08-09T00:00:00+00:00"}]
        self.assertTrue(vs.merge(local, remote)[0]["deleted"])

    def test_an_edit_after_a_delete_brings_the_entry_back(self):
        local = [{"id": "1", "term": "X", "deleted": True,
                  "updated_at": "2026-08-01T00:00:00+00:00"}]
        remote = [{"id": "1", "term": "X", "updated_at": "2026-08-09T00:00:00+00:00"}]
        self.assertFalse(vs.merge(local, remote)[0].get("deleted"))

    def test_disjoint_rows_are_unioned(self):
        local = [{"id": "1", "updated_at": "2026-08-01T00:00:00+00:00"}]
        remote = [{"id": "2", "updated_at": "2026-08-02T00:00:00+00:00"}]
        self.assertEqual({"1", "2"}, {e["id"] for e in vs.merge(local, remote)})

    def test_rows_without_an_id_are_dropped_not_duplicated(self):
        merged = vs.merge([{"term": "no id"}, None, "junk"], [])
        self.assertEqual([], merged)

    def test_merging_with_nothing_is_identity(self):
        local = [{"id": "1", "updated_at": "2026-08-01T00:00:00+00:00"}]
        self.assertEqual(local, vs.merge(local, None))
        self.assertEqual(local, vs.merge(None, local))
        self.assertEqual([], vs.merge(None, None))


class LegacyMigrationTests(unittest.TestCase):

    def test_the_old_comma_separated_field_becomes_entries(self):
        cfg = FakeConfig("FTC, Salesforce , CRM")
        self.assertEqual(3, vs.migrate_legacy_vocabulary(cfg, RYAN))
        self.assertEqual(["FTC", "Salesforce", "CRM"],
                         [e["term"] for e in vs.load(cfg, vs.VOCABULARY, RYAN)])

    def test_migrated_terms_carry_no_mishearings(self):
        cfg = FakeConfig("FTC")
        vs.migrate_legacy_vocabulary(cfg, RYAN)
        self.assertEqual([], vs.load(cfg, vs.VOCABULARY, RYAN)[0]["sounds_like"])

    def test_the_legacy_string_is_left_in_place_for_a_downgrade(self):
        cfg = FakeConfig("FTC, CRM")
        vs.migrate_legacy_vocabulary(cfg, RYAN)
        self.assertEqual("FTC, CRM", cfg.custom_vocabulary)

    def test_running_twice_adds_nothing(self):
        cfg = FakeConfig("FTC, CRM")
        self.assertEqual(2, vs.migrate_legacy_vocabulary(cfg, RYAN))
        self.assertEqual(0, vs.migrate_legacy_vocabulary(cfg, RYAN))
        self.assertEqual(2, len(vs.load(cfg, vs.VOCABULARY, RYAN)))

    def test_a_term_the_user_already_added_by_hand_is_not_duplicated(self):
        cfg = FakeConfig("ftc")
        vs.upsert(cfg, vs.VOCABULARY, vs.new_entry(term="FTC"), RYAN)
        self.assertEqual(0, vs.migrate_legacy_vocabulary(cfg, RYAN))

    def test_empty_or_blank_legacy_value_does_nothing(self):
        for raw in ("", "   ", " , , "):
            cfg = FakeConfig(raw)
            self.assertEqual(0, vs.migrate_legacy_vocabulary(cfg, RYAN))
            self.assertEqual([], vs.load(cfg, vs.VOCABULARY, RYAN))

    def test_migration_is_per_account(self):
        cfg = FakeConfig("FTC")
        vs.migrate_legacy_vocabulary(cfg, RYAN)
        self.assertEqual([], vs.load(cfg, vs.VOCABULARY, JAY))


class ConfigFieldTests(unittest.TestCase):

    def test_the_real_config_dataclass_carries_both_fields(self):
        from config import Config
        cfg = Config()
        self.assertEqual({}, cfg.vocabulary)
        self.assertEqual({}, cfg.snippets)

    def test_the_defaults_are_not_shared_between_instances(self):
        # A mutable default shared across instances would leak one install's
        # words into another's config on the next save.
        from config import Config
        a, b = Config(), Config()
        a.vocabulary["x"] = [1]
        self.assertEqual({}, b.vocabulary)


if __name__ == "__main__":
    unittest.main()
