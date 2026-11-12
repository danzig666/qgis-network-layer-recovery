<img src="assets/icon_512.png" width="128" align="right" alt="icon">

# Network Layer Recovery – QGIS plugin

Fixes a problem with QGIS projects opened from a network drive (SMB, UNC `\\server\share`, or a mapped `X:` drive). After the PC sleeps or hibernates, the layers misbehave: some (mostly rasters) don't render, and others load but can't be edited.

## Cause

Sleep breaks the SMB connection. Windows reconnects the share after wake-up, but the GDAL/OGR data-source handles that QGIS holds stay stale. Refreshing the map canvas doesn't help: the layer's data provider has to be reopened.

## How it works

```
detect resume ──► wait for the network ──► reopen layers ──► report
```

1. **Resume detection** (`power_monitor.py`)
   - Uses Windows `PowerRegisterSuspendResumeNotification` (powrprof.dll), which also reports Modern Standby (S0ix) transitions. The callback runs on an OS thread, so it only sets a flag and never touches QGIS objects.
   - Fallback: a timer ticks every 5 seconds. If much more time passed between two ticks than the threshold (60 s by default), the plugin treats it as a sleep.
2. **Waiting for the network** (`network_probe.py`)
   - Only network layers are handled: UNC paths, or drives where `GetDriveTypeW == DRIVE_REMOTE`. Optionally it also handles removable USB drives, or every file-based layer.
   - Share roots and files are checked on **background threads** (`stat` plus a directory listing), so a dead share never freezes QGIS.
   - The plugin waits for N consecutive successful checks (2 by default). If it times out, it still recovers the layers on reachable shares and reports the rest.
3. **Recovery** (`recovery.py`): runs on the main thread, one layer at a time, handing control back to the event loop between layers.
   - **Layers in edit mode are skipped**, because reopening the provider could lose unsaved edits.
   - First it calls `layer.reload()`. The OGR/GDAL provider closes and reopens its data source, which also invalidates the OGR connection pool.
   - Then it runs a health check:
     - rasters: a real read from the middle of the image;
     - vectors: fetching one feature, plus a check that the layer **hasn't lost its editing capabilities**. For this it snapshots each layer's capabilities when the layer is loaded.
   - If the reload wasn't enough, `setDataSource()` rebuilds the provider from the same source. The layer's ID, style, joins/relations and subset filter are kept.
   - Finally it stops any rendering in progress and fully refreshes the canvas, cache included (`refreshAllLayers`).
4. **Report**: a summary appears in the message bar, with a "Details" button. The full log is in the *Log Messages → Network Layer Recovery* tab.

## Manual commands (Plugins → Network Layer Recovery)

- **Recover network layers now**: reopens every network layer immediately. It's also a toolbar button.
- **Recover selected layers**: reopens the layers selected in the Layers panel, including ones on local disks. Use this for a layer that was skipped because it was in edit mode, after you've saved or rolled back its edits.
- **Settings…**: automatic or ask-first mode, removable drives, timings.

## Installation

- **From ZIP:** download `network_layer_recovery.zip` from the [Releases](https://github.com/danzig666/qgis-network-layer-recovery/releases/latest) page. Then in QGIS, go to *Plugins → Manage and Install Plugins → Install from ZIP*.
- **For development:** copy or symlink the `network_layer_recovery` folder into `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`.

Works with QGIS 3.22+ (Qt5) and QGIS 4 (Qt6) (`supportsQt6=True`).

## Limitations

- If Windows doesn't reconnect the share, the plugin can't recover it either; after the timeout it lists the unreachable layers. Open the drive in File Explorer, then run the manual command.
- A layer in edit mode with a stale handle isn't recovered automatically: save or roll back its edits first, then use *Recover selected layers*.
- GeoPackage over SMB in WAL mode is an SQLite limitation the plugin can't work around.
- The wall-clock fallback can also trigger a recovery (harmless) when the QGIS main thread was blocked for more than 60 seconds.

## License

GPL-2.0-or-later (see [LICENSE](LICENSE)).
