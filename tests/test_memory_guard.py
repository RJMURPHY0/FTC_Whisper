"""MemoryGuard contract.

Pinned here: pressure thresholds; telemetry is rate-limited and fires even
while a dictation is in flight (stamped busy=True); trims + gc run only when
IDLE and respect their cooldown; a raising trim or telemetry callback never
propagates; the live sampler returns sane numbers on Windows.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_guard import MemoryGuard, sample_memory, own_gdi_count, own_private_mb


def _stats(load=50, phys=8000, commit=20000):
    return {"memory_load": load, "avail_phys_mb": phys,
            "total_phys_mb": 16000, "avail_commit_mb": commit}


class PressureDetectionTests(unittest.TestCase):
    def test_healthy_machine_is_not_pressure(self):
        self.assertFalse(MemoryGuard.is_pressure(_stats()))

    def test_none_sample_is_not_pressure(self):
        self.assertFalse(MemoryGuard.is_pressure(None))

    def test_high_memory_load_is_pressure(self):
        self.assertTrue(MemoryGuard.is_pressure(_stats(load=90)))
        self.assertTrue(MemoryGuard.is_pressure(_stats(load=99)))
        self.assertFalse(MemoryGuard.is_pressure(_stats(load=89)))

    def test_low_physical_is_pressure(self):
        self.assertTrue(MemoryGuard.is_pressure(_stats(phys=400)))
        self.assertFalse(MemoryGuard.is_pressure(_stats(phys=401)))

    def test_low_commit_is_pressure(self):
        self.assertTrue(MemoryGuard.is_pressure(_stats(commit=1024)))
        self.assertFalse(MemoryGuard.is_pressure(_stats(commit=1025)))


class TickBehaviourTests(unittest.TestCase):
    def _guard(self, busy=False):
        self.trimmed = 0
        self.reports = []

        def _trim():
            self.trimmed += 1

        return MemoryGuard(trims=[_trim],
                           on_pressure=self.reports.append,
                           is_busy=lambda: busy)

    def test_no_pressure_no_action(self):
        g = self._guard()
        g._tick(1000.0, stats=_stats())
        self.assertEqual(self.trimmed, 0)
        self.assertEqual(self.reports, [])

    def test_pressure_trims_and_reports(self):
        g = self._guard()
        g._tick(1000.0, stats=_stats(load=95))
        self.assertEqual(self.trimmed, 1)
        self.assertEqual(len(self.reports), 1)
        detail = self.reports[0]
        self.assertEqual(detail["memory_load"], 95)
        self.assertIn("own_private_mb", detail)
        self.assertIn("own_gdi", detail)
        self.assertFalse(detail["busy"])

    def test_trim_cooldown(self):
        g = self._guard()
        g._tick(1000.0, stats=_stats(load=95))
        g._tick(1000.0 + MemoryGuard.TRIM_COOLDOWN - 1, stats=_stats(load=95))
        self.assertEqual(self.trimmed, 1)
        g._tick(1000.0 + MemoryGuard.TRIM_COOLDOWN, stats=_stats(load=95))
        self.assertEqual(self.trimmed, 2)

    def test_telemetry_cooldown_longer_than_trim(self):
        g = self._guard()
        g._tick(1000.0, stats=_stats(load=95))
        g._tick(1000.0 + MemoryGuard.TRIM_COOLDOWN, stats=_stats(load=95))
        self.assertEqual(self.trimmed, 2)
        self.assertEqual(len(self.reports), 1)
        g._tick(1000.0 + MemoryGuard.TELEMETRY_COOLDOWN, stats=_stats(load=95))
        self.assertEqual(len(self.reports), 2)

    def test_busy_skips_trim_but_still_reports(self):
        g = self._guard(busy=True)
        g._tick(1000.0, stats=_stats(load=95))
        self.assertEqual(self.trimmed, 0)
        self.assertEqual(len(self.reports), 1)
        self.assertTrue(self.reports[0]["busy"])

    def test_raising_callbacks_never_propagate(self):
        def _boom(*_a):
            raise RuntimeError("boom")

        g = MemoryGuard(trims=[_boom], on_pressure=_boom,
                        is_busy=lambda: False)
        g._tick(1000.0, stats=_stats(load=95))  # must not raise

    def test_no_photoimage_cache_in_app_trims(self):
        """The app's trim callback must never clear PhotoImage caches —
        source-level invariant (see ui_render blanking note)."""
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "app.py"), encoding="utf-8") as f:
            src = f.read()
        start = src.index("def _start_memory_guard")
        body = src[start:src.index("def _start_auto_update", start)]
        self.assertNotIn("ui_render", body)
        self.assertIsNone(re.search(r"(?<!raw)_icon_cache", body))
        self.assertIn("_raw_icon_cache", body)


class LiveSamplerTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows-only API")
    def test_sample_memory_returns_sane_numbers(self):
        stats = sample_memory()
        self.assertIsNotNone(stats)
        self.assertTrue(0 <= stats["memory_load"] <= 100)
        self.assertGreater(stats["total_phys_mb"], 0)
        self.assertGreaterEqual(stats["total_phys_mb"], stats["avail_phys_mb"])

    @unittest.skipUnless(sys.platform == "win32", "Windows-only API")
    def test_own_counters_do_not_raise(self):
        self.assertGreaterEqual(own_private_mb(), 0)
        self.assertGreaterEqual(own_gdi_count(), 0)


if __name__ == "__main__":
    unittest.main()
