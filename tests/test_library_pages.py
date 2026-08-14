"""The Custom Vocabulary and Snippets pages.

Two kinds of check: source-level invariants that encode the house rules this
project has already paid for (ScrollPane not Canvas, atomic swaps), and live
Tk exercising the list / search / editor / delete cycle against a fake config.
"""

import inspect
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vocab_store as vs  # noqa: E402
from app_window import AppWindow  # noqa: E402

EMAIL = "ryan@ftcsafety.co.uk"


class FakeConfig:
    def __init__(self):
        self.vocabulary = {}
        self.snippets = {}
        self.custom_vocabulary = ""

    def save_async(self):
        pass


class SourceInvariantTests(unittest.TestCase):

    def test_the_list_scrolls_on_scrollpane_never_a_canvas(self):
        # Canvas blit-scroll is what minted the duplicated-card ghosts the
        # v1.6.36 structural fix removed; a page rebuilt on Canvas would
        # reintroduce them.
        src = inspect.getsource(AppWindow._build_library_page)
        self.assertIn("ScrollPane(", src)
        self.assertNotIn("tk.Canvas(", src)

    def test_rendering_the_list_is_one_atomic_frame(self):
        # Destroying and rebuilding rows live is the map/unmap churn that
        # shows as a flicker.
        self.assertIn("_atomic_ui",
                      inspect.getsource(AppWindow._render_library))

    def test_both_pages_are_registered_as_swappable_frames(self):
        src = inspect.getsource(AppWindow._switch_dash_tab)
        self.assertIn('"vocabulary": self._vocabulary_frame', src)
        self.assertIn('"snippets": self._snippets_frame', src)

    def test_both_pages_are_built_by_the_dashboard(self):
        src = inspect.getsource(AppWindow._build_dashboard)
        self.assertIn('_build_library_page(self._vocabulary_frame, "vocabulary")',
                      src)
        self.assertIn('_build_library_page(self._snippets_frame, "snippets")',
                      src)

    def test_back_returns_to_settings(self):
        # The pages are sub-pages of Settings, not tabs: Back must land where
        # the user came from, not on Home.
        self.assertIn('_switch_dash_tab("settings")',
                      inspect.getsource(AppWindow._build_library_page))

    def test_the_pages_are_not_in_the_tab_bar(self):
        src = inspect.getsource(AppWindow._build_dashboard)
        tab_bar = src.split("for name, label in")[1].split("]")[0]
        self.assertNotIn("vocabulary", tab_bar)
        self.assertNotIn("snippets", tab_bar)

    def test_arriving_at_a_page_closes_any_stranded_editor(self):
        src = inspect.getsource(AppWindow._switch_dash_tab)
        self.assertIn('st["editing"] = None', src)

    def test_sync_never_blocks_the_ui_thread(self):
        self.assertIn("threading.Thread",
                      inspect.getsource(AppWindow._sync_library))

    def test_settings_links_to_both_pages_instead_of_an_inline_field(self):
        src = inspect.getsource(AppWindow._build_settings_tab)
        self.assertIn('_link_card("vocabulary"', src)
        self.assertIn('_link_card("snippets"', src)
        # The old comma-separated Entry is gone; its config key survives only
        # for the migration and a downgrade.
        self.assertNotIn("vocab_entry", src)


_SHARED_ROOT = None


def tearDownModule():
    global _SHARED_ROOT
    try:
        import ui_render
        ui_render.clear_cache()
    except Exception:
        pass
    if _SHARED_ROOT is not None:
        try:
            _SHARED_ROOT.destroy()
        except Exception:
            pass
        _SHARED_ROOT = None


def _shared_root():
    """One Tk root for the module — see test_impact_panel for why a second
    root in the same process breaks on ui_render's cached ImageTk objects."""
    global _SHARED_ROOT
    import tkinter as tk
    if _SHARED_ROOT is not None and _SHARED_ROOT.winfo_exists():
        return _SHARED_ROOT
    try:
        _SHARED_ROOT = tk.Tk()
    except Exception as e:                          # no window station (CI)
        raise unittest.SkipTest(f"Tk unavailable: {e}")
    _SHARED_ROOT.geometry("440x620")
    return _SHARED_ROOT


