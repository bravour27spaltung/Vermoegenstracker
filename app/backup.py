"""Konsistente Sicherung der SQLite-Datenbank, auch während die App läuft.

    python -m app.backup            → data/backups/tracker-JJJJ-MM-TT_HHMM.db
    python -m app.backup --keep 30  → ältere Sicherungen darüber hinaus löschen

Nutzt die Backup-API von SQLite statt einer Dateikopie: Ein Kopieren der .db-Datei während
einer Schreibtransaktion (oder ohne die -wal-Datei) kann eine beschädigte Kopie erzeugen
(sqlite.org, „How To Corrupt An SQLite Database File“). Die Sicherungen liegen unter data/
und damit wie die Datenbank außerhalb von Git (.gitignore) und – mit FileVault – verschlüsselt.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime
from pathlib import Path

DB = Path(os.environ.get("VT_DB_PATH", "data/tracker.db"))
TARGET = DB.parent / "backups"


def backup(db: Path = DB, target_dir: Path = TARGET, keep: int | None = None) -> Path:
    if not db.exists():
        raise FileNotFoundError(f"Datenbank nicht gefunden: {db}")
    target_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(target_dir, 0o700)
    out = target_dir / f"tracker-{datetime.now():%Y-%m-%d_%H%M%S}.db"
    # Nicht read-only öffnen: Eine WAL-Datenbank braucht ihre -shm-Datei; fehlt sie (App gerade
    # beendet), kann ein read-only-Zugriff sie nicht anlegen („unable to open database file“).
    # Die Backup-API liest nur, sie verändert die Quelle nicht.
    src = sqlite3.connect(db, timeout=10)
    dst = sqlite3.connect(out)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")   # Sicherung als eine einzelne Datei, ohne -wal/-shm
    finally:
        dst.close()
        src.close()
    os.chmod(out, 0o600)
    check = sqlite3.connect(out)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"Sicherung fehlerhaft: {out}")
    finally:
        check.close()
    if keep:
        for old in sorted(target_dir.glob("tracker-*.db"))[:-keep]:
            old.unlink()
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Datenbank sichern")
    parser.add_argument("--keep", type=int, default=None, help="nur die neuesten N Sicherungen behalten")
    args = parser.parse_args()
    print(f"Sicherung: {backup(keep=args.keep)}")
