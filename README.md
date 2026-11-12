<img src="assets/icon_512.png" width="128" align="right" alt="icon">

# Network Layer Recovery – QGIS plugin

Megoldja azt a problémát, hogy hálózati meghajtóról (SMB, UNC `\\szerver\megosztás`, csatolt `X:` meghajtó) nyitott QGIS-projekt a gép alvása/hibernálása után hibásan működik: egyes rétegek (főleg raszterek) nem rajzolódnak ki, mások betöltődnek, de nem szerkeszthetők.

## Ok

Alvás közben megszakad az SMB-kapcsolat. A Windows ébredés után újracsatlakozik, de a QGIS által nyitva tartott GDAL/OGR adatforrás-kezelők (file handle-ök) elavultak maradnak. A vászon frissítése ezt nem oldja meg, az adatszolgáltatót (data provider) újra kell nyitni.

## Működés

```
ébredés észlelése ──► várakozás a hálózatra ──► rétegek újranyitása ──► jelentés
```

1. **Ébredés észlelése** (`power_monitor.py`)
   - Windows `PowerRegisterSuspendResumeNotification` (powrprof.dll). Ez Modern Standby (S0ix) esetén is jelez. A visszahívás egy OS-szálon fut, ezért csak egy jelzőt állít be, QGIS-objektumhoz nem nyúl.
   - Tartalék: 5 másodpercenként futó időzítő. Ha két ütés között a beállított küszöbnél (alapból 60 s) több idő telt el, azt alvásnak veszi.
2. **Várakozás a hálózatra** (`network_probe.py`)
   - Csak a hálózati rétegeket kezeli (UNC útvonal vagy `GetDriveTypeW == DRIVE_REMOTE`; opcionálisan a cserélhető USB-meghajtókat vagy minden fájlalapú réteget is).
   - A megosztások gyökerét és a fájlokat **háttérszálon** ellenőrzi (`stat` + könyvtárlistázás), így egy halott megosztás sem fagyasztja le a QGIS-t.
   - N egymást követő sikeres ellenőrzést vár (alapból 2). Időtúllépés esetén az elérhető rétegeket helyreállítja, a többit jelenti.
3. **Helyreállítás** (`recovery.py`, a fő szálon, rétegenként az eseményhurokba visszaadva a vezérlést)
   - A **szerkesztés alatt álló rétegeket kihagyja**, mert a szolgáltató újranyitása elveszítheti a nem mentett módosításokat.
   - Először `layer.reload()` fut: az OGR/GDAL provider bezárja és újranyitja az adatforrást, az OGR kapcsolatpoolt is érvényteleníti.
   - Ezután állapotellenőrzés: raszternél valódi olvasás a kép közepéből, vektornál egy feature lekérése, valamint annak ellenőrzése, hogy a réteg **nem vesztette-e el a szerkesztési képességeit** (ehhez betöltéskor pillanatképet készít).
   - Ha ez nem elég, `setDataSource()` újraépíti a providert ugyanazzal a forrással. A réteg azonosítója, stílusa, kapcsolatai és subset-szűrője megmarad.
   - A végén leállítja a renderelést, majd teljesen frissíti a vásznat (`refreshAllLayers`) a gyorsítótárral együtt.
4. **Jelentés** az üzenetsávban („Details” gombbal), részletesen a *Log Messages → Network Layer Recovery* fülön.

## Kézi parancsok (Plugins → Network Layer Recovery)

- **Recover network layers now**: az összes hálózati réteg azonnali újranyitása (eszköztár-ikon is).
- **Recover selected layers**: a Rétegek panelen kijelölt rétegek újranyitása, helyi meghajtón lévőké is. Akkor hasznos, ha egy rétegről mentés vagy visszavonás után kikerült a szerkesztési mód.
- **Settings…**: automatikus/kérdezős mód, USB-meghajtók, időzítések.

## Telepítés

- **ZIP-ből:** töltsd le a `network_layer_recovery.zip` fájlt a [Releases](https://github.com/danzig666/qgis-network-layer-recovery/releases/latest) oldalról, majd *Plugins → Manage and Install Plugins → Install from ZIP*.
- **Fejlesztéshez:** másold vagy linkeld a `network_layer_recovery` mappát ide: `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`.

QGIS 3.22+ (Qt5) és QGIS 4 (Qt6) alatt is működik (`supportsQt6=True`).

## Korlátok

- Ha a Windows nem csatlakoztatja újra a megosztást, a plugin sem tudja helyreállítani. Ilyenkor időtúllépés után jelzi az elérhetetlen rétegeket. Nyisd meg a meghajtót az Intézőben, majd futtasd a kézi parancsot.
- Szerkesztés alatt álló réteg elavult kezelővel: előbb mentsd vagy vond vissza a módosításokat, aztán használd a *Recover selected layers* parancsot.
- GeoPackage SMB-n WAL módban: ez SQLite-korlát, a plugin nem tudja áthidalni.
- A tartalék időköz-alapú észlelés akkor is elindíthat egy (ártalmatlan) újratöltést, ha a QGIS fő szála 60 másodpercnél tovább blokkolva volt.

## Licenc

GPL-2.0-or-later (lásd [LICENSE](LICENSE)).