class SharedSearchBarTests(unittest.TestCase):
    """History, Custom Vocabulary and Snippets all run on this one control, so
    a regression here breaks three pages at once."""

    @classmethod
    def setUpClass(cls):
        cls.root = _shared_root()

    def setUp(self):
        import tkinter as tk
        from app_window import C
        self.w = AppWindow.__new__(AppWindow)
        self.w._root = self.root
        self.frame = tk.Frame(self.root, bg=C["bg"])
        self.frame.pack(fill="both", expand=True)
        self.addCleanup(self.frame.destroy)
        self.seen = []
        self.entry = self.w._search_bar(self.frame, "Search things…",
                                        self.seen.append)
        self.root.update()

    def _type(self, text, *, replace=True):
        """Type into the field for real. Tk only dispatches a generated key
        event to the widget that HOLDS FOCUS, so a KeyRelease without this
        silently goes nowhere and the test passes for the wrong reason."""
        self.entry.focus_force()
        self.root.update()
        if replace:
            self.entry.delete(0, "end")
        for ch in text:
            self.entry.insert("end", ch)
            self.entry.event_generate("<KeyRelease>")

    def _settle(self):
        # The callback is debounced by 90ms; drain the after queue.
        self.root.after(140, self.root.quit)
        self.root.mainloop()

    def test_history_uses_the_shared_control(self):
        self.assertIn("_search_bar",
                      inspect.getsource(AppWindow._build_history_tab))

    def test_it_starts_showing_the_placeholder(self):
        self.assertEqual("Search things…", self.entry.get())

    def test_focus_clears_the_placeholder_and_blur_restores_it(self):
        self.entry.event_generate("<FocusIn>")
        self.assertEqual("", self.entry.get())
        self.entry.event_generate("<FocusOut>")
        self.assertEqual("Search things…", self.entry.get())

    def test_typing_reports_a_lower_cased_query(self):
        self._type("PipeDrive")
        self._settle()
        self.assertEqual(["pipedrive"], self.seen)

    def test_the_placeholder_never_leaks_out_as_a_query(self):
        # The field still holds the placeholder text until it is focused; a
        # naive read would filter every list down to nothing.
        self.entry.focus_force()
        self.root.update()
        self.entry.event_generate("<KeyRelease>")
        self._settle()
        self.assertEqual([""], self.seen)

    def test_rapid_typing_fires_once(self):
        self._type("pipe")
        self._settle()
        self.assertEqual(["pipe"], self.seen)


