"""
System memory-pressure guard.

When Windows runs out of physical memory or commit charge, GDI allocations
start failing — tkinter's next widget repaint hits CreateDIBSection == NULL
and Tk shows a one-shot modal "Tk_GetPixmap: Error from CreateDIBSection"
box mid-paint. Observed live on 2026-07-29: the app was the messenger, not
the culprit (18 idle MCP server processes were holding ~12.6 GB of commit
on a 16 GB machine), but on an 8 GB customer machine the same squeeze is
easy to hit and the fleet needs to see it happening.

A daemon thread samples GlobalMemoryStatusEx every CHECK_INTERVAL (one
ctypes call — nothing on the dictation path). Under pressure it:
  - reports a rate-limited `memory_pressure` fleet telemetry event with the
    system numbers plus our own private bytes / GDI count, so a starved
    machine is distinguishable from an app leak in the error log
  - runs the registered cache trims and gc.collect(), but only while IDLE —
    a gc pause must never land inside a live dictation

Deliberate non-actions: the working set is never trimmed (paging the ASR
model out would add latency to the next dictation), and PhotoImage caches
are never cleared (dropping an image a canvas is still displaying blanks
that widget until its next full redraw).
"""

import ctypes
import gc
import threading
import time


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]

_MB = 1024 * 1024


def sample_memory():
    """One GlobalMemoryStatusEx call → dict, or None on failure/off-Windows."""
    try:
        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return None
        return {
            "memory_load": int(stat.dwMemoryLoad),
            "avail_phys_mb": int(stat.ullAvailPhys // _MB),
            "total_phys_mb": int(stat.ullTotalPhys // _MB),
            "avail_commit_mb": int(stat.ullAvailPageFile // _MB),
        }
    except Exception:
        return None


def own_private_mb() -> int:
    """This process's private commit in MB (0 on failure)."""
    try:
        counters = _PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if not ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb):
            return 0
        return int(counters.PrivateUsage // _MB)
    except Exception:
        return 0


def own_gdi_count() -> int:
    """This process's GDI handle count (0 on failure). A four-digit value
    here would point at a leak in US rather than a starved machine."""
    try:
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        return int(ctypes.windll.user32.GetGuiResources(handle, 0))
    except Exception:
        return 0


class MemoryGuard:
    CHECK_INTERVAL = 30.0
    # The observed failure happened around 90% physical load with commit
    # still plentiful — physical squeeze is the primary signal.
    PRESSURE_MEMORY_LOAD = 90
    PRESSURE_AVAIL_PHYS_MB = 400
    PRESSURE_AVAIL_COMMIT_MB = 1024
    TRIM_COOLDOWN = 300.0
    TELEMETRY_COOLDOWN = 3600.0

    def __init__(self, trims=None, on_pressure=None, is_busy=None):
        """trims: zero-arg callables clearing regenerate-on-demand caches.
        on_pressure: callable(detail_dict) → fire-and-forget telemetry.
        is_busy: callable() → True while a dictation is in flight."""
        self._trims = list(trims or [])
        self._on_pressure = on_pressure
        self._is_busy = is_busy or (lambda: False)
        self._stop = threading.Event()
        # None = never yet — time.monotonic() counts from boot, so a plain
        # 0.0 baseline would suppress the first report/trim for a whole
        # cooldown on a freshly booted machine.
        self._last_trim = None
        self._last_report = None
        self._thread = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="memory-guard")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @classmethod
    def is_pressure(cls, stats) -> bool:
        if not stats:
            return False
        return (stats.get("memory_load", 0) >= cls.PRESSURE_MEMORY_LOAD
                or stats.get("avail_phys_mb", 1 << 30) <= cls.PRESSURE_AVAIL_PHYS_MB
                or stats.get("avail_commit_mb", 1 << 30) <= cls.PRESSURE_AVAIL_COMMIT_MB)

    def _loop(self) -> None:
        while not self._stop.wait(self.CHECK_INTERVAL):
            try:
                self._tick(time.monotonic())
            except Exception:
                pass  # the guard must never take the app down

    def _tick(self, now: float, stats=None) -> None:
        """One sample-and-react step. `stats` injectable for tests."""
        if stats is None:
            stats = sample_memory()
        if not self.is_pressure(stats):
            return
        busy = bool(self._is_busy())
        # Telemetry first — pressure DURING a dictation is exactly the
        # interesting case, and the report is a dict + daemon-thread insert.
        if self._on_pressure and (
                self._last_report is None
                or now - self._last_report >= self.TELEMETRY_COOLDOWN):
            self._last_report = now
            detail = dict(stats)
            detail["own_private_mb"] = own_private_mb()
            detail["own_gdi"] = own_gdi_count()
            detail["busy"] = busy
            try:
                self._on_pressure(detail)
            except Exception:
                pass
        if busy:
            return
        if self._last_trim is None or now - self._last_trim >= self.TRIM_COOLDOWN:
            self._last_trim = now
            for trim in self._trims:
                try:
                    trim()
                except Exception:
                    pass
            try:
                gc.collect()
            except Exception:
                pass
