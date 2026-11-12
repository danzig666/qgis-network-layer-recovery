"""Detects that the computer has resumed from sleep / hibernation.

Two independent mechanisms are combined:

1. Windows power notifications via ``PowerRegisterSuspendResumeNotification``
   (powrprof.dll, Windows 8+).  The OS calls our ctypes callback on a worker
   thread; the callback only flips a thread-safe flag, so no Qt or QGIS
   object is ever touched outside the main thread.  This API also reports
   Modern Standby (S0ix) transitions, which classic ``WM_POWERBROADCAST``
   handling in a top-level window can miss.

2. A wall-clock gap detector: a ``QTimer`` ticks every few seconds on the
   main thread.  If far more wall-clock time elapsed between two ticks than
   expected, the process was frozen (sleep, hibernate, VM pause, ...).
   This works on every platform and is the fallback when (1) is unavailable.

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


class PowerMonitor(QObject):
    """Emits ``resumed(reason)`` on the main thread after a sleep/resume."""

    resumed = pyqtSignal(str)
    suspended = pyqtSignal()

    def __init__(self, parent=None, tick_seconds=5, gap_threshold_seconds=60):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._pending_resume = False
        self._pending_suspend = False
        self._tick_seconds = max(1, int(tick_seconds))
        self.gap_threshold_seconds = max(10, int(gap_threshold_seconds))
        self._last_tick = time.time()
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
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self._native.unregister()

    @property
    def native_error(self):
        return self._native.error

    def _on_tick(self):
        now = time.time()
        gap = now - self._last_tick
        self._last_tick = now

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
        elif gap > self._tick_seconds + self.gap_threshold_seconds:
            reason = f"wall-clock gap of {int(gap)} s"

        if reason and now - self._last_emit > self.debounce_seconds:
            self._last_emit = now
            self.resumed.emit(reason)
