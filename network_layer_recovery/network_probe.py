"""Path classification and non-blocking reachability probes.

Everything in this module is plain Python (no QGIS objects) so the probe can
run on a background thread.  File-system calls on a dead SMB share can block
for a long time, therefore they are executed on daemon threads and the main
thread only polls for the result.
"""

import os
import sys
import threading
import time

DRIVE_REMOVABLE = 2
DRIVE_REMOTE = 4

_VSI_PREFIXES = ("/vsizip/", "/vsigzip/", "/vsitar/", "/vsi7z/", "/vsirar/", "/vsisubfile/")


def strip_vsi(path):
    """Turn '/vsizip/X:/data/a.zip/b.tif' into 'X:/data/a.zip' (best effort)."""
    if not path:
        return path
    lowered = path.lower()
    for prefix in _VSI_PREFIXES:
        if lowered.startswith(prefix):
            inner = path[len(prefix):]
            # cut at the archive member if any
            for ext in (".zip", ".gz", ".tar", ".tgz", ".7z", ".rar"):
                idx = inner.lower().find(ext)
                if idx != -1:
                    return inner[: idx + len(ext)]
            return inner
    if lowered.startswith("/vsi"):
        return None  # /vsicurl/, /vsis3/ ... remote but not a file share
    return path


def normalize(path):
    if not path:
        return None
    path = strip_vsi(path)
    if not path:
        return None
    if path.lower().startswith("file:///"):
        path = path[8:]
    return os.path.normpath(path)


def path_root(path):
    """'X:' for drive paths, '\\\\server\\share' for UNC paths, '/' elsewhere."""
    drive, _ = os.path.splitdrive(path)
    return drive or os.sep


def _drive_type(root):
    if sys.platform != "win32" or len(root) != 2 or root[1] != ":":
        return None
    try:
        import ctypes

        return ctypes.windll.kernel32.GetDriveTypeW(root + "\\")
    except Exception:
        return None


class PathClassifier:
    """Decides whether a path lives on a network (or removable) drive."""

    def __init__(self, include_removable=True, include_local=False):
        self.include_removable = include_removable
        self.include_local = include_local
        self._cache = {}

    def is_candidate(self, path):
        if self.include_local:
            return True
        root = path_root(path)
        if root.startswith("\\\\") or root.startswith("//"):
            return True  # UNC share
        if root not in self._cache:
            self._cache[root] = _drive_type(root)
        dtype = self._cache[root]
        if dtype == DRIVE_REMOTE:
            return True
        if dtype == DRIVE_REMOVABLE and self.include_removable:
            return True
        if sys.platform != "win32":
            # No reliable cheap test elsewhere: treat mounts outside / as remote.
            return os.path.ismount(root) and root != os.sep
        return False


class ProbeResult:
    def __init__(self):
        self.roots = {}  # root -> bool
        self.files = {}  # path -> bool
        self.elapsed = 0.0

    @property
    def all_roots_ok(self):
        return all(self.roots.values()) if self.roots else True

    def unreachable_roots(self):
        return sorted(r for r, ok in self.roots.items() if not ok)

    def missing_files(self):
        return sorted(p for p, ok in self.files.items() if not ok)


def _probe(paths):
    result = ProbeResult()
    start = time.time()
    roots = sorted({path_root(p) for p in paths})
    for root in roots:
        try:
            ok = os.path.isdir(root + os.sep)
            if ok:
                # Listing forces SMB to actually talk to the server rather
                # than answering from a cached directory entry.
                with os.scandir(root + os.sep) as it:
                    next(it, None)
        except OSError:
            ok = False
        result.roots[root] = ok
    for path in paths:
        if not result.roots.get(path_root(path), False):
            result.files[path] = False
            continue
        try:
            os.stat(path)
            result.files[path] = True
        except OSError:
            result.files[path] = False
    result.elapsed = time.time() - start
    return result


class AsyncProbe:
    """Runs one probe round on a daemon thread; poll ``done``/``result``."""

    def __init__(self, paths):
        self._paths = list(paths)
        self._result = None
        self._error = None
        self._done = threading.Event()
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._run, name="NLR-probe", daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self._result = _probe(self._paths)
        except Exception as exc:  # pragma: no cover - defensive
            self._error = exc
        finally:
            self._done.set()

    @property
    def done(self):
        return self._done.is_set()

    @property
    def result(self):
        return self._result

    @property
    def error(self):
        return self._error
