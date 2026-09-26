"""Tests für Depot-Berechnungen (app/depot.py) und die Depot-Seiten (app/routes/depots.py)."""
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import date

import pytest

from app import depot as calc
from app.models import AssetClass, TransactionKind


# --------------------------------------------------------------------------- Steuer


def test_capital_gains_tax_rate():
    assert calc.capital_gains_tax_rate(0) == pytest.approx(0.26375)
    assert calc.capital_gains_tax_rate(0.08) == pytest.approx(0.2782, abs=1e-4)
    assert calc.capital_gains_tax_rate(0.09) == pytest.approx(0.2799, abs=1e-4)


def test_advance_lump_sum():
    # 100 Stück à 100 €, Basiszins 3,2 %: 10.000 € × 3,2 % × 70 % = 224 €
    assert calc.advance_lump_sum_cent(100, 10000, 11000, 0, 0.032) == 22400
    # Deckel auf den Wertzuwachs (100 €)
    assert calc.advance_lump_sum_cent(100, 10000, 10100, 0, 0.032) == 10000
    assert calc.advance_lump_sum_cent(100, 10000, 9000, 0, 0.032) == 0
    # Ausschüttung 50 € wird abgezogen
    assert calc.advance_lump_sum_cent(100, 10000, 11000, 5000, 0.032) == 17400
    # Kauf im April: 9/12
    assert calc.advance_lump_sum_cent(100, 10000, 11000, 0, 0.032, purchase_month=4) == 16800


# --------------------------------------------------------------------------- Bestand


@dataclass
class Tx:
    id: int
    trade_date: date
    kind: TransactionKind
    quantity: float = 0.0
    amount_cent: int = 0
    fee_cent: int = 0
    security_id: int = 1
    depot_id: int = 1


def test_fifo_and_realized():
    txs = [
        Tx(1, date(2024, 1, 1), TransactionKind.BUY, 10, 100000, 1000),   # 10 × 100 € + 10 €
        Tx(2, date(2024, 6, 1), TransactionKind.BUY, 10, 200000),         # 10 × 200 €
        Tx(3, date(2025, 1, 1), TransactionKind.SELL, 15, 450000, 500),   # 15 × 300 € − 5 €
        Tx(4, date(2025, 2, 1), TransactionKind.DIVIDEND, 0, 1200),
    ]
    h = calc.holdings(txs)[1]
    assert h.quantity == pytest.approx(5)
    assert h.cost_cent == 100000                 # 5 Rest à 200 €
    assert h.realized_cent == 449500 - (101000 + 100000)
    assert h.distributions_cent == 1200


def test_net_contribution_window():
    txs = [
        Tx(1, date(2026, 1, 10), TransactionKind.BUY, 1, 50000, 100),
        Tx(2, date(2026, 2, 10), TransactionKind.SELL, 1, 20000, 100),
        Tx(3, date(2026, 3, 10), TransactionKind.DIVIDEND, 0, 3000),
        Tx(4, date(2026, 4, 10), TransactionKind.BUY, 1, 99999),
    ]
    # Kauf +50.100, Verkauf −19.900, Ausschüttung −3.000; der April-Kauf liegt außerhalb
    assert calc.net_contribution_cent(txs, date(2025, 12, 31), date(2026, 3, 31)) == 50100 - 19900 - 3000


# --------------------------------------------------------------------------- Rendite


def test_xirr_simple_and_reference():
    assert calc.xirr([(date(2024, 1, 1), -1000), (date(2024, 12, 31), 1100)]) == pytest.approx(0.1, abs=5e-4)
    # Referenzwert aus der Excel-Dokumentation zu XINTZINSFUSS: 37,34 %
    flows = [(date(2008, 1, 1), -10000), (date(2008, 3, 1), 2750), (date(2008, 10, 30), 4250),
             (date(2009, 2, 15), 3250), (date(2009, 4, 1), 2750)]
    assert calc.xirr(flows) == pytest.approx(0.373363, abs=1e-4)
    assert calc.xirr([(date(2024, 1, 1), -1000)]) is None


