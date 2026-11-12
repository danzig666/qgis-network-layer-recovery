"""Layer recovery engine.

Pipeline (all QGIS calls on the main thread):

    collect candidate layers  ->  wait for shares (async probes)
        ->  per layer: reload()  ->  health check
                       -> (if unhealthy) setDataSource()  ->  health check
        ->  refresh canvas  ->  report

Layers in edit mode are never touched: re-creating an OGR provider during an
edit session can silently discard the edit buffer.
"""

import time

from qgis.core import (
    Qgis,
    QgsDataProvider,
    QgsFeatureRequest,
    QgsMapLayer,
    QgsMessageLog,
    QgsProject,
    QgsProviderRegistry,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorDataProvider,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from . import config
from .network_probe import AsyncProbe, PathClassifier, normalize, path_root

LOG_TAG = "Network Layer Recovery"

FILE_PROVIDERS = {"gdal", "ogr", "delimitedtext", "spatialite", "mdal", "pdal"}


def log(message, level=None):
    if level is None:
        level = Qgis.MessageLevel.Info
    QgsMessageLog.logMessage(message, LOG_TAG, level)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _flags_to_int(flags):
    try:
        return int(flags)
    except TypeError:
        return int(getattr(flags, "value", 0))


def _edit_capability_mask():
    names = ("AddFeatures", "DeleteFeatures", "ChangeAttributeValues", "ChangeGeometries")
    mask = 0
    for name in names:
        value = None
        cap_enum = getattr(Qgis, "VectorProviderCapability", None)
        if cap_enum is not None:
            value = getattr(cap_enum, name, None)
        if value is None:
            value = getattr(QgsVectorDataProvider, name, None)
        if value is not None:
            mask |= _flags_to_int(value)
    return mask


EDIT_CAPS_MASK = _edit_capability_mask()


def layer_file_path(layer):
    """Local/UNC file path backing a layer, or None if not file based."""
    if layer is None or layer.providerType() not in FILE_PROVIDERS:
        return None
    try:
        parts = QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source())
    except Exception:
        parts = {}
    path = parts.get("path") if parts else None
    if not path and layer.providerType() in ("gdal", "ogr"):
        path = layer.source().split("|")[0]
    return normalize(path) if path else None


def _vector_edit_caps(layer):
    provider = layer.dataProvider()
    if provider is None:
        return 0
    return _flags_to_int(provider.capabilities()) & EDIT_CAPS_MASK


def is_busy_editing(layer):
    return isinstance(layer, QgsVectorLayer) and (layer.isEditable() or layer.isModified())


class CapabilitySnapshot:
    """Remembers which edit capabilities each vector layer had when healthy.

    A provider whose file handle went stale is often re-opened read-only, so
    the layer still draws but cannot be edited.  Comparing against the
    snapshot detects exactly that symptom.
    """

    def __init__(self):
        self._caps = {}

    def capture(self, layers):
        for layer in layers:
            if isinstance(layer, QgsVectorLayer) and layer.isValid():
                caps = _vector_edit_caps(layer)
                if caps:
                    self._caps[layer.id()] = caps

    def capture_project(self):
        self.capture(QgsProject.instance().mapLayers().values())

    def forget(self, layer_ids):
        for lid in layer_ids:
            self._caps.pop(lid, None)

    def clear(self):
        self._caps.clear()

    def lost_capabilities(self, layer):
        expected = self._caps.get(layer.id())
        if not expected:
            return False
        return (_vector_edit_caps(layer) & expected) != expected


def check_health(layer, snapshot):
    """Return (ok, reason).  Must be called with the share reachable."""
    if not layer.isValid():
        return False, "layer invalid"
    provider = layer.dataProvider()
    if provider is None or not provider.isValid():
        return False, "data provider invalid"

    try:
        if isinstance(layer, QgsVectorLayer):
            request = QgsFeatureRequest()
            request.setLimit(1)
            for _feature in layer.getFeatures(request):
                break
            if snapshot.lost_capabilities(layer):
                return False, "editing capabilities lost"
        elif isinstance(layer, QgsRasterLayer):
            extent = provider.extent()
            if extent.isEmpty():
                return False, "empty extent"
            # Read a tiny window in the middle of the raster: cheap, but it
            # forces GDAL to really hit the file.
            xs, ys = max(provider.xSize(), 1), max(provider.ySize(), 1)
            px_w, px_h = extent.width() / xs, extent.height() / ys
            c = extent.center()
            window = QgsRectangle(c.x() - px_w * 2, c.y() - px_h * 2, c.x() + px_w * 2, c.y() + px_h * 2)
            block = provider.block(1, window, 4, 4)
            if block is None or not block.isValid():
                return False, "raster read failed"
    except Exception as exc:
        return False, f"read test raised {exc}"
    return True, ""


