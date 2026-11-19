"""QGIS glue: actions, menu, message bar, signal wiring."""

import os

from qgis.core import Qgis, QgsProject
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QMessageBox, QPushButton

try:  # Qt6 / QGIS 4
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3
    from qgis.PyQt.QtWidgets import QAction

from . import config
from .power_monitor import PowerMonitor
from .recovery import CapabilitySnapshot, RecoveryEngine, log
from .settings_dialog import SettingsDialog

MENU = "&Network Layer Recovery"
TITLE = "Network Layer Recovery"


class NetworkLayerRecoveryPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.actions = []
        self.monitor = None
        self.engine = None
        self.snapshot = CapabilitySnapshot()
        self._status_item = None
        self._connections = []

    # ------------------------------------------------------------------ setup
    def initGui(self):  # noqa: N802
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.png"))
        window = self.iface.mainWindow()

        self.action_all = QAction(icon, "Recover network layers now", window)
        self.action_all.setToolTip(
            "Check every layer stored on a network drive and reopen the broken ones")
        self.action_all.triggered.connect(self.recover_all)

        self.action_selected = QAction("Recover selected layers", window)
        self.action_selected.setToolTip(
            "Reopen the layers selected in the Layers panel (always reopened, no health check)")
        self.action_selected.triggered.connect(self.recover_selected)

        self.action_force = QAction("Force reopen all network layers", window)
        self.action_force.setToolTip(
            "Reopen every network layer, even if it looks healthy (slow on big projects)")
        self.action_force.triggered.connect(self.force_all)

        self.action_settings = QAction("Settings...", window)
        self.action_settings.triggered.connect(self.show_settings)

        for action in (self.action_all, self.action_selected, self.action_force,
                       self.action_settings):
            self.iface.addPluginToMenu(MENU, action)
            self.actions.append(action)
        self.iface.addToolBarIcon(self.action_all)

        self.engine = RecoveryEngine(self.iface, self.snapshot)
        self.engine.status.connect(self._on_status)
        self.engine.finished.connect(self._on_finished)

        self.monitor = PowerMonitor(gap_threshold_seconds=config.get("gap_threshold_s"))
        self.monitor.resumed.connect(self._on_resumed)
        self.monitor.suspended.connect(lambda: log("System is suspending"))
        self.monitor.blocked.connect(lambda s: log(
            f"QGIS main thread was busy for {s} s (not a sleep) - no recovery needed"))
        self.monitor.start()
        if self.monitor.native_available:
            log("Windows suspend/resume notifications registered")
        else:
            log(f"OS power notifications unavailable ({self.monitor.native_error}); "
                "using sleep-time detection only")

        project = QgsProject.instance()
        self._connect(project.layersAdded, self._on_layers_added)
        self._connect(project.layersWillBeRemoved, self.snapshot.forget)
        self._connect(project.cleared, self.snapshot.clear)
        self.snapshot.capture_project()

    def _connect(self, signal, slot):
        signal.connect(slot)
        self._connections.append((signal, slot))

    def unload(self):
        if self.monitor:
            self.monitor.stop()
            self.monitor.deleteLater()
            self.monitor = None
        if self.engine:
            self.engine.cancel()
            self.engine.deleteLater()
            self.engine = None
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._connections = []
        self._clear_status()
        self.iface.removeToolBarIcon(self.action_all)
        for action in self.actions:
            self.iface.removePluginMenu(MENU, action)
            action.deleteLater()
        self.actions = []

    # ---------------------------------------------------------------- actions
    def recover_all(self):
        self.engine.start("manual: all network layers", layers=None, wait_for_network=False)

    def force_all(self):
        self.engine.start("manual: force reopen all", layers=None, wait_for_network=False,
                          force=True)

    def recover_selected(self):
        layers = self.iface.layerTreeView().selectedLayers()
        if not layers:
            self.iface.messageBar().pushMessage(
                TITLE, "Select one or more layers in the Layers panel first.",
                Qgis.MessageLevel.Info, 5)
            return
        self.engine.start("manual: selected layers", layers=layers, wait_for_network=False,
                          force=True)

    def show_settings(self):
        dialog = SettingsDialog(self.iface.mainWindow())
        if dialog.exec():
            dialog.save()
            if self.monitor:
                self.monitor.gap_threshold_seconds = max(10, config.get("gap_threshold_s"))

    # ----------------------------------------------------------------- events
    def _on_layers_added(self, layers):
        # Providers are ready immediately, but defer so we never slow down
        # project loading.
        QTimer.singleShot(0, lambda: self.snapshot.capture(
            [lyr for lyr in layers if QgsProject.instance().mapLayer(lyr.id()) is not None]))

    def _on_resumed(self, reason):
        log(f"Resume detected ({reason})")
        if not QgsProject.instance().mapLayers():
            return
        if config.get("auto_recover"):
            self.engine.start(f"resume: {reason}", layers=None, wait_for_network=True)
            return
        bar = self.iface.messageBar()
        item = bar.createMessage(TITLE, "The computer woke up from sleep. "
                                 "Network layers may need to be reopened.")
        button = QPushButton("Recover now")
        button.clicked.connect(lambda: (bar.popWidget(item), self.engine.start(
            f"resume (confirmed): {reason}", layers=None, wait_for_network=True)))
        item.layout().addWidget(button)
        bar.pushWidget(item, Qgis.MessageLevel.Info, 0)

    def _on_status(self, text):
        if self._status_item is not None:
            try:
                self._status_item.setText(text)
                return
            except RuntimeError:  # user closed it
                self._status_item = None
        bar = self.iface.messageBar()
        item = bar.createMessage(TITLE, text)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.engine.cancel)
        cancel.clicked.connect(self._clear_status)
        item.layout().addWidget(cancel)
        bar.pushWidget(item, Qgis.MessageLevel.Info, 0)
        self._status_item = item

    def _clear_status(self):
        if self._status_item is not None:
            try:
                self.iface.messageBar().popWidget(self._status_item)
            except RuntimeError:
                pass
            self._status_item = None

    def _on_finished(self, report):
        self._clear_status()
        problems = report.failed or report.skipped_editing or report.unreachable
        if not problems and not report.touched and report.reason.startswith("resume"):
            return  # nothing to say
        level = Qgis.MessageLevel.Warning if problems else Qgis.MessageLevel.Success
        bar = self.iface.messageBar()
        item = bar.createMessage(TITLE, report.summary())
        details = QPushButton("Details")
        details.clicked.connect(lambda: QMessageBox.information(
            self.iface.mainWindow(), TITLE, report.details()))
        item.layout().addWidget(details)
        bar.pushWidget(item, level, 0 if problems else 8)