class LivePageTests(unittest.TestCase):
    kind = "vocabulary"

    @classmethod
    def setUpClass(cls):
        cls.root = _shared_root()

    def setUp(self):
        import tkinter as tk
        from app_window import C
        self.w = AppWindow.__new__(AppWindow)
        self.w._root = self.root
        self.w._config = FakeConfig()
        self.w._auth = types.SimpleNamespace(user_email=EMAIL)
        self.w._db = None
        self.w._lib = {}
        self.w._lib_count_labels = {}
        self.w._atomic_ui = lambda fn: fn()
        self.w._ui_after = lambda ms, fn: fn()
        self.w._scrollbar_command = lambda pane, *a: None
        self.frame = tk.Frame(self.root, bg=C["bg"])
        self.frame.pack(fill="both", expand=True)
        self.addCleanup(self.frame.destroy)

    def _build(self):
        self.w._build_library_page(self.frame, self.kind)
        self.root.update()

    def _texts(self):
        """Every label string currently on the page."""
        import tkinter as tk
        out = []

        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, tk.Label):
                    try:
                        out.append(child.cget("text"))
                    except tk.TclError:
                        pass
                walk(child)

        walk(self.frame)
        return out

    def _seed(self, **fields):
        return vs.upsert(self.w._config, self.kind, vs.new_entry(**fields),
                         EMAIL)

    # ── list ────────────────────────────────────────────────────────────────

    def test_empty_state_is_shown_when_there_is_nothing(self):
        self._build()
        self.assertIn("No words added", self._texts())

    def test_entries_are_listed_with_their_mishearings(self):
        self._seed(term="Pipedrive", sounds_like=["pipe drive"])
        self._build()
        texts = self._texts()
        self.assertIn("Pipedrive", texts)
        self.assertIn("sounds like: pipe drive", texts)
        self.assertNotIn("No words added", texts)

    def test_the_count_reads_naturally_at_one_and_at_many(self):
        self._seed(term="Pipedrive")
        self._build()
        self.assertIn("1 word", self._texts())
        self._seed(term="Xero")
        self.w._render_library(self.kind)
        self.root.update()
        self.assertIn("2 words", self._texts())

    def test_a_deleted_entry_leaves_the_list(self):
        entry = self._seed(term="Pipedrive")
        self._build()
        self.w._lib_delete(self.kind, entry["id"])
        self.root.update()
        self.assertNotIn("Pipedrive", self._texts())
        # ...but survives as a tombstone, or the other machine resurrects it.
        self.assertEqual(
            1, len(vs.load_all(self.w._config, self.kind, EMAIL)))

    # ── search ──────────────────────────────────────────────────────────────

    def test_search_filters_the_list(self):
        self._seed(term="Pipedrive")
        self._seed(term="Xero")
        self._build()
        self.w._lib_state(self.kind)["query"] = "xer"
        self.w._render_library(self.kind)
        self.root.update()
        texts = self._texts()
        self.assertIn("Xero", texts)
        self.assertNotIn("Pipedrive", texts)

    def test_search_matches_the_mishearings_too(self):
        self._seed(term="Pipedrive", sounds_like=["pied drive"])
        self._build()
        self.w._lib_state(self.kind)["query"] = "pied"
        self.w._render_library(self.kind)
        self.root.update()
        self.assertIn("Pipedrive", self._texts())

    def test_a_search_with_no_matches_says_so_rather_than_looking_empty(self):
        self._seed(term="Pipedrive")
        self._build()
        self.w._lib_state(self.kind)["query"] = "zzzz"
        self.w._render_library(self.kind)
        self.root.update()
        texts = self._texts()
        self.assertIn("No matches", texts)
        # The empty state would wrongly imply nothing has ever been added.
        self.assertNotIn("No words added", texts)

    # ── editor ──────────────────────────────────────────────────────────────

    def _editor_fields(self):
        import tkinter as tk
        found = []

        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, (tk.Entry, tk.Text)):
                    found.append(child)
                walk(child)

        walk(self.frame)
        return found

    def test_add_opens_an_editor(self):
        self._build()
        self.w._lib_start_add(self.kind)
        self.root.update()
        self.assertIn("WORD OR PHRASE", self._texts())

    def test_saving_a_new_entry_persists_it(self):
        self._build()
        self.w._lib_start_add(self.kind)
        self.root.update()
        # [0] is the search box; the editor's own fields follow.
        fields = self._editor_fields()
        term, sounds = fields[1], fields[2]
        term.insert(0, "Pipedrive")
        sounds.insert("1.0", "pipe drive\npied drive")
        err = types.SimpleNamespace(configure=lambda **kw: None)
        self.w._lib_save(self.kind, {}, {"term": term, "sounds_like": sounds},
                         err)
        self.root.update()

        rows = vs.load(self.w._config, self.kind, EMAIL)
        self.assertEqual(1, len(rows))
        self.assertEqual("Pipedrive", rows[0]["term"])
        self.assertEqual(["pipe drive", "pied drive"], rows[0]["sounds_like"])
        self.assertIsNone(self.w._lib_state(self.kind)["editing"])

    def test_saving_without_a_term_is_refused_with_a_reason(self):
        self._build()
        seen = {}
        err = types.SimpleNamespace(
            configure=lambda **kw: seen.update(kw))
        blank = self._blank_fields()
        self.w._lib_save(self.kind, {}, blank, err)
        self.assertTrue(seen.get("text"))
        self.assertEqual([], vs.load(self.w._config, self.kind, EMAIL))

    def test_an_unsafe_mishearing_is_refused_and_nothing_is_saved(self):
        # A two-character pattern matches ordinary speech; saving it would
        # quietly corrupt every later dictation.
        self._build()
        seen = {}
        err = types.SimpleNamespace(configure=lambda **kw: seen.update(kw))
        fields = self._blank_fields()
        fields["term"].insert(0, "United States")
        fields["sounds_like"].insert("1.0", "us")
        self.w._lib_save(self.kind, {}, fields, err)
        self.assertIn("us", seen.get("text", ""))
        self.assertEqual([], vs.load(self.w._config, self.kind, EMAIL))

    def _blank_fields(self):
        import tkinter as tk
        return {"term": tk.Entry(self.frame),
                "sounds_like": tk.Text(self.frame)}

    def test_editing_an_existing_entry_replaces_it(self):
        entry = self._seed(term="Pipdrive", sounds_like=[])
        self._build()
        import tkinter as tk
        fields = {"term": tk.Entry(self.frame), "sounds_like": tk.Text(self.frame)}
        fields["term"].insert(0, "Pipedrive")
        err = types.SimpleNamespace(configure=lambda **kw: None)
        self.w._lib_save(self.kind, entry, fields, err)

        rows = vs.load(self.w._config, self.kind, EMAIL)
        self.assertEqual(1, len(rows), "editing must not add a second entry")
        self.assertEqual("Pipedrive", rows[0]["term"])


