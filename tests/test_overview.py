"""Gesamtübersicht (Positionen + Depots live) und Jahresupdate."""
import os
import tempfile
from datetime import date

import pytest
from sqlalchemy.orm import Session

from app.db import init_db, make_engine
from app.models import (
    AssetClass, Depot, DepotPositionLink, DepotTransaction, Position, PositionKind, Security,
    SecurityPrice, Snapshot, TransactionKind,
)
from app.overview import build_overview, due_year_end, year_steps

TODAY = date(2026, 9, 24)


@pytest.fixture()
def session():
    engine = make_engine("sqlite://")
    init_db(engine)
    with Session(engine) as s:
        tg = Position(name="Tagesgeld", kind=PositionKind.ASSET, asset_class=AssetClass.LIQUIDITY, is_liquid=True)
        loan = Position(name="Kredit", kind=PositionKind.LIABILITY)
        old = Position(name="Test-Alt", kind=PositionKind.ASSET, asset_class=AssetClass.OTHER, is_active=False)
        s.add_all([tg, loan, old])
        s.flush()
        s.add_all([
            Snapshot(snapshot_date=date(2025, 12, 31), position_id=tg.id, value_cent=1_000_000),
            Snapshot(snapshot_date=date(2026, 9, 1), position_id=tg.id, value_cent=1_200_000),
            Snapshot(snapshot_date=date(2025, 12, 31), position_id=loan.id, value_cent=500_000),
            Snapshot(snapshot_date=date(2025, 12, 31), position_id=old.id, value_cent=9_999_900),
        ])
        d = Depot(name="TR")
        sec = Security(isin="IE00B4L5Y983", name="World", asset_class=AssetClass.EQUITIES)
        s.add_all([d, sec])
        s.flush()
        s.add(DepotTransaction(depot_id=d.id, security_id=sec.id, trade_date=date(2024, 3, 1),
                               kind=TransactionKind.BUY, quantity=100, amount_cent=800_000))
        s.add(SecurityPrice(security_id=sec.id, price_date=date(2025, 12, 31), price_cent=9_000))
        s.add(SecurityPrice(security_id=sec.id, price_date=date(2026, 9, 1), price_cent=10_000))
        s.commit()
        yield s


def test_overview_combines_positions_and_live_depots(session):
    ov = build_overview(session, TODAY)
    assert ov.assets_cent == 1_200_000 + 1_000_000          # Tagesgeld + Depot 100 × 100 €
    assert ov.liabilities_cent == 500_000
    assert ov.net_cent == 1_700_000
    assert ov.reference_net_cent == 1_000_000 - 500_000 + 900_000
    assert ov.depot_cent == 1_000_000 and ov.liquid_cent == 1_200_000
    # Verlauf: 31.12.2024 (nur Depot, Kaufkurs), 31.12.2025, 01.09.2026, heute; ausgeblendete Position zählt nie
    assert [d for d, _ in ov.series] == [date(2024, 12, 31), date(2025, 12, 31), date(2026, 9, 1), TODAY]
    assert dict(ov.series)[date(2024, 12, 31)] == 800_000
    assert dict(ov.series)[date(2025, 12, 31)] == 1_400_000
    depot_row = next(r for r in ov.rows if r.source == "depot")
    assert depot_row.change_cent == 100_000


def test_linked_position_is_replaced_by_depot(session):
    p = Position(name="Depot TR (alt)", kind=PositionKind.ASSET, asset_class=AssetClass.EQUITIES)
    session.add(p)
    session.flush()
    session.add(Snapshot(snapshot_date=date(2025, 12, 31), position_id=p.id, value_cent=5_000_000))
    session.add(DepotPositionLink(depot_id=1, asset_class=AssetClass.EQUITIES, position_id=p.id))
    session.commit()
    assert build_overview(session, TODAY).assets_cent == 2_200_000


def test_due_year_end_and_steps(session):
    # Tagesgeld und Kredit haben einen Wert zum 31.12.2025 → Vorjahr erledigt
    assert due_year_end(session, TODAY) == date(2026, 12, 31)
    steps = {s.key: s for s in year_steps(session, date(2025, 12, 31))}
    assert steps["prices"].done and steps["positions"].done
    assert not steps["pension"].done
    steps = {s.key: s for s in year_steps(session, date(2024, 12, 31))}
    assert not steps["prices"].done and "World" in steps["prices"].detail


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.db import make_session_factory
    from app.deps import get_session
    from app.main import app

    engine = make_engine(f"sqlite:///{os.path.join(tempfile.mkdtemp(), 't.db')}")
    init_db(engine)
    factory = make_session_factory(engine)

    def override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_pages(client):
    assert "Los geht" in client.get("/dashboard").text
    client.post("/positions", data={"name": "Girokonto", "preset": "girokonto", "is_liquid": "true"})
    last = date.today().year - 1
    r = client.get("/snapshots/new")
    assert f"Werte zum 31.12.{last}" in r.text
    client.post("/snapshots", data={"snapshot_date": f"{last}-12-31", "value_1": "5.000,00", "contribution_1": "0"})
    r = client.get("/jahresupdate")
    assert r.status_code == 200 and f"31.12.{date.today().year}" in r.text   # Vorjahr erledigt → nächstes Jahr
    r = client.get(f"/jahresupdate?jahr={last}")
    assert "Konten, Immobilien und Kredite" in r.text
    client.post("/jahresupdate/ausgaben", data={"snapshot_date": f"{last}-12-31", "expenses": "2.000"})
    r = client.get("/dashboard")
    assert r.status_code == 200 and "5.000 €" in r.text and "2,5 Monate" in r.text
