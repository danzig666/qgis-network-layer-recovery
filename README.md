<img src="assets/icon_512.png" width="128" align="right" alt="icon">

# Network Layer Recovery – QGIS plugin

Fixes a problem with QGIS projects opened from a network drive (SMB, UNC `\\server\share`, or a mapped `X:` drive). After the PC sleeps or hibernates, the layers misbehave: some (mostly rasters) don't render, and others load but can't be edited.

## Cause

Sleep breaks the SMB connection. Windows reconnects the share after wake-up, but the GDAL/OGR data-source handles that QGIS holds stay stale. Refreshing the map canvas doesn't help: the layer's data provider has to be reopened.

## How it works

```
detect resume ──► wait for network ──► check layers ──► background read test ──► reopen broken ──► report
```

1. **Resume detection** (`power_monitor.py`)
   - Uses Windows `PowerRegisterSuspendResumeNotification` (powrprof.dll), which also reports Modern Standby (S0ix) transitions. The callback runs on an OS thread, so it only sets a flag and never touches QGIS objects.
   - Fallback: a timer ticks every 5 seconds. If much more time passed between two ticks than the threshold (60 s by default), the plugin treats it as a sleep.
2. **Waiting for the network** (`network_probe.py`)
   - Only network layers are handled: UNC paths, or drives where `GetDriveTypeW == DRIVE_REMOTE`. Optionally it also handles removable USB drives, or every file-based layer.
   - Share roots and files are checked on **background threads** (`stat` plus a directory listing), so a dead share never freezes QGIS.
   - The plugin waits for N consecutive successful checks (2 by default). If it times out, it still recovers the layers on reachable shares and reports the rest.
3. **Health check** (`recovery.py`): every candidate layer gets a cheap check on the main thread, in 50 ms slices so the UI stays responsive (about 10 ms per layer).
   - rasters: a real read of a small window in the middle of the image;
   - vectors: fetching one feature (a layer with features that returns none counts as broken), plus a check that the layer **hasn't lost its editing capabilities**. For this it snapshots each layer's capabilities when the layer is loaded.
   - **Healthy layers are never reloaded.** A manual run on a healthy 600+ layer project takes seconds.
4. **Background read test** (`preflight.py`): each broken file is first read on a background thread. This uses plain file reads, plus a read-only SQLite query for GeoPackages, which also tests SQLite locks. A file that fails or doesn't answer within the limit (15 s by default) is **reported, not reopened**, because reopening it on the main thread could freeze QGIS. (No GDAL here: calling GDAL from several threads inside QGIS crashes it.)
5. **Recovery**: runs on the main thread, one layer at a time, handing control back to the event loop between layers.
   - **Layers in edit mode are skipped**, because reopening the provider could lose unsaved edits.
   - First it calls `layer.reload()`. The OGR/GDAL provider closes and reopens its data source, which also invalidates the OGR connection pool. The health check runs again afterwards.
   - If the reload wasn't enough, `setDataSource()` rebuilds the provider from the same source. The layer's ID, style, joins/relations and subset filter are kept.
   - Finally it stops any rendering in progress and fully refreshes the canvas, cache included (`refreshAllLayers`).
6. **Report**: a summary appears in the message bar, with a "Details" button showing phase timings and any slow operations (> 1 s). The full log is in the *Log Messages → Network Layer Recovery* tab.

## Manual commands (Plugins → Network Layer Recovery)

- **Recover network layers now**: checks every network layer and reopens only the broken ones. It's also a toolbar button.
- **Recover selected layers**: reopens the layers selected in the Layers panel, including ones on local disks. Use this for a layer that was skipped because it was in edit mode, after you've saved or rolled back its edits.
- **Force reopen all network layers**: reopens every network layer, even healthy-looking ones. This is slow on big projects.
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
