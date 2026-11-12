"""Background "can this file be read right now?" test.

Reopening a layer happens on the QGIS main thread and cannot be interrupted.
Right after a resume, opening a file can hang for a long time, for example
when a GeoPackage still has a stale SQLite lock on the SMB server or when SMB
times out on an old handle.  So before QGIS touches a file, a background
thread reads it, and only files that pass within the time limit are reopened
in QGIS; the rest are reported instead of freezing the application.

IMPORTANT: no GDAL here.  Calling GDAL (e.g. ``gdal.OpenEx``) from several
Python threads inside QGIS crashes the process: driver probing is not thread
safe there.  The test therefore uses only plain file reads and Python's own
``sqlite3`` module (a separate SQLite library, opened read-only), which
still exercises SMB reachability and SQLite file locking.  No QGIS objects
are used either.
"""

import os
import queue
import sqlite3
import sys
import threading
import time
from pathlib import Path

RUNNING, OK, FAILED, TIMEOUT = "running", "ok", "failed", "timeout"

_SQLITE_EXTS = {".gpkg", ".sqlite", ".db", ".spatialite", ".gpkx"}
_SIDECARS = {".shp": (".shx", ".dbf")}


def _read_head(path, size=4096):
    with open(path, "rb") as fh:
        fh.read(size)


def _open_test(path, timeout_s):
    """Raise on failure; return normally when the file is readable."""
    ext = os.path.splitext(path)[1].lower()
    _read_head(path)
    for side_ext in _SIDECARS.get(ext, ()):
        side = os.path.splitext(path)[0] + side_ext
        if os.path.exists(side):
            _read_head(side, 512)
    # SQLite lock test only on Windows: on POSIX, closing any descriptor of a
    # file drops every lock this process holds on it, including GDAL's.
    if ext in _SQLITE_EXTS and sys.platform == "win32":
        uri = Path(path).as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=timeout_s, check_same_thread=False)
        try:
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            con.close()


class Preflight:
    """Tests many files with a few daemon worker threads; poll with ``poll()``."""

    def __init__(self, items, workers=4, timeout_s=15):
        """items: iterable of (key, path)."""
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._state = {}  # key -> [status, started_at, message, elapsed]
        self._queue = queue.Queue()
        self._total = 0
        for key, path in items:
            self._queue.put((key, path))
            self._state[key] = [None, None, "", 0.0]
            self._total += 1
        self._last_progress = time.time()
        for i in range(max(1, min(workers, self._total))):
            threading.Thread(target=self._worker, name=f"NLR-preflight-{i}", daemon=True).start()

    def _worker(self):
        while True:
            try:
                key, path = self._queue.get_nowait()
            except queue.Empty:
                return
            start = time.time()
            with self._lock:
                if self._state[key][0] == TIMEOUT:  # given up on while queued
                    continue
                self._state[key][0:2] = [RUNNING, start]
            try:
                _open_test(path, self.timeout_s)
                ok, msg = True, ""
            except Exception as exc:  # OSError, sqlite3.Error, ...
                ok, msg = False, str(exc) or exc.__class__.__name__
            with self._lock:
                entry = self._state[key]
                if entry[0] == RUNNING:  # not already declared timed out
                    entry[0] = OK if ok else FAILED
                    entry[2] = msg
                    entry[3] = time.time() - start
                self._last_progress = time.time()

    def poll(self):
        """Mark stuck items as timed out.  Returns (finished_count, total, all_done)."""
        now = time.time()
        with self._lock:
            stalled = now - self._last_progress > self.timeout_s
            for entry in self._state.values():
                status, started = entry[0], entry[1]
                if status == RUNNING and now - started > self.timeout_s:
                    entry[0], entry[2], entry[3] = TIMEOUT, f"no answer in {self.timeout_s} s", now - started
                elif status is None and stalled:
                    # every worker is stuck on a hung file: give up on the rest
                    entry[0], entry[2] = TIMEOUT, "not tested, workers stuck on other files"
            finished = sum(1 for e in self._state.values() if e[0] in (OK, FAILED, TIMEOUT))
        return finished, self._total, finished == self._total

    def results(self):
        """key -> (status, message, elapsed)."""
        with self._lock:
            return {k: (e[0], e[2], e[3]) for k, e in self._state.items()}
