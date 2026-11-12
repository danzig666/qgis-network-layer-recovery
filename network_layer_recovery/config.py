"""Persistent plugin settings (stored in the QGIS user profile)."""

from qgis.core import QgsSettings

PREFIX = "network_layer_recovery/"

DEFAULTS = {
    # Run recovery automatically after resume (otherwise only offer a button).
    "auto_recover": True,
    # Also treat USB / removable drives as "network" (same failure mode).
    "include_removable": True,
    # Treat every file-based layer as affected, even on local disks.
    "include_local": False,
    # Wall-clock gap that counts as a sleep when no OS notification arrives.
    "gap_threshold_s": 60,
    # Wait this long after resume before the first probe.
    "initial_delay_s": 5,
    # Seconds between probe rounds while waiting for the shares.
    "probe_interval_s": 3,
    # Consecutive successful probe rounds needed before recovering.
    "required_successes": 2,
    # Give up waiting for the network after this many seconds.
    "network_timeout_s": 180,
    # Background read test per broken file; slower files are not reopened.
    "preflight_timeout_s": 15,
    # Re-create the provider with setDataSource() if reload() is not enough.
    "allow_full_reopen": True,
}


def get(key):
    default = DEFAULTS[key]
    value = QgsSettings().value(PREFIX + key, default)
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes")
        return bool(value)
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return value


def set(key, value):  # noqa: A001 (module-level API, mirrors QgsSettings)
    QgsSettings().setValue(PREFIX + key, value)