def full_reopen(layer):
    """Re-create the data provider in place (keeps id, style, joins...)."""
    source, name, provider_type = layer.source(), layer.name(), layer.providerType()
    subset = layer.subsetString() if isinstance(layer, QgsVectorLayer) else None
    options = QgsDataProvider.ProviderOptions()
    options.transformContext = QgsProject.instance().transformContext()
    layer.setDataSource(source, name, provider_type, options)
    if subset and layer.subsetString() != subset:
        layer.setSubsetString(subset)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
class RecoveryReport:
    def __init__(self, reason):
        self.reason = reason
        self.started = time.time()
        self.finished = None
        self.reloaded = []  # names
        self.reopened = []
        self.failed = []  # (name, why)
        self.skipped_editing = []
        self.unreachable = []  # (name, path)
        self.network_wait_s = 0.0
        self.timed_out = False

    @property
    def touched(self):
        return len(self.reloaded) + len(self.reopened)

    def summary(self):
        bits = []
        if self.reloaded or self.reopened:
            bits.append(f"{self.touched} layer(s) recovered")
        if self.failed:
            bits.append(f"{len(self.failed)} failed")
        if self.skipped_editing:
            bits.append(f"{len(self.skipped_editing)} skipped (in edit mode)")
        if self.unreachable:
            bits.append(f"{len(self.unreachable)} unreachable")
        return ", ".join(bits) if bits else "no network layers needed recovery"

    def details(self):
        lines = [f"Trigger: {self.reason}"]
        if self.network_wait_s:
            lines.append(f"Waited {self.network_wait_s:.0f} s for network shares"
                         + (" (timed out)" if self.timed_out else ""))
        sections = [
            ("Reloaded (provider reopened)", self.reloaded),
            ("Fully re-created (setDataSource)", self.reopened),
            ("Failed", [f"{n} - {why}" for n, why in self.failed]),
            ("Skipped - in edit mode, save or roll back then run 'Recover selected layers'",
             self.skipped_editing),
            ("Unreachable - share/file not available", [f"{n} - {p}" for n, p in self.unreachable]),
        ]
        for title, items in sections:
            if items:
                lines.append("")
                lines.append(f"{title}:")
                lines.extend(f"  - {item}" for item in items)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------