class LiveSnippetPageTests(LivePageTests):
    kind = "snippets"

    def _seed(self, term=None, sounds_like=None, **fields):
        fields.setdefault("name", term or "Snippet")
        fields.setdefault("trigger", (term or "snippet").lower())
        fields.setdefault("body", "expanded text")
        return vs.upsert(self.w._config, self.kind, vs.new_entry(**fields),
                         EMAIL)

    def _blank_fields(self):
        import tkinter as tk
        return {"name": tk.Entry(self.frame), "trigger": tk.Entry(self.frame),
                "body": tk.Text(self.frame)}

    # Vocabulary-shaped assertions that do not apply here.
    def test_empty_state_is_shown_when_there_is_nothing(self):
        self._build()
        self.assertIn("No snippets added", self._texts())

    def test_entries_are_listed_with_their_mishearings(self):
        self._seed(term="My email", body="ryan@ftc.co.uk")
        self._build()
        texts = self._texts()
        self.assertIn("My email", texts)
        self.assertIn('say "my email"', texts)
        self.assertIn("ryan@ftc.co.uk", texts)

    def test_the_count_reads_naturally_at_one_and_at_many(self):
        self._seed(term="One")
        self._build()
        self.assertIn("1 snippet", self._texts())
        self._seed(term="Two")
        self.w._render_library(self.kind)
        self.root.update()
        self.assertIn("2 snippets", self._texts())

    def test_a_deleted_entry_leaves_the_list(self):
        entry = self._seed(term="My email")
        self._build()
        self.w._lib_delete(self.kind, entry["id"])
        self.root.update()
        self.assertNotIn("My email", self._texts())
        self.assertEqual(1, len(vs.load_all(self.w._config, self.kind, EMAIL)))

    def test_search_filters_the_list(self):
        self._seed(term="My email")
        self._seed(term="Site address")
        self._build()
        self.w._lib_state(self.kind)["query"] = "site"
        self.w._render_library(self.kind)
        self.root.update()
        texts = self._texts()
        self.assertIn("Site address", texts)
        self.assertNotIn("My email", texts)

    def test_search_matches_the_mishearings_too(self):
        self._seed(term="My email", body="ryan@ftcsafety.co.uk")
        self._build()
        self.w._lib_state(self.kind)["query"] = "ftcsafety"
        self.w._render_library(self.kind)
        self.root.update()
        self.assertIn("My email", self._texts())

    def test_a_search_with_no_matches_says_so_rather_than_looking_empty(self):
        self._seed(term="My email")
        self._build()
        self.w._lib_state(self.kind)["query"] = "zzzz"
        self.w._render_library(self.kind)
        self.root.update()
        texts = self._texts()
        self.assertIn("No matches", texts)
        self.assertNotIn("No snippets added", texts)

    def test_add_opens_an_editor(self):
        self._build()
        self.w._lib_start_add(self.kind)
        self.root.update()
        self.assertIn("WHEN I SAY", self._texts())

    def test_saving_a_new_entry_persists_it(self):
        self._build()
        fields = self._blank_fields()
        fields["name"].insert(0, "My email")
        fields["trigger"].insert(0, "my email")
        fields["body"].insert("1.0", "ryan@ftc.co.uk")
        err = types.SimpleNamespace(configure=lambda **kw: None)
        self.w._lib_save(self.kind, {}, fields, err)

        rows = vs.load(self.w._config, self.kind, EMAIL)
        self.assertEqual(1, len(rows))
        self.assertEqual("my email", rows[0]["trigger"])
        self.assertEqual("ryan@ftc.co.uk", rows[0]["body"])

    def test_saving_without_a_term_is_refused_with_a_reason(self):
        self._build()
        seen = {}
        err = types.SimpleNamespace(configure=lambda **kw: seen.update(kw))
        self.w._lib_save(self.kind, {}, self._blank_fields(), err)
        self.assertTrue(seen.get("text"))
        self.assertEqual([], vs.load(self.w._config, self.kind, EMAIL))

    def test_an_unsafe_mishearing_is_refused_and_nothing_is_saved(self):
        # Snippet twin: a trigger that is an everyday word would expand every
        # time the user said it.
        self._build()
        seen = {}
        err = types.SimpleNamespace(configure=lambda **kw: seen.update(kw))
        fields = self._blank_fields()
        fields["trigger"].insert(0, "the")
        fields["body"].insert("1.0", "something")
        self.w._lib_save(self.kind, {}, fields, err)
        self.assertTrue(seen.get("text"))
        self.assertEqual([], vs.load(self.w._config, self.kind, EMAIL))

    def test_an_empty_body_is_refused(self):
        self._build()
        seen = {}
        err = types.SimpleNamespace(configure=lambda **kw: seen.update(kw))
        fields = self._blank_fields()
        fields["trigger"].insert(0, "my email")
        self.w._lib_save(self.kind, {}, fields, err)
        self.assertTrue(seen.get("text"))
        self.assertEqual([], vs.load(self.w._config, self.kind, EMAIL))

    def test_a_snippet_with_no_name_falls_back_to_its_trigger(self):
        # An unnamed row would otherwise render as a blank line in the list.
        self._build()
        fields = self._blank_fields()
        fields["trigger"].insert(0, "my email")
        fields["body"].insert("1.0", "ryan@ftc.co.uk")
        err = types.SimpleNamespace(configure=lambda **kw: None)
        self.w._lib_save(self.kind, {}, fields, err)
        self.assertEqual("my email",
                         vs.load(self.w._config, self.kind, EMAIL)[0]["name"])

    def test_editing_an_existing_entry_replaces_it(self):
        entry = self._seed(term="My email", body="old@ftc.co.uk")
        self._build()
        fields = self._blank_fields()
        fields["name"].insert(0, "My email")
        fields["trigger"].insert(0, "my email")
        fields["body"].insert("1.0", "new@ftc.co.uk")
        err = types.SimpleNamespace(configure=lambda **kw: None)
        self.w._lib_save(self.kind, entry, fields, err)

        rows = vs.load(self.w._config, self.kind, EMAIL)
        self.assertEqual(1, len(rows))
        self.assertEqual("new@ftc.co.uk", rows[0]["body"])


if __name__ == "__main__":
    unittest.main()