# --------------------------------------------------------------------------- Prognose


def test_forecast_without_volatility_is_deterministic():
    fc = calc.forecast(10000, 0, math.log(1.05), 0.0, 10, paths=20)
    assert fc.p10[-1] == pytest.approx(fc.p90[-1])
    assert fc.p50[-1] == pytest.approx(10000 * 1.05 ** 10)
    assert fc.contributions[-1] == 10000


def test_portfolio_parameters():
    a = {AssetClass.EQUITIES: (0.05, 0.17), AssetClass.BONDS: (0.017, 0.07)}
    mu, s = calc.portfolio_parameters({AssetClass.EQUITIES: 1}, a)
    assert mu == pytest.approx(math.log(1.05)) and s == pytest.approx(0.17)
    mu_ter, _ = calc.portfolio_parameters({AssetClass.EQUITIES: 1}, a, ter=0.002)
    assert mu_ter == pytest.approx(math.log(1.05) - math.log(1.002))
    mu, s = calc.portfolio_parameters({AssetClass.EQUITIES: 0.6, AssetClass.BONDS: 0.4}, a)
    assert s < 0.6 * 0.17 + 0.4 * 0.07                                  # Diversifikation senkt Risiko
    assert mu > 0.6 * math.log(1.05) + 0.4 * math.log(1.017)            # Diversifikationsrendite


# --------------------------------------------------------------------------- Web


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    from app.db import init_db, make_engine, make_session_factory
    from app.deps import get_session
    from app.main import app

    path = os.path.join(tempfile.mkdtemp(), "t.db")
    engine = make_engine(f"sqlite:///{path}")
    init_db(engine)
    factory = make_session_factory(engine)

    def override():
        s: Session = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_empty_overview(client):
    r = client.get("/depots")
    assert r.status_code == 200 and "Noch kein Depot" in r.text


