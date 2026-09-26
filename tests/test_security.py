"""Zugriffsschutz, Sicherheits-Header, PWA-Dateien und Sicherung."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.security import access_decision


def test_access_rules():
    me = {"steffen@example.com"}
    assert access_decision("127.0.0.1", {}, set())[0]                                   # lokal am Mac
    assert not access_decision("192.168.1.20", {}, me)[0]                                 # WLAN
    assert not access_decision("127.0.0.1", {"x-forwarded-for": "100.64.0.2"}, me)[0]   # Proxy ohne Identität
    assert access_decision("127.0.0.1", {"tailscale-user-login": "Steffen@example.com"}, me)[0]
    assert not access_decision("127.0.0.1", {"tailscale-user-login": "gast@example.com"}, me)[0]
    assert not access_decision("127.0.0.1", {"tailscale-user-login": "steffen@example.com"}, set())[0]


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """Eigene Test-Datenbank – die echte data/tracker.db wird nie berührt."""
    from app.db import init_db, make_engine, make_session_factory
    from app.deps import get_session
    from app.main import app

    engine = make_engine(f"sqlite:///{tmp_path / 't.db'}")
    init_db(engine)
    factory = make_session_factory(engine)

    def override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setenv("VT_ALLOWED_USERS", "steffen@example.com")
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_headers_and_denial(client):
    r = client.get("/static/manifest.webmanifest")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    assert "Cache-Control" not in r.headers or "no-store" not in r.headers["Cache-Control"]
    r = client.get("/static/apple-touch-icon.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    r = client.get("/fire", headers={"Tailscale-User-Login": "gast@example.com"})
    assert r.status_code == 403 and "nicht freigegeben" in r.text
    assert r.headers["X-Frame-Options"] == "DENY"
    assert client.get("/docs").status_code == 404                                         # keine API-Doku


def test_pages_have_pwa_tags_and_no_cdn(client):
    r = client.get("/depots/assumptions", headers={"Tailscale-User-Login": "steffen@example.com"})
    assert r.status_code == 200
    assert 'rel="manifest"' in r.text and 'apple-touch-icon' in r.text
    assert r.headers["Cache-Control"] == "no-store"
    assert "script-src 'self' 'unsafe-inline'" in r.headers["Content-Security-Policy"]
    assert "cdnjs" not in r.text and "unpkg" not in r.text


def test_dashboard_without_cdn():
    import pathlib
    html = pathlib.Path("app/templates/dashboard.html").read_text()
    assert "cdnjs" not in html and "chart_svg" in html


def test_backup(tmp_path):
    from app.backup import backup
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("create table x(a)")
    con.execute("insert into x values (42)")
    con.commit()                                   # Verbindung bleibt offen, wie bei laufender App
    for _ in range(3):
        out = backup(db, tmp_path / "b", keep=2)
    assert sqlite3.connect(out).execute("select a from x").fetchone() == (42,)
    assert len(list((tmp_path / "b").glob("tracker-*.db"))) <= 2
    con.close()
