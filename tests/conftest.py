"""Tests laufen nie gegen die echte Datenbank: app.deps legt beim Import eine Engine an,
deshalb wird DATABASE_URL vor jedem Import auf eine temporäre Datei gesetzt."""
import os
import tempfile

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp(prefix='vt-test-')}/test.db"