def test_depot_workflow_and_sync(client):
    r = client.post("/depots", data={"name": "Neobroker", "monthly_savings": "500"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/depots/1"
    r = client.post("/depots/1/transactions", data={
        "trade_date": "2025-01-10", "kind": "BUY", "security_id": "new", "isin": "ie00b4l5y983",
        "security_name": "MSCI World", "asset_class": "EQUITIES", "region": "Welt", "ter": "0,2",
        "partial_exemption": "0.3", "is_fund": "1", "quantity": "10", "price": "100", "fee": "1"})
    assert "gebucht" in r.text
    # Verkauf über Bestand wird abgelehnt
    r = client.post("/depots/1/transactions", data={
        "trade_date": "2025-02-01", "kind": "SELL", "security_id": "1", "quantity": "11", "price": "100"})
    assert "nicht gespeichert" in r.text
    client.post("/depots/prices", data={"price_date": "2025-12-31", "price_1": "110"})
    client.post("/depots/prices", data={"price_date": "2026-03-31", "price_1": "121,50"})

    j = client.get("/depots/api/summary.json").json()
    assert j["value_cent"] == 121500
    assert j["net_invested_cent"] == 100100
    assert j["monthly_savings_cent"] == 50000
    assert j["by_class"] == {"EQUITIES": 121500}

    for url in ("/depots", "/depots/1", "/depots/securities", "/depots/prices", "/depots/assumptions",
                "/depots/import", "/depots/sync", "/depots/forecast?years=5&nominal=1", "/depots/forecast?depot=1"):
        assert client.get(url).status_code == 200, url

    # Stichtag 31.12.2025 übernehmen → neue Position, erster Stichtag ohne Einzahlung
    client.post("/depots/sync", data={"snapshot_date": "2025-12-31", "target_1_EQUITIES": "new"})
    # Nachkauf im Q1, dann 31.03.2026 übernehmen → Netto-Einzahlung seit 31.12.
    client.post("/depots/1/transactions", data={
        "trade_date": "2026-02-01", "kind": "BUY", "security_id": "1", "quantity": "1", "price": "115"})
    r = client.get("/depots/sync?snapshot_date=2026-03-31")
    assert "Neobroker – Aktien" in r.text
    client.post("/depots/sync", data={"snapshot_date": "2026-03-31", "target_1_EQUITIES": "1"})

    from sqlalchemy import select
    from app.deps import get_session
    from app.main import app
    from app.models import Position, Snapshot
    session = next(app.dependency_overrides[get_session]())
    pos = session.execute(select(Position)).scalar_one()
    assert pos.asset_class == AssetClass.EQUITIES and pos.name == "Neobroker – Aktien"
    snaps = session.execute(select(Snapshot).order_by(Snapshot.snapshot_date)).scalars().all()
    assert [(s.value_cent, s.net_contribution_cent) for s in snaps] == [(110000, 0), (11 * 12150, 11500)]
    # Dashboard (bestehende Seite) sieht die übernommenen Werte
    assert "1.336,50 €" in client.get("/dashboard").text


def test_csv_import_and_export(client):
    csv_text = ("datum;depot;isin;name;typ;stueck;kurs;gebuehren;betrag;notiz\n"
                "2025-01-02;Import;DE000A0S9GB0;Gold;kauf;10;50,50;1;;\n"
                "2025-13-01;Import;DE000A0S9GB0;Gold;kauf;1;1;0;;\n"
                "2025-06-01;Import;DE000A0S9GB0;Gold;dividende;;;;12,34;\n")
    r = client.post("/depots/import", files={"file": ("t.csv", csv_text.encode(), "text/csv")})
    assert "2 Zeile(n) importiert" in r.text and "Zeile 3" in r.text
    client.post("/depots/import", files={"file": ("k.csv", b"datum;isin;kurs\n2026-01-05;DE000A0S9GB0;60\n", "text/csv")})
    assert client.get("/depots/api/summary.json").json()["value_cent"] == 60000
    export = client.get("/depots/export/transactions.csv").text
    assert "DE000A0S9GB0" in export and "dividende" in export
    # Export lässt sich wieder importieren (Rundlauf)
    r = client.post("/depots/import", files={"file": ("e.csv", export.encode("utf-8"), "text/csv")})
    assert "2 Zeile(n) importiert" in r.text


# --------------------------------------------------------------------------- Trade Republic

TR_HEADER = ('"datetime","date","account_type","category","type","asset_class","name","symbol","shares","price",'
             '"amount","fee","tax","currency","original_amount","original_currency","fx_rate","description",'
             '"transaction_id","counterparty_name","counterparty_iban","payment_reference","mcc_code"')


def _tr_row(d, cat, typ, cls="", name="", sym="", shares="", price="", amount="", fee="", tax="", tid="x"):
    vals = [f"{d}T10:00:00Z", d, "DEFAULT", cat, typ, cls, name, sym, shares, price, amount, fee, tax, "EUR",
            "", "", "", "", tid, "", "", "", ""]
    return ",".join(f'"{v}"' for v in vals)


TR_SAMPLE = "\n".join([
    TR_HEADER,
    _tr_row("2023-03-06", "CASH", "CUSTOMER_INBOUND", amount="1000.000000", tid="t1"),
    _tr_row("2023-03-10", "TRADING", "BUY", "FUND", "MSCI ACWI USD (Acc)", "IE00B6R52259", "10.0000000000",
            "60.000000", "-600.00", "-1.00", tid="t2"),
    _tr_row("2023-03-23", "DELIVERY", "FREE_RECEIPT", "FUND", "MSCI ACWI USD (Acc)", "IE00B6R52259", "5.0000000000",
            tid="t3"),
    _tr_row("2023-04-01", "CASH", "INTEREST_PAYMENT", amount="2.500000", tax="0.00", tid="t4"),
    _tr_row("2023-05-31", "CASH", "DISTRIBUTION", "FUND", "Developed Markets Property Yield USD (Dist)",
            "IE00B1FZS350", "100.0", "", "10.000000", "", "-0.50", tid="t5"),
    _tr_row("2023-06-01", "TRADING", "BUY", "FUND", "Developed Markets Property Yield USD (Dist)", "IE00B1FZS350",
            "20.0000000000", "20.000000", "-400.00", tid="t6"),
    _tr_row("2023-12-18", "TRADING", "SELL", "FUND", "MSCI ACWI USD (Acc)", "IE00B6R52259", "-3.0000000000",
            "70.000000", "210.00", "-1.00", "-2.10", tid="t7"),
    _tr_row("2024-01-02", "TRADING", "BUY", "CRYPTO", "Bitcoin", "BTC", "0.0010000000", "40000.000000", "-40.00",
            tid="t8"),
    _tr_row("2024-01-03", "CASH", "CARD_TRANSACTION", amount="-5.000000", tid="t9"),
]) + "\n"


def test_parse_trade_republic():
    from app.broker_import import parse_trade_republic

    p = parse_trade_republic(TR_SAMPLE)
    kinds = [(t.external_id, t.kind.value, t.isin, t.quantity, t.amount_cent, t.fee_cent, t.tax_cent) for t in p.transactions]
    assert ("t2", "BUY", "IE00B6R52259", 10.0, 60000, 100, 0) in kinds
    assert ("t7", "SELL", "IE00B6R52259", 3.0, 21000, 100, 210) in kinds
    assert ("t5", "DIVIDEND", "IE00B1FZS350", 0.0, 1000, 0, 50) in kinds          # brutto, Steuer separat
    delivery = next(t for t in p.transactions if t.external_id == "t3")
    assert delivery.estimated and delivery.amount_cent == 5 * 6000             # nächster Handelskurs 60 €
    assert p.securities["IE00B1FZS350"].asset_class == AssetClass.REAL_ESTATE
    assert p.securities["BTC"].asset_class == AssetClass.CRYPTO and not p.securities["BTC"].is_fund
    assert p.ignored == {"CASH/CUSTOMER_INBOUND": 1, "CASH/INTEREST_PAYMENT": 1, "CASH/CARD_TRANSACTION": 1}
    # Verrechnungskonto: 1000 − 601 + 2,50 + 10 − 0,50 − 400 + 210 − 1 − 2,10 − 40 − 5
    assert p.cash_balance_cent == 17290


def test_trade_republic_import_is_idempotent(client):
    files = {"file": ("tr.csv", TR_SAMPLE.encode(), "text/csv")}
    r = client.post("/depots/import", data={"depot_id": "new", "new_depot_name": "Trade Republic"}, files=files)
    assert "6 Buchung(en) übernommen" in r.text and "Einlieferung" in r.text
    r = client.post("/depots/import", data={"depot_id": "1"}, files=files)
    assert "0 Buchung(en) übernommen, 6 bereits vorhanden" in r.text
    j = client.get("/depots/api/summary.json").json()
    assert j["depots"][0]["name"] == "Trade Republic"
    assert any(h["title"] == "Einstand fehlt" for h in j["hints"])
    # Einstand der Einlieferung korrigieren → Hinweis verschwindet
    from sqlalchemy import select
    from app.deps import get_session
    from app.main import app
    from app.models import DepotTransaction
    session = next(app.dependency_overrides[get_session]())
    tid = session.execute(select(DepotTransaction.id).where(DepotTransaction.external_id == "t3")).scalar_one()
    client.post(f"/depots/transactions/{tid}/edit", data={"trade_date": "2023-03-23", "quantity": "5",
                                                          "amount": "250,00", "fee": "0", "note": session.get(DepotTransaction, tid).note})
    j = client.get("/depots/api/summary.json").json()
    assert not any(h["title"] == "Einstand fehlt" for h in j["hints"])
    assert client.get("/depots/1").status_code == 200
