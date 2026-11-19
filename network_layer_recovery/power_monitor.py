"""Detects that the computer has resumed from sleep / hibernation.

Two independent mechanisms are combined:

1. Windows power notifications via ``PowerRegisterSuspendResumeNotification``
   (powrprof.dll, Windows 8+).  The OS calls our ctypes callback on a worker
   thread; the callback only flips a thread-safe flag, so no Qt or QGIS
   object is ever touched outside the main thread.  This API also reports
   Modern Standby (S0ix) transitions, which classic ``WM_POWERBROADCAST``
   handling in a top-level window can miss.

2. A sleep-time detector: a ``QTimer`` ticks every few seconds on the main
   thread.  Between two ticks it compares the wall clock with a clock that
   does NOT advance while the computer sleeps (``QueryUnbiasedInterruptTime``
   on Windows, ``time.monotonic`` elsewhere).  The difference is the time
   actually spent asleep.  Merely late ticks - the QGIS main thread blocked
   by a long processing algorithm or plugin - do not count as a sleep,
   because both clocks advance equally while the main thread is blocked.

Both mechanisms feed into the same ``resumed`` signal, which is emitted on
the main thread and debounced.
"""

import sys
import threading
import time

from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

# Power broadcast event types (winuser.h)
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
DEVICE_NOTIFY_CALLBACK = 2


class _WindowsPowerNotification:
    """Thin ctypes wrapper around PowerRegisterSuspendResumeNotification."""

    def __init__(self, on_suspend, on_resume):
        self._on_suspend = on_suspend
        self._on_resume = on_resume
        self._handle = None
        self._callback = None  # must stay referenced while registered
        self._params = None  # must stay referenced while registered
        self.error = None

    def register(self):
        if sys.platform != "win32":
            self.error = "not Windows"
            return False
        try:
            import ctypes
            from ctypes import wintypes

            callback_type = ctypes.WINFUNCTYPE(
                wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p
            )

            class DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS(ctypes.Structure):  # noqa: N801
                _fields_ = [("Callback", callback_type), ("Context", ctypes.c_void_p)]

            def _routine(_context, event_type, _setting):
                # Runs on an OS worker thread: keep it trivial and never raise.
                try:
                    if event_type == PBT_APMSUSPEND:
                        self._on_suspend()
                    elif event_type in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                        self._on_resume()
                except Exception:  # pragma: no cover - defensive
                    pass
                return 0

            self._callback = callback_type(_routine)
            self._params = DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS(self._callback, None)
            handle = ctypes.c_void_p()

            powrprof = ctypes.WinDLL("powrprof.dll")
            func = powrprof.PowerRegisterSuspendResumeNotification
            func.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
            func.restype = wintypes.DWORD
            result = func(
                DEVICE_NOTIFY_CALLBACK, ctypes.cast(ctypes.byref(self._params), ctypes.c_void_p),
                ctypes.byref(handle),
            )
            if result != 0:
                self.error = f"PowerRegisterSuspendResumeNotification failed ({result})"
                self._callback = self._params = None
                return False
            self._handle = handle
            return True
        except Exception as exc:  # missing DLL/export, ctypes problems, ...
            self.error = str(exc)
            self._callback = self._params = None
            return False

    def unregister(self):
        if self._handle is None:
            return
        try:
            import ctypes
            from ctypes import wintypes

            powrprof = ctypes.WinDLL("powrprof.dll")
            func = powrprof.PowerUnregisterSuspendResumeNotification
            func.argtypes = [ctypes.c_void_p]
            func.restype = wintypes.DWORD
            func(self._handle)
        except Exception:
            pass
        self._handle = None
        # Keep the callback alive a little longer is unnecessary: after
        # unregister returns, Windows guarantees no further invocations.
        self._callback = self._params = None


def _awake_seconds():
    """Seconds on a clock that stops while the system sleeps/hibernates."""
    if sys.platform == "win32":
        try:
            import ctypes

            value = ctypes.c_ulonglong()
            if ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(value)):
                return value.value / 1e7  # 100 ns units
        except Exception:
            pass
    # Linux CLOCK_MONOTONIC and macOS mach time also exclude suspend.
    return time.monotonic()


def sleep_detected(wall_gap, awake_gap, threshold):
    """Return seconds slept if it exceeds ``threshold``, else 0.

    ``wall_gap - awake_gap`` is the time spent suspended.  A blocked main
    thread makes both gaps large but their difference stays ~0.
    """
    slept = wall_gap - awake_gap
    return slept if slept > threshold else 0.0


class PowerMonitor(QObject):
    """Emits ``resumed(reason)`` on the main thread after a sleep/resume."""

    resumed = pyqtSignal(str)
    suspended = pyqtSignal()
    # main thread was blocked this many seconds without the system sleeping
    blocked = pyqtSignal(int)

    def __init__(self, parent=None, tick_seconds=5, gap_threshold_seconds=60):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._pending_resume = False
        self._pending_suspend = False
        self._tick_seconds = max(1, int(tick_seconds))
        self.gap_threshold_seconds = max(10, int(gap_threshold_seconds))
        self._last_tick = time.time()
        self._last_awake = _awake_seconds()
        self._last_emit = 0.0
        self.debounce_seconds = 20

        self._timer = QTimer(self)
        self._timer.setInterval(self._tick_seconds * 1000)
        self._timer.timeout.connect(self._on_tick)

        self._native = _WindowsPowerNotification(self._flag_suspend, self._flag_resume)
        self.native_available = False

    # -- called from the OS thread ------------------------------------------
    def _flag_suspend(self):
        with self._lock:
            self._pending_suspend = True

    def _flag_resume(self):
        with self._lock:
            self._pending_resume = True

    # -- main thread -------------------------------------------------------
    def start(self):
        self.native_available = self._native.register()
        self._last_tick = time.time()
        self._last_awake = _awake_seconds()
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self._native.unregister()

    @property
    def native_error(self):
        return self._native.error

    def _on_tick(self):
        now = time.time()
        awake = _awake_seconds()
        gap = now - self._last_tick
        awake_gap = awake - self._last_awake
        self._last_tick = now
        self._last_awake = awake

        with self._lock:
            native_resume = self._pending_resume
            native_suspend = self._pending_suspend
            self._pending_resume = False
            self._pending_suspend = False

        if native_suspend:
            self.suspended.emit()

        reason = None
        if native_resume:
            reason = "Windows resume notification"
        else:
            slept = sleep_detected(gap, awake_gap, self.gap_threshold_seconds)
            if slept:
                reason = f"system was asleep for about {int(slept)} s"
            elif gap > self._tick_seconds + self.gap_threshold_seconds:
                # Late tick without sleep: something blocked the main thread.
                self.blocked.emit(int(gap))

        if reason and now - self._last_emit > self.debounce_seconds:
            self._last_emit = now
            self.resumed.emit(reason)
