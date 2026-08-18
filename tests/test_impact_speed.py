import unittest

from app_window import AppWindow


class _Stats:
    def __init__(self, wpm):
        self.wpm = wpm

    def snapshot(self, rng="all"):
        return {
            "saved_minutes": 0,
            "avg_wpm": self.wpm,
            "streak_days": 0,
            "streak_active_today": False,
            "today_words": 0,
            "words": {"today": 0, "week": 0, "month": 0, "year": 0, "all": 0},
        }


class _Label:
    def configure(self, **_kwargs):
        pass


class ImpactSpeedTests(unittest.TestCase):
    def _speed_card(self, wpm):
        window = AppWindow.__new__(AppWindow)
        window._stats = _Stats(wpm)
        window._impact_cards = {"time": {}, "speed": {}, "streak": {}}
        window._impact_today_lbl = _Label()
        updates = {}
        window._set_impact_card = (
            lambda key, value, unit, sub:
                updates.__setitem__(key, (value, unit, sub))
        )
        window._refresh_impact()
        return updates["speed"]

    def test_measured_speed_shows_typing_multiple(self):
        self.assertEqual(
            ("213", "wpm", "5.3× faster than typing"),
            self._speed_card(213),
        )

    def test_nominal_speed_shows_four_times_typing(self):
        self.assertEqual(
            ("160", "wpm", "4× faster than typing"),
            self._speed_card(0),
        )


if __name__ == "__main__":
    unittest.main()
