# Vermögenstracker auf dem Handy

Die Datenbank bleibt auf dem Mac. Das iPhone greift über **Tailscale** darauf zu, ein privates,
Ende-zu-Ende-verschlüsseltes Netz nur aus deinen Geräten. Es gibt keine Cloud-Kopie der Daten
und keine Synchronisation, die schiefgehen kann.

## Einmalig einrichten
1. **Tailscale** auf Mac und iPhone installieren (App Store) und auf beiden mit demselben Konto anmelden.
2. In der Tailscale-Verwaltung (login.tailscale.com → Machines) dem Mac einen **neutralen Namen** geben,
   z. B. `mac-home`. Der Name steht im HTTPS-Zertifikat, und das ist öffentlich einsehbar
   (Certificate Transparency).
3. Tailscale-App auf dem Mac → Einstellungen → **CLI installieren** (damit `tailscale` im Terminal geht).
4. **FileVault** einschalten: Systemeinstellungen → Datenschutz & Sicherheit.

## Starten

**Dauerhaft (empfohlen)**: als Hintergrunddienst, der beim Anmelden startet und sich nach einem Absturz neu startet:
```bash
cd ~/vermoegenstracker
./autostart.sh install
```
Danach musst du nichts mehr starten, auch nicht nach einem Neustart des Macs.
- `./check.sh` prüft Schritt für Schritt, warum die App erreichbar ist oder nicht.
- `./autostart.sh restart` startet neu (z. B. nach einem Update).
- `./autostart.sh logs` zeigt die letzten Meldungen.
- `./autostart.sh uninstall` entfernt den Dienst.

**Zum Ausprobieren** (im Vordergrund, Beenden mit Strg+C):
```bash
./start.sh            # Mac + Handy
./start.sh --local    # nur Mac
```

Beide Varianten
- erstellen beim Start eine Sicherung unter `data/backups/` (die letzten 30 werden behalten),
- geben **nur dein eigenes Tailscale-Konto** frei,
- starten die App auch ohne Tailscale und laufen dann nur lokal. Ist Tailscale nicht angemeldet
  oder HTTPS im Tailnet noch nicht aktiviert, steht der Grund im Terminal.

## Dauerbetrieb: Mac wach halten
Schläft der Mac, ist die App fürs Handy nicht erreichbar. Der Autostart-Dienst verhindert den
Ruhezustand, **solange der Mac am Netzteil hängt** (`caffeinate -s`). Zusätzlich in den
Systemeinstellungen → Batterie → Optionen den automatischen Ruhezustand am Netzteil bei ausgeschaltetem
Display verhindern (Wortlaut je nach macOS-Version). **Bei zugeklapptem Deckel schläft ein MacBook trotzdem.**
Für echten 24/7-Betrieb eignet sich später ein kleiner Heimserver (Raspberry Pi, Mac mini, NAS) mit
demselben Aufbau.

## Auf dem iPhone
Die Adresse in **Safari** öffnen → Teilen → **Zum Home-Bildschirm**. Die App startet dann im Vollbild
mit eigenem Symbol.

## Datenschutz im Detail
- Die App lauscht nur auf `127.0.0.1`. Aus dem WLAN ist sie nicht erreichbar, auch nicht versehentlich.
- Zugriff haben nur Anfragen direkt vom Mac oder solche mit dem Tailscale-Login aus `VT_ALLOWED_USERS`.
  Tailscale setzt diesen Header selbst und entfernt gefälschte.
- Die Seiten werden nicht im Browser-Cache gespeichert (`Cache-Control: no-store`). Es gibt keine
  Offline-Kopie auf dem Handy, keine Skripte von Drittanbietern (CSP `'self'`) und keine öffentliche API-Doku.
- Tailscale sieht Metadaten (welche Geräte, IP-Adressen), aber keine Inhalte. Wer auch das nicht will:
  eigener Koordinationsserver mit Headscale.

## Beenden
Vordergrund: `Strg+C`. Dienst: `./autostart.sh uninstall` (entfernt auch die Freigabe im Tailnet).

## Sicherung von Hand
```bash
python -m app.backup
```
Bitte die `.db`-Datei **nicht** in iCloud Drive oder Dropbox legen. Sync-Dienste können SQLite-Dateien
während eines Schreibvorgangs kopieren und dabei beschädigen.
