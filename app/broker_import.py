"""Einlesen von Broker-Exporten (derzeit Trade Republic) – ohne Datenbankbezug, testbar.

Trade Republic: App → Profil → Kontoauszüge → Transaktionsexport (CSV, Komma, Punkt als
Dezimaltrenner). Relevante Zeilen und ihre Bedeutung (geprüft an einem echten Export):

- TRADING/BUY:   shares > 0, price, amount = −Kurswert, fee = −Gebühr (Sparplan: leer)
- TRADING/SELL:  shares < 0, amount = +Kurswert, fee = −Gebühr, tax = −einbehaltene Steuer
- CASH/DIVIDEND, CASH/DISTRIBUTION: amount = Bruttobetrag, tax = −einbehaltene Steuer
- DELIVERY/FREE_RECEIPT: Einlieferung (Depotübertrag) nur mit Stückzahl, ohne Einstand
- CASH/*: alle übrigen Zeilen betreffen das Verrechnungskonto (Überweisungen, Karte, Zinsen,
  Saveback, Stockperk). Sie ändern den Depotbestand nicht; Saveback und Stockperk werden als
  eigener Kauf (TRADING/BUY) gebucht und darüber erfasst.

Der Saldo des Verrechnungskontos ergibt sich als Σ(amount + fee + tax) über alle Zeilen,
sofern der Export ab Kontoeröffnung reicht.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.models import AssetClass, TransactionKind
from app.money import to_cents

TR_REQUIRED = {"date", "category", "type", "asset_class", "name", "symbol", "shares", "price",
               "amount", "fee", "tax", "transaction_id"}


@dataclass
class SecurityInfo:
    isin: str
    name: str
    asset_class: AssetClass
    region: str
    is_fund: bool
    partial_exemption: float


@dataclass
class ParsedTransaction:
    external_id: str
    trade_date: date
    kind: TransactionKind
    isin: str
    quantity: float
    amount_cent: int
    fee_cent: int = 0
    tax_cent: int = 0
    note: str | None = None
    estimated: bool = False     # Einstand geschätzt (Einlieferung ohne Kaufkurs)


@dataclass
class ParsedImport:
    broker: str
    transactions: list[ParsedTransaction] = field(default_factory=list)
    securities: dict[str, SecurityInfo] = field(default_factory=dict)
    cash_balance_cent: int | None = None
    first_date: date | None = None
    last_date: date | None = None
    ignored: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _cent(text: str) -> int:
    return to_cents(Decimal(text)) if text and text.strip() else 0


def _qty(text: str) -> float:
    return abs(float(text)) if text and text.strip() else 0.0


def is_trade_republic(header: list[str]) -> bool:
    return TR_REQUIRED <= {h.strip().lower() for h in header}


def guess_security(isin: str, name: str, tr_class: str) -> SecurityInfo:
    """Stammdaten aus Name und TR-Klasse ableiten. Nur ein Vorschlag – auf der Seite
    „Wertpapiere“ prüfen (Teilfreistellung hängt von den Anlagebedingungen des Fonds ab)."""
    n = name.lower()
    if tr_class == "CRYPTO":
        return SecurityInfo(isin, name, AssetClass.CRYPTO, "Welt", False, 0.0)
    if tr_class == "STOCK":
        country = {"DE": "Deutschland", "US": "USA", "FR": "Europa", "NL": "Europa", "IE": "Europa",
                   "CH": "Europa", "GB": "Europa", "JP": "Japan"}.get(isin[:2], "Welt")
        return SecurityInfo(isin, name, AssetClass.EQUITIES, country, False, 0.0)
    if any(w in n for w in ("gov bond", "govt bond", "treasury", "aggregate", "corporate bond", "bond")):
        cls, tfs = AssetClass.BONDS, 0.0
    elif any(w in n for w in ("commodity", "gold", "rohstoff")):
        cls, tfs = AssetClass.COMMODITIES, 0.0
    elif any(w in n for w in ("property", "real estate", "reit")):
        # REIT-ETFs investieren in Aktien → in der Regel Aktienfonds (30 %), kein Immobilienfonds.
        cls, tfs = AssetClass.REAL_ESTATE, 0.3
    elif any(w in n for w in ("money market", "overnight", "geldmarkt", "€str", "ester")):
        cls, tfs = AssetClass.LIQUIDITY, 0.0
    else:
        cls, tfs = AssetClass.EQUITIES, 0.3
    region = "Welt"
    for keys, reg in ((("emerging", " em "), "Schwellenländer"), (("pacific",), "Asien-Pazifik"),
                      (("nikkei", "japan"), "Japan"), (("europe", "euro ", "stoxx", "dax"), "Europa"),
                      (("s&p 500", "usa", "nasdaq"), "USA"), (("developed", "world"), "Industrieländer"),
                      (("acwi", "all-world", "all world"), "Welt")):
        if any(k in f" {n} " for k in keys):
            region = reg
            break
    return SecurityInfo(isin, name, cls, region, True, tfs)


def parse_trade_republic(text: str) -> ParsedImport:
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    out = ParsedImport(broker="Trade Republic")
    balance = 0
    trade_prices: dict[str, list[tuple[date, int]]] = {}
    deliveries = []
    for r in rows:
        r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
        try:
            d = date.fromisoformat(r["date"])
            balance += _cent(r["amount"]) + _cent(r["fee"]) + _cent(r["tax"])
        except (ValueError, ArithmeticError) as exc:
            out.problems.append(f"Zeile {r.get('transaction_id') or '?'} nicht lesbar: {exc}")
            continue
        out.first_date = min(filter(None, [out.first_date, d]))
        out.last_date = max(filter(None, [out.last_date, d]))
        cat, typ, isin = r["category"], r["type"], r["symbol"].upper()
        if isin and isin not in out.securities and r["name"]:
            out.securities[isin] = guess_security(isin, r["name"], r["asset_class"])
        tid = r["transaction_id"]
        if cat == "TRADING" and typ in ("BUY", "SELL"):
            qty = _qty(r["shares"])
            amount = abs(_cent(r["amount"]))
            if qty <= 0:
                out.problems.append(f"{d}: {typ} {isin} ohne Stückzahl übersprungen")
                continue
            trade_prices.setdefault(isin, []).append((d, round(amount / qty)))
            out.transactions.append(ParsedTransaction(
                tid, d, TransactionKind.BUY if typ == "BUY" else TransactionKind.SELL, isin, qty, amount,
                fee_cent=abs(_cent(r["fee"])), tax_cent=max(-_cent(r["tax"]), 0)))
        elif cat == "CASH" and typ in ("DIVIDEND", "DISTRIBUTION") and isin:
            out.transactions.append(ParsedTransaction(
                tid, d, TransactionKind.DIVIDEND, isin, 0.0, abs(_cent(r["amount"])),
                tax_cent=max(-_cent(r["tax"]), 0)))
        elif cat == "DELIVERY" and typ == "FREE_RECEIPT":
            deliveries.append((tid, d, isin, _qty(r["shares"])))
        else:
            key = f"{cat}/{typ}"
            out.ignored[key] = out.ignored.get(key, 0) + 1
    # Einlieferungen: Einstand ist im Export nicht enthalten → Marktwert über den zeitlich
    # nächsten Handelskurs schätzen und markieren.
    for tid, d, isin, qty in deliveries:
        prices = trade_prices.get(isin, [])
        if prices:
            _, price = min(prices, key=lambda p: abs((p[0] - d).days))
            amount = round(price * qty)
            note = "Einlieferung – Einstand geschätzt (Marktkurs), bitte korrigieren"
        else:
            amount, note = 0, "Einlieferung – Einstand unbekannt, bitte eintragen"
        out.transactions.append(ParsedTransaction(tid, d, TransactionKind.BUY, isin, qty, amount,
                                                  note=note, estimated=True))
    out.transactions.sort(key=lambda t: (t.trade_date, t.kind != TransactionKind.BUY))
    out.cash_balance_cent = balance
    return out
