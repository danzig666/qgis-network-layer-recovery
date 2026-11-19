"""Settings dialog (built in code, no .ui file)."""

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)

from . import config

_BOOL_FIELDS = [
    ("auto_recover", "Recover automatically after the PC wakes up",
     "If unchecked, a message with a 'Recover now' button is shown instead."),
    ("include_removable", "Include removable (USB) drives",
     "External drives suffer from the same stale-handle problem after sleep."),
    ("include_local", "Include ALL file-based layers (also local disks)",
     "Normally only UNC paths and mapped network drives are handled."),
    ("allow_full_reopen", "Re-create the data provider if a plain reload is not enough",
     "Uses setDataSource() with the same source; style and properties are kept."),
]

_INT_FIELDS = [
    ("gap_threshold_s", "Minimum sleep to react to (s)", 10, 3600,
     "Fallback detection: the computer must have been asleep at least this long. "
     "A busy QGIS (long processing task) does not count."),
    ("initial_delay_s", "Delay after resume (s)", 0, 300,
     "Wait before the first network check."),
    ("probe_interval_s", "Network check interval (s)", 1, 120, ""),
    ("required_successes", "Successful checks required", 1, 10,
     "Consecutive successful checks before layers are reopened."),
    ("network_timeout_s", "Give up after (s)", 10, 3600,
     "Reachable shares are still recovered; others are reported."),
    ("preflight_timeout_s", "File read test limit (s)", 3, 300,
     "Broken files are first read in the background; files that do not answer "
     "in time are reported instead of being reopened (which could freeze QGIS)."),
]


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Network Layer Recovery - Settings")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self._bools = {}
        for key, label, tip in _BOOL_FIELDS:
            box = QCheckBox(label)
            box.setToolTip(tip)
            box.setChecked(config.get(key))
            form.addRow(box)
            self._bools[key] = box

        self._ints = {}
        for key, label, lo, hi, tip in _INT_FIELDS:
            spin = QSpinBox()
            spin.setRange(lo, hi)
            spin.setValue(config.get(key))
            spin.setToolTip(tip)
            form.addRow(label, spin)
            self._ints[key] = spin

        note = QLabel(
            "Layers in edit mode are never reopened automatically. "
            "Details of every run are written to the 'Network Layer Recovery' "
            "tab of the Log Messages panel."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save(self):
        for key, box in self._bools.items():
            config.set(key, box.isChecked())
        for key, spin in self._ints.items():
            config.set(key, spin.value())
