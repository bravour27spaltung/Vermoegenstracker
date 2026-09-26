"""Tests für Entnahme im Ruhestand und FIRE (app/retirement.py, /pensions, /fire)."""
import math
import os
import tempfile
from datetime import date

import pytest

from app.retirement import (
    FireInputs,
    TaxParams,
    drawdown,
    earliest_fire_age,
    fire_curve,
    gross_withdrawal,
    pension_share_at,
    tax_on_withdrawal,
)

TAX = TaxParams(rate=0.26375, exemption=0.3, allowance_eur=1000)


def test_gross_withdrawal_is_inverse_of_tax():
    for net, g in ((50000, 0.6), (20000, 0.2), (800, 0.9), (30000, 0.0)):
        w = gross_withdrawal(net, g, TAX)
        assert w - tax_on_withdrawal(w, g, TAX) == pytest.approx(net, abs=0.01)
    # Unter dem Pauschbetrag keine Steuer
    assert gross_withdrawal(1000, 1.0, TAX) == 1000


def test_drawdown_matches_annuity():
    # Ohne Zins und ohne Gewinn: 300.000 € über 25 Jahre = 1.000 €/Monat, steuerfrei
    dd = drawdown(300000, 300000, 0.0, 25, TAX)
    assert dd.gross_monthly_eur == pytest.approx(1000)
    assert dd.tax_monthly_eur == 0
    # Mit Gewinnanteil fällt Steuer an, netto < brutto
    dd = drawdown(300000, 100000, 0.0, 25, TAX)
    assert dd.tax_monthly_eur == pytest.approx(((12000 * 2 / 3 * 0.7) - 1000) * 0.26375 / 12)


def test_pension_share():
    # Hälfte bereits erreicht, Ausstieg nach der Hälfte der Restzeit → 75 %
    assert pension_share_at(47, 27, 67, 0.5) == pytest.approx(0.75)
    assert pension_share_at(67, 30, 67, None) == 1.0
    # Linear ab 22: mit 22 ausgestiegen → heutiger Stand
    assert pension_share_at(30, 30, 67, None, 22) == pytest.approx(8 / 45)


def test_fire_success_increases_with_age_and_savings():
    inp = FireInputs(current_age=30, retirement_age=67, horizon_age=95, start_eur=100000, basis_eur=80000,
                     savings_yearly_eur=20000, mu=math.log(1.05), sigma=0.15, tax=TAX)
    spend = lambda fa: (lambda age: 30000.0 if age < 67 else 10000.0)
    curve = fire_curve(inp, spend, paths=300)
    assert curve.success[0] < 0.05 and curve.success[-1] > 0.9
    assert all(b >= a - 0.05 for a, b in zip(curve.success, curve.success[1:]))   # im Wesentlichen monoton
    base = earliest_fire_age(inp, spend, 0.9, paths=300)
    richer = earliest_fire_age(FireInputs(**{**inp.__dict__, "savings_yearly_eur": 40000}), spend, 0.9, paths=300)
    assert richer is not None and base is not None and richer < base
    # Ohne Risiko und ohne Bedarf: sofort
    safe = FireInputs(**{**inp.__dict__, "sigma": 0.0})
    assert earliest_fire_age(safe, lambda fa: (lambda age: 0.0), 0.9, paths=10) == 31


# --------------------------------------------------------------------------- Web


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.db import init_db, make_engine, make_session_factory
    from app.deps import get_session
    from app.main import app
    from app.models import PensionContract, PensionSettings, PensionSnapshot, PensionType, Profile

    engine = make_engine(f"sqlite:///{os.path.join(tempfile.mkdtemp(), 't.db')}")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        s.add(Profile(id=1, birth_date=date(1997, 8, 1), retirement_age=67, desired_income_monthly_cent=300000,
                      planned_savings_monthly_cent=100000, withdrawal_rate=0.035))
        s.add(PensionSettings(id=1, pkv_today_total_cent=80000, pkv_today_sick_pay_cent=4000, pkv_today_care_cent=6000))
        c = PensionContract(name="DRV", pension_type=PensionType.STATUTORY)
        s.add(c)
        s.flush()
        s.add(PensionSnapshot(contract_id=c.id, as_of=date(2026, 9, 1), projected_monthly_cent=150000,
                              accrued_monthly_cent=30000))
        s.commit()

    def override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def _depot_with_buy(client, savings="1000"):
    client.post("/depots", data={"name": "TR", "monthly_savings": savings})
    client.post("/depots/1/transactions", data={
        "trade_date": "2024-01-02", "kind": "BUY", "security_id": "new", "isin": "IE00B4L5Y983",
        "security_name": "MSCI World", "asset_class": "EQUITIES", "region": "Welt", "ter": "0,2",
        "partial_exemption": "0.3", "is_fund": "1", "quantity": "1000", "price": "80"})
    client.post("/depots/prices", data={"price_date": date.today().isoformat(), "price_1": "100"})


def test_pensions_show_drawdown(client):
    _depot_with_buy(client)
    r = client.get("/pensions")
    assert r.status_code == 200
    assert "Entnahme aus Depots und Vermögen" in r.text and "Verbleibende Lücke" in r.text
    assert "Depots 100.000,00 €" in r.text


def test_depot_linked_position_not_double_counted(client):
    _depot_with_buy(client)
    client.post("/depots/sync", data={"snapshot_date": date.today().isoformat(), "target_1_EQUITIES": "new"})
    r = client.get("/pensions")
    assert "sind durch die aktuellen Depotwerte ersetzt" in r.text
    assert "übrige ruhestandsrelevante Positionen 0,00 €" in r.text


def test_fire_page_and_settings(client):
    _depot_with_buy(client)
    r = client.get("/fire")
    assert r.status_code == 200 and "Frühestes FIRE-Alter" in r.text and "Coast FIRE" in r.text
    # PKV-Standard: 800 − 40 + 60 = 820 €/Monat
    assert "820 €/Monat" in r.text
    r = client.post("/fire/settings", data={"spending": "2.500", "health": "", "withdrawal_rate": "3,25",
                                            "target_success": "85", "horizon_age": "100", "career_start_age": "22"})
    r = client.get("/fire")
    assert "2.500 €/Monat" in r.text and "3,25 %" in r.text and "bis 100" in r.text
    assert "Fehler" in client.post("/fire/settings", data={"target_success": "150", "withdrawal_rate": "3"}).text
