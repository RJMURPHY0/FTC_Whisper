import unittest
from types import SimpleNamespace

from app_window import AppWindow


class _FakeCanvas:
    def __init__(self, content_height=1000, viewport_height=200):
        self.content_height = float(content_height)
        self.viewport_height = float(viewport_height)
        self.top_fraction = 0.0
        self.jobs = []
        self.cancelled = set()
        self.yview_calls = []

    def bbox(self, _tag):
        raise AssertionError(
            "scroll metrics must read the scrollregion, not walk every item")

    def cget(self, option):
        assert option == "scrollregion"
        return f"0 0 100 {self.content_height}"

    def winfo_height(self):
        return self.viewport_height

    def yview(self, *args):
        if args:
            self.yview_calls.append(args)
            if args[0] == "moveto":
                self.top_fraction = float(args[1])
            return None
        return (
            self.top_fraction,
            min(1.0, self.top_fraction + self.viewport_height / self.content_height),
        )

    def yview_moveto(self, fraction):
        self.top_fraction = float(fraction)

    def after(self, delay, callback, *args):
        job = len(self.jobs) + 1
        self.jobs.append((job, delay, callback, args))
        return job

    def after_cancel(self, job):
        self.cancelled.add(job)

    def run_jobs(self, limit=100):
        count = 0
        while self.jobs and count < limit:
            job, _delay, callback, args = self.jobs.pop(0)
            if job not in self.cancelled:
                callback(*args)
            count += 1
        return count


class _MeasureFont:
    def measure(self, value):
        return len(value) * 10

    def metrics(self, _name):
        return 15


class _ProbeCanvas:
    def __init__(self, rendered_height):
        self.rendered_height = rendered_height
        self.deleted = []

    def create_text(self, *_args, **_kwargs):
        return 7

    def bbox(self, _item):
        return 0, 0, 100, self.rendered_height

    def delete(self, item):
        self.deleted.append(item)


def _window_for(canvas):
    window = object.__new__(AppWindow)
    window._scroll_states = {}
    window._hist_cv = canvas
    window._set_history_hover = lambda _index: None
    window._refresh_history_hover = lambda: None
    return window


class SmoothScrollTests(unittest.TestCase):
    def test_precision_delta_is_preserved_and_wheel_events_coalesce(self):
        canvas = _FakeCanvas()
        window = _window_for(canvas)
        px = AppWindow._SCROLL_PX_PER_DELTA
        notch = 120 * px

        self.assertEqual("break", window._wheel_scroll(
            canvas, SimpleNamespace(delta=-1)))
        self.assertAlmostEqual(px, window._scroll_states[canvas]["target"])
        self.assertEqual(1, len(canvas.jobs))

        window._wheel_scroll(canvas, SimpleNamespace(delta=-120))
        self.assertAlmostEqual(notch + px,
                               window._scroll_states[canvas]["target"])
        # A burst updates one target instead of scheduling competing animations.
        self.assertEqual(1, len(canvas.jobs))

    def test_animation_reaches_pixel_target_in_small_frames(self):
        canvas = _FakeCanvas()
        window = _window_for(canvas)
        notch = 120 * AppWindow._SCROLL_PX_PER_DELTA

        window._wheel_scroll(canvas, SimpleNamespace(delta=-120))
        frames = canvas.run_jobs()

        # Frames run every 12ms, so this is the settle time the user feels.
        # Anything past ~8 frames (100ms) reads as the view lagging the wheel.
        self.assertLessEqual(frames, 8)
        self.assertAlmostEqual(notch,
                               canvas.top_fraction * canvas.content_height,
                               delta=AppWindow._SCROLL_SNAP)
        self.assertIsNone(window._scroll_states[canvas]["job"])

    def test_a_single_notch_moves_most_of_the_way_on_the_first_frame(self):
        """The first frame must land near the target, not creep toward it."""
        canvas = _FakeCanvas()
        window = _window_for(canvas)
        notch = 120 * AppWindow._SCROLL_PX_PER_DELTA

        window._wheel_scroll(canvas, SimpleNamespace(delta=-120))
        job, _delay, callback, args = canvas.jobs.pop(0)
        callback(*args)

        moved = canvas.top_fraction * canvas.content_height
        self.assertGreaterEqual(moved, notch * 0.45)

    def test_scrollbar_drag_cancels_animation_and_tracks_directly(self):
        canvas = _FakeCanvas()
        window = _window_for(canvas)
        window._wheel_scroll(canvas, SimpleNamespace(delta=-120))
        pending = window._scroll_states[canvas]["job"]

        window._scrollbar_command(canvas, "moveto", 0.4)

        self.assertIn(pending, canvas.cancelled)
        self.assertNotIn(canvas, window._scroll_states)
        self.assertEqual(("moveto", 0.4), canvas.yview_calls[-1])


class HistoryCanvasLayoutTests(unittest.TestCase):
    def test_preview_is_measured_and_forced_to_one_line(self):
        window = object.__new__(AppWindow)
        window._hist_elide_font_9 = _MeasureFont()

        value, font = window._history_elide(
            "A long preview\nthat must never wrap into metadata", 120, size=9)

        self.assertNotIn("\n", value)
        self.assertTrue(value.endswith("…"))
        self.assertLessEqual(font.measure(value), 120)

    def test_expanded_height_uses_canvas_rendered_bbox(self):
        window = object.__new__(AppWindow)
        window._hist_body_font = _MeasureFont()
        window._hist_cv = _ProbeCanvas(rendered_height=90)

        height = window._history_detail_height("x" * 200, 100)

        self.assertEqual(106, height)
        self.assertEqual([7], window._hist_cv.deleted)


if __name__ == "__main__":
    unittest.main()