class RecoveryEngine(QObject):
    """Asynchronous (event-loop driven) recovery state machine."""

    status = pyqtSignal(str)
    finished = pyqtSignal(object)  # RecoveryReport

    IDLE, DELAY, PROBING, RECOVERING = range(4)

    def __init__(self, iface, snapshot, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.snapshot = snapshot
        self.state = self.IDLE
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._step)
        self._reset()

    def _reset(self):
        self._report = None
        self._targets = {}  # layer_id -> path
        self._queue = []
        self._probe = None
        self._wait_started = 0.0
        self._successes = 0
        self._required = 1
        self._timeout = 0
        self._canvas_frozen = False

    @property
    def running(self):
        return self.state != self.IDLE

    # -- public API ------------------------------------------------------------
    def start(self, reason, layers=None, wait_for_network=True):
        """Begin a recovery run.  ``layers=None`` means every candidate layer.

        Explicitly passed layers are always candidates (even on local disk).
        """
        if self.running:
            log("Recovery already in progress - request ignored")
            return False
        self._reset()
        self._report = RecoveryReport(reason)

        if layers is None:
            classifier = PathClassifier(
                include_removable=config.get("include_removable"),
                include_local=config.get("include_local"),
            )
            layers = QgsProject.instance().mapLayers().values()
        else:
            classifier = None

        for layer in layers:
            path = layer_file_path(layer)
            if not path:
                continue
            if classifier is not None and not classifier.is_candidate(path):
                continue
            self._targets[layer.id()] = path

        log(f"Recovery started ({reason}); {len(self._targets)} candidate layer(s)")
        if not self._targets:
            self._finish()
            return True

        if wait_for_network:
            self._required = max(1, config.get("required_successes"))
            self._timeout = max(10, config.get("network_timeout_s"))
            self.state = self.DELAY
            self.status.emit("System resumed - waiting for network drives...")
            self._timer.start(max(0, config.get("initial_delay_s")) * 1000)
        else:
            self._required = 1
            self._timeout = 20
            self.state = self.DELAY
            self._timer.start(0)
        return True

    def cancel(self):
        if not self.running:
            return
        self._timer.stop()
        self._unfreeze()
        log("Recovery cancelled", Qgis.MessageLevel.Warning)
        self.state = self.IDLE
        self._reset()

    # -- state machine ------------------------------------------------------
    def _step(self):
        try:
            if self.state == self.DELAY:
                self._wait_started = time.time()
                self._start_probe()
            elif self.state == self.PROBING:
                self._poll_probe()
            elif self.state == self.RECOVERING:
                self._recover_next()
        except Exception as exc:  # never leave the engine stuck
            log(f"Unexpected error: {exc!r}", Qgis.MessageLevel.Critical)
            self._unfreeze()
            if self._report:
                self._report.failed.append(("<engine>", repr(exc)))
            self._finish()

    def _start_probe(self):
        self.state = self.PROBING
        self._probe = AsyncProbe(sorted(set(self._targets.values())))
        self._timer.start(250)

    def _poll_probe(self):
        waited = time.time() - self._wait_started
        if not self._probe.done:
            if waited > self._timeout:
                self._network_timeout(None)
            else:
                self._timer.start(250)
            return

        result = self._probe.result
        if result is not None and result.all_roots_ok:
            self._successes += 1
            log(f"Probe OK ({self._successes}/{self._required}) in {result.elapsed:.1f} s")
            if self._successes >= self._required:
                self._report.network_wait_s = waited
                self._begin_recovery(result)
                return
        else:
            self._successes = 0
            if result is not None:
                log("Shares not reachable yet: " + ", ".join(result.unreachable_roots()))

        if waited > self._timeout:
            self._network_timeout(result)
            return
        self.status.emit(f"Waiting for network drives... ({int(waited)} s)")
        self._probe = None
        self.state = self.DELAY
        self._timer.start(max(1, config.get("probe_interval_s")) * 1000)

    def _network_timeout(self, result):
        """Recover what is reachable, report the rest."""
        self._report.timed_out = True
        self._report.network_wait_s = time.time() - self._wait_started
        log("Timed out waiting for network shares", Qgis.MessageLevel.Warning)
        if result is None:
            # The probe itself is hung on a dead share: nothing is safe.
            self._mark_unreachable(lambda path: True)
            self._finish()
            return
        self._begin_recovery(result)

    def _mark_unreachable(self, predicate):
        project = QgsProject.instance()
        for lid, path in list(self._targets.items()):
            if predicate(path):
                layer = project.mapLayer(lid)
                self._report.unreachable.append((layer.name() if layer else lid, path))
                del self._targets[lid]

    def _begin_recovery(self, result):
        bad_roots = set(result.unreachable_roots())
        self._mark_unreachable(lambda p: path_root(p) in bad_roots)
        missing = set(result.missing_files())
        self._mark_unreachable(lambda p: p in missing)

        self._queue = list(self._targets.keys())
        self.state = self.RECOVERING
        canvas = self.iface.mapCanvas()
        canvas.stopRendering()  # render jobs hold cloned providers
        canvas.freeze(True)
        self._canvas_frozen = True
        self._timer.start(0)

    def _recover_next(self):
        if not self._queue:
            self._unfreeze()
            self.iface.mapCanvas().refreshAllLayers()
            self._finish()
            return
        lid = self._queue.pop(0)
        layer = QgsProject.instance().mapLayer(lid)
        if layer is not None:
            done = len(self._targets) - len(self._queue)
            self.status.emit(f"Recovering layers {done}/{len(self._targets)}: {layer.name()}")
            self._recover_layer(layer)
        self._timer.start(0)  # yield to the event loop between layers

    def _recover_layer(self, layer):
        name = layer.name()
        if is_busy_editing(layer):
            self._report.skipped_editing.append(name)
            log(f"{name}: skipped (edit mode)", Qgis.MessageLevel.Warning)
            return

        why = "layer invalid"
        try:
            if layer.isValid() and layer.dataProvider() is not None:
                layer.reload()
                ok, why = check_health(layer, self.snapshot)
                if ok:
                    self._report.reloaded.append(name)
                    self.snapshot.capture([layer])
                    layer.triggerRepaint()
                    log(f"{name}: reloaded")
                    return
                log(f"{name}: reload not sufficient ({why})")
        except Exception as exc:
            why = f"reload raised {exc}"

        if not config.get("allow_full_reopen"):
            self._report.failed.append((name, why))
            return
        try:
            full_reopen(layer)
            ok, why2 = check_health(layer, self.snapshot)
        except Exception as exc:
            ok, why2 = False, f"setDataSource raised {exc}"
        if ok:
            self._report.reopened.append(name)
            self.snapshot.capture([layer])
            layer.triggerRepaint()
            log(f"{name}: provider re-created")
        else:
            self._report.failed.append((name, why2))
            log(f"{name}: recovery failed ({why2})", Qgis.MessageLevel.Warning)

    def _unfreeze(self):
        if self._canvas_frozen:
            self.iface.mapCanvas().freeze(False)
            self._canvas_frozen = False

    def _finish(self):
        report = self._report
        self.state = self.IDLE
        self._reset()
        if report is None:
            return
        report.finished = time.time()
        log("Recovery finished: " + report.summary() + "\n" + report.details())
        self.finished.emit(report)
