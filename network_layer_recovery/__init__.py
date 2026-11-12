"""Network Layer Recovery - QGIS plugin entry point."""


def classFactory(iface):  # noqa: N802 (QGIS API name)
    from .plugin import NetworkLayerRecoveryPlugin

    return NetworkLayerRecoveryPlugin(iface)
