"""The refine popup's voice prompt must never undo the user's own edits.

The mic re-transcribes the WHOLE audio buffer on every ~1.6s preview, so the
old code — write the transcription straight into the Ask box — restored every
word the user had just deleted the moment they spoke again. Reported live:
delete the text mid-sentence, keep talking, and the deleted text comes back.

Two rules are pinned here:

1. Dictation appends to whatever is in the box RIGHT NOW. Text the user typed
   survives; text the user deleted stays deleted; only words spoken since the
   edit are added.
2. Submitting the instruction (Enter / ✦ Ask) ends the capture, and the
   capture's final transcription is dropped — a sent box never refills itself.
"""
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from popup import FloatingPopup


class _FakeAsk:
    """Duck-types the handful of tk.Text calls the Ask box paths make."""

    def __init__(self, value: str = ""):
        self.value = value

    def get(self, _start, _end):
        return self.value

    def delete(self, _start, _end):
        self.value = ""

    def insert(self, _index, text):
        self.value = text

    def configure(self, **_kw):
        pass


def _popup(box_text: str = "") -> FloatingPopup:
    """A FloatingPopup with just the voice-prompt state wired up — no Tk."""
    p = FloatingPopup.__new__(FloatingPopup)
    p._ask_entry = _FakeAsk(box_text)
    p._ask_showing_placeholder = False
    p._mic_recording = True
    p._voice_token = 1
    p._voice_base = ""
    p._voice_drop = 0
    p._voice_written_words = 0
    p._voice_last = None
    p._autosize_ask = lambda: None
    p._mic_btn = _FakeAsk()          # _reset_mic_btn only configures it
    return p


class VoiceAppendTests(unittest.TestCase):
    def test_previews_replace_while_the_user_leaves_the_box_alone(self):
        p = _popup()
        p._apply_voice_text("make this", 1)
        p._apply_voice_text("make this shorter", 1)
        p._apply_voice_text("make this shorter and friendlier", 1)
        self.assertEqual(p._ask_entry.value, "make this shorter and friendlier")

    def test_deleting_the_box_mid_dictation_keeps_it_deleted(self):
        p = _popup()
        p._apply_voice_text("make this shorter", 1)
        p._ask_entry.value = ""                      # user clears the box
        p._apply_voice_text("make this shorter and friendlier", 1)
        # Only the words spoken since the delete — the cleared ones stay gone.
        self.assertEqual(p._ask_entry.value, "and friendlier")

    def test_hand_typed_text_is_not_wiped_by_the_mic(self):
        p = _popup("keep this bit")
        p._apply_voice_text("and add a sign off", 1)
        self.assertEqual(p._ask_entry.value, "keep this bit and add a sign off")
        p._apply_voice_text("and add a sign off from Ryan", 1)
        self.assertEqual(p._ask_entry.value,
                         "keep this bit and add a sign off from Ryan")

    def test_partial_edit_is_respected(self):
        p = _popup()
        p._apply_voice_text("rewrite this in plain English", 1)
        p._ask_entry.value = "rewrite this"          # user trims it back
        p._apply_voice_text("rewrite this in plain English please", 1)
        self.assertEqual(p._ask_entry.value, "rewrite this please")

    def test_a_delete_between_every_preview_never_resurrects_anything(self):
        p = _popup()
        transcript = ["one", "one two", "one two three", "one two three four"]
        for t in transcript:
            p._ask_entry.value = ""
            p._apply_voice_text(t, 1)
        self.assertEqual(p._ask_entry.value, "four")


class SubmitStopsCaptureTests(unittest.TestCase):
    def test_stop_drops_the_captures_final_transcription(self):
        p = _popup()
        p._apply_voice_text("shorten this", 1)
        token = p._voice_token
        p._stop_voice_capture()
        self.assertFalse(p._mic_recording)
        self.assertNotEqual(p._voice_token, token)
        # The capture's final blocking pass lands after the ask has gone.
        p._apply_voice_text("shorten this a lot", token)
        self.assertEqual(p._ask_entry.value, "shorten this")

    def test_stop_is_a_noop_when_the_mic_is_idle(self):
        p = _popup()
        p._mic_recording = False
        token = p._voice_token
        p._stop_voice_capture()
        self.assertEqual(p._voice_token, token)

    def test_submitting_the_instruction_stops_the_mic(self):
        src = inspect.getsource(FloatingPopup._run_ai_custom)
        self.assertIn("_stop_voice_capture()", src)

    def test_closing_the_panel_stops_the_mic(self):
        src = inspect.getsource(FloatingPopup._do_hide)
        self.assertIn("_stop_voice_capture()", src)

    def test_mic_paths_never_write_the_ask_box_directly(self):
        # _set_ask_entry is the raw setter — it has no idea the user may have
        # edited the box, so every voice write must go through the reconciling
        # wrapper instead.
        src = inspect.getsource(FloatingPopup._on_mic_click)
        self.assertIn("_apply_voice_text", src)
        self.assertNotIn("_set_ask_entry", src)


if __name__ == "__main__":
    unittest.main()
