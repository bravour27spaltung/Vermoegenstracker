"""Rechenlogik für Depots – ohne Web- und Datenbankbezug, dadurch einzeln testbar.

Geld wird in ganzen Cent gerechnet (siehe app/money.py). Renditen, Quoten und die
Monte-Carlo-Simulation arbeiten mit float, weil dort keine Buchungsbeträge entstehen.

Inhalt
- Bestand nach FIFO (§ 20 Abs. 4 Satz 7 EStG)
- XIRR: geldgewichtete Rendite mit taggenauen Zahlungen
- Steuer: Abgeltungsteuer inkl. Soli/KiSt (§ 32d Abs. 1 EStG), Teilfreistellung (§ 20 InvStG),
  Vorabpauschale (§ 18 InvStG)
- Prognose: Monte-Carlo mit Sparrate, Portfolio-Parameter aus Allokation und Annahmen
- Hinweise: Klumpenrisiko, Allokationslücke, Kosten, veraltete Kurse

Die Funktionen nehmen Objekte mit den Attributen der ORM-Klassen entgegen (Duck-Typing),
also DepotTransaction/Security direkt oder einfache Dataclasses in Tests.
"""
from __future__ import annotations

import bisect
import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional, Protocol

from app.models import AssetClass, TransactionKind

ASSET_CLASS_LABELS: dict[AssetClass, str] = {
    AssetClass.EQUITIES: "Aktien",
    AssetClass.BONDS: "Anleihen",
    AssetClass.LIQUIDITY: "Geldmarkt/Cash",
    AssetClass.REAL_ESTATE: "Immobilien (REITs)",
    AssetClass.COMMODITIES: "Rohstoffe/Gold",
    AssetClass.CRYPTO: "Krypto",
    AssetClass.OTHER: "Sonstiges",
}
SECURITY_CLASSES = list(ASSET_CLASS_LABELS)

# Standardannahmen: reale geometrische Rendite und Volatilität p. a.
# Quellen und Evidenzgrad: Seite „Annahmen & Methodik“ (templates/depots/assumptions.html).
DEFAULT_ASSUMPTIONS: dict[AssetClass, tuple[float, float]] = {
    AssetClass.EQUITIES: (0.050, 0.17),     # DMS/UBS Yearbook 2025: Welt 5,2 % real 1900–2024
    AssetClass.BONDS: (0.017, 0.07),        # ebd.: 1,7 % real
    AssetClass.LIQUIDITY: (0.005, 0.01),    # ebd.: Bills 0,5 % real
    AssetClass.REAL_ESTATE: (0.040, 0.18),  # eigene Annahme
    AssetClass.COMMODITIES: (0.010, 0.16),  # Erb & Harvey 2013 (Gold)
    AssetClass.CRYPTO: (0.000, 0.70),       # keine belastbare Evidenz
    AssetClass.OTHER: (0.020, 0.10),        # eigene Annahme
}

# Grobe Korrelationsannahmen; fehlende Paare 0,1.
_CORRELATION = {
    (AssetClass.EQUITIES, AssetClass.BONDS): 0.1,
    (AssetClass.EQUITIES, AssetClass.REAL_ESTATE): 0.7,
    (AssetClass.EQUITIES, AssetClass.COMMODITIES): 0.2,
    (AssetClass.EQUITIES, AssetClass.CRYPTO): 0.4,
    (AssetClass.EQUITIES, AssetClass.LIQUIDITY): 0.0,
    (AssetClass.BONDS, AssetClass.LIQUIDITY): 0.3,
    (AssetClass.BONDS, AssetClass.REAL_ESTATE): 0.2,
    (AssetClass.BONDS, AssetClass.COMMODITIES): 0.0,
}


def correlation(a: AssetClass, b: AssetClass) -> float:
    if a == b:
        return 1.0
    return _CORRELATION.get((a, b), _CORRELATION.get((b, a), 0.1))


class TxLike(Protocol):
    depot_id: int
    security_id: int
    trade_date: date
    kind: TransactionKind
    quantity: float
    amount_cent: int
    fee_cent: int


# --------------------------------------------------------------------------- Steuer


def capital_gains_tax_rate(church_tax_rate: float = 0.0) -> float:
    """Effektiver Satz auf Kapitalerträge.

    § 32d Abs. 1 EStG: Einkommensteuer = e / (4 + k) bei Kirchensteuersatz k.
    Darauf Soli 5,5 % (§ 4 SolZG) und KiSt k. Ohne KiSt 26,375 %, mit 8 % ≈ 27,82 %.
    """
    k = church_tax_rate or 0.0
    return (1 + 0.055 + k) / (4 + k)


def advance_lump_sum_cent(
    quantity: float,
    price_year_start_cent: int | None,
    price_now_cent: int,
    distributions_cent: int,
    base_rate: float,
    purchase_month: int | None = None,
) -> int:
    """Vorabpauschale eines Jahres (vor Teilfreistellung), vorläufig.

    § 18 InvStG: Basisertrag = Rücknahmepreis zu Jahresbeginn × Basiszins × 70 %,
    gekürzt um 1/12 je vollem Monat vor dem Erwerb (Abs. 2), höchstens Wertzuwachs
    plus Ausschüttungen (Abs. 1 S. 3), abzüglich Ausschüttungen. Vor Jahresende wird
    der aktuelle Kurs als Näherung für den Jahresendkurs verwendet.
    """
    if not price_year_start_cent or quantity <= 0:
        return 0
    base = price_year_start_cent * quantity * base_rate * 0.7
    if purchase_month:
        base *= (13 - purchase_month) / 12
    gain = (price_now_cent - price_year_start_cent) * quantity + distributions_cent
    base = min(base, max(gain, 0))
    return max(round(base - distributions_cent), 0)


# --------------------------------------------------------------------------- Bestand


@dataclass
class Holding:
    security_id: int
    quantity: float = 0.0
    cost_cent: int = 0                 # Anschaffungskosten der Restlots inkl. Kaufgebühren
    realized_cent: int = 0             # realisierter G/V vor Steuern
    distributions_cent: int = 0
    lots: list = field(default_factory=list)  # [menge, kosten_cent]


def _sort_key(t) -> tuple:
    return (t.trade_date, getattr(t, "id", 0) or 0)


def holdings(transactions: Iterable[TxLike], as_of: date | None = None) -> dict[int, Holding]:
    """Bestand je Wertpapier nach FIFO. Verkäufe verbrauchen die ältesten Lots zuerst."""
    result: dict[int, Holding] = {}
    for t in sorted(transactions, key=_sort_key):
        if as_of and t.trade_date > as_of:
            break
        h = result.setdefault(t.security_id, Holding(t.security_id))
        if t.kind == TransactionKind.BUY:
            cost = t.amount_cent + t.fee_cent
            h.lots.append([t.quantity, cost])
            h.quantity += t.quantity
            h.cost_cent += cost
        elif t.kind == TransactionKind.SELL:
            rest = t.quantity
            used = 0
            while rest > 1e-9 and h.lots:
                lot = h.lots[0]
                take = min(rest, lot[0])
                part = lot[1] if take >= lot[0] - 1e-9 else round(lot[1] * take / lot[0])
                used += part
                lot[0] -= take
                lot[1] -= part
                rest -= take
                if lot[0] <= 1e-9:
                    h.lots.pop(0)
            h.realized_cent += t.amount_cent - t.fee_cent - used
            h.quantity -= t.quantity - rest
            h.cost_cent -= used
        elif t.kind == TransactionKind.DIVIDEND:
            h.distributions_cent += t.amount_cent
    for h in result.values():
        if abs(h.quantity) < 1e-9:
            h.quantity, h.cost_cent = 0.0, 0
    return result


def cash_flow_cent(t: TxLike) -> int:
    """Zahlung aus Sicht des Anlegers: Kauf negativ, Verkauf und Ausschüttung positiv."""
    if t.kind == TransactionKind.BUY:
        return -(t.amount_cent + t.fee_cent)
    if t.kind == TransactionKind.SELL:
        return t.amount_cent - t.fee_cent
    return t.amount_cent


def net_contribution_cent(transactions: Iterable[TxLike], after: date | None, until: date) -> int:
    """Saldo der Ein- und Auszahlungen im Zeitraum (after, until] aus Sicht des Depots.

    Käufe sind Einzahlungen (+), Verkaufserlöse und Ausschüttungen verlassen das Depot (−).
    Entspricht Snapshot.net_contribution_cent der Positionen.
    """
    return -sum(
        cash_flow_cent(t)
        for t in transactions
        if (after is None or t.trade_date > after) and t.trade_date <= until
    )


class PriceBook:
    """Kurse je Wertpapier; liefert den letzten Kurs am/vor einem Stichtag.

    Fehlt ein gepflegter Kurs oder ist der letzte Transaktionskurs jünger, gilt dieser.
    """

    def __init__(self, prices: Iterable, transactions: Iterable[TxLike]):
        self._p: dict[int, list[tuple[date, int]]] = defaultdict(list)
        for p in prices:
            self._p[p.security_id].append((p.price_date, p.price_cent))
        for t in transactions:
            if t.kind in (TransactionKind.BUY, TransactionKind.SELL) and t.quantity > 0:
                self._p[t.security_id].append((t.trade_date, round(t.amount_cent / t.quantity)))
        # Gepflegte Kurse gewinnen bei gleichem Datum (stabile Sortierung, später eingefügt = Transaktion)
        for sid, lst in self._p.items():
            seen: dict[date, int] = {}
            for d, c in lst:
                seen.setdefault(d, c)
            self._p[sid] = sorted(seen.items())

    def at(self, security_id: int, as_of: date) -> tuple[int, date | None]:
        lst = self._p.get(security_id, [])
        i = bisect.bisect_right(lst, (as_of, float("inf"))) - 1
        if i < 0:
            return 0, None
        return lst[i][1], lst[i][0]

    def dates(self, security_ids: Iterable[int], start: date, end: date) -> set[date]:
        out: set[date] = set()
        for sid in security_ids:
            out |= {d for d, _ in self._p.get(sid, []) if start <= d <= end}
        return out


# --------------------------------------------------------------------------- Rendite


def xirr(flows: list[tuple[date, float]]) -> Optional[float]:
    """Interner Zinsfuß p. a. (Act/365). Newton-Verfahren mit Bisektion als Rückfall.

    Vorzeichen: Einzahlungen negativ, Rückflüsse und Endwert positiv.
    """
    flows = [(d, float(b)) for d, b in flows if abs(b) > 1e-9]
    if len(flows) < 2 or not any(b < 0 for _, b in flows) or not any(b > 0 for _, b in flows):
        return None
    t0 = min(d for d, _ in flows)
    ys = [((d - t0).days / 365.0, b) for d, b in flows]
    if max(y for y, _ in ys) < 1 / 365:
        return None

    def npv(r: float) -> float:
        return sum(b / (1 + r) ** y for y, b in ys)

    def dnpv(r: float) -> float:
        return sum(-y * b / (1 + r) ** (y + 1) for y, b in ys)

    r = 0.05
    for _ in range(100):
        try:
            f, df = npv(r), dnpv(r)
        except (OverflowError, ZeroDivisionError):
            break
        if df == 0:
            break
        new = r - f / df
        if new <= -0.9999:
            break
        if abs(new - r) < 1e-10:
            return new
        r = new
    lo, hi = -0.9999, 10.0
    try:
        flo, fhi = npv(lo), npv(hi)
    except OverflowError:
        return None
    if flo * fhi > 0:
        return None
    for _ in range(300):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if flo * fm <= 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


# --------------------------------------------------------------------------- Prognose


def portfolio_parameters(
    weights: dict[AssetClass, float],
    assumptions: dict[AssetClass, tuple[float, float]],
    ter: float = 0.0,
) -> tuple[float, float]:
    """Log-Drift µ und Volatilität σ (real, p. a.) eines Portfolios mit konstanten Gewichten.

    µ = Σ wᵢ ln(1+gᵢ) + ½ (Σ wᵢ σᵢ² − σₚ²) − ln(1+TER)
    Der mittlere Term ist die Diversifikationsrendite bei Rebalancing (Booth & Fama 1992,
    Financial Analysts Journal 48(3)). gᵢ sind geometrische (Median-)Renditen.
    """
    classes = [c for c, w in weights.items() if w > 0]
    if not classes:
        return 0.0, 0.0
    total = sum(weights[c] for c in classes)
    w = [weights[c] / total for c in classes]
    g = [assumptions.get(c, DEFAULT_ASSUMPTIONS[AssetClass.OTHER])[0] for c in classes]
    s = [assumptions.get(c, DEFAULT_ASSUMPTIONS[AssetClass.OTHER])[1] for c in classes]
    var_p = sum(
        w[i] * w[j] * s[i] * s[j] * correlation(classes[i], classes[j])
        for i in range(len(classes))
        for j in range(len(classes))
    )
    mu = (
        sum(wi * math.log1p(gi) for wi, gi in zip(w, g))
        + 0.5 * (sum(wi * si * si for wi, si in zip(w, s)) - var_p)
        - math.log1p(ter)
    )
    return mu, math.sqrt(max(var_p, 0.0))


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


@dataclass
class Forecast:
    years: list[int]
    p10: list[float]
    p25: list[float]
    p50: list[float]
    p75: list[float]
    p90: list[float]
    contributions: list[float]
    prob_below_contributions: float
    mu: float
    sigma: float


def forecast(
    start_eur: float,
    monthly_savings_eur: float,
    mu: float,
    sigma: float,
    years: int,
    paths: int = 2000,
    seed: int = 42,
) -> Forecast:
    """Monte-Carlo in realen Euro (heutige Kaufkraft).

    Monatliche log-normale Renditen, Sparrate am Monatsende, real konstant (nominal also
    mit der Inflation steigend). Fester Seed, damit die Anzeige stabil bleibt.
    """
    rng = random.Random(seed)
    m, s = mu / 12, sigma / math.sqrt(12)
    months = years * 12
    yearly: list[list[float]] = [[] for _ in range(years + 1)]
    for _ in range(paths):
        v = start_eur
        yearly[0].append(v)
        for i in range(1, months + 1):
            v = v * math.exp(rng.gauss(m, s)) + monthly_savings_eur
            if i % 12 == 0:
                yearly[i // 12].append(v)
    for lst in yearly:
        lst.sort()
    contributions = [start_eur + monthly_savings_eur * 12 * y for y in range(years + 1)]
    below = sum(1 for v in yearly[-1] if v < contributions[-1]) / paths if years else 0.0
    pct = {q: [_percentile(lst, q) for lst in yearly] for q in (0.1, 0.25, 0.5, 0.75, 0.9)}
    return Forecast(
        years=list(range(years + 1)),
        p10=pct[0.1], p25=pct[0.25], p50=pct[0.5], p75=pct[0.75], p90=pct[0.9],
        contributions=contributions, prob_below_contributions=below, mu=mu, sigma=sigma,
    )


# --------------------------------------------------------------------------- Auswertung


@dataclass
class PositionRow:
    depot_id: int
    security: object
    quantity: float
    cost_cent: int
    price_cent: int
    price_date: date | None
    value_cent: int
    realized_cent: int
    distributions_cent: int
    advance_lump_sum_cent: int = 0
    share: float = 0.0

    @property
    def gain_cent(self) -> int:
        return self.value_cent - self.cost_cent

    @property
    def gain_ratio(self) -> float | None:
        return self.gain_cent / self.cost_cent if self.cost_cent else None

    @property
    def taxable_gain_cent(self) -> int:
        """Unrealisierter G/V nach Teilfreistellung (nur Fonds)."""
        ex = self.security.partial_exemption if self.security.is_fund else 0.0
        return round(self.gain_cent * (1 - ex))

    @property
    def taxable_advance_cent(self) -> int:
        ex = self.security.partial_exemption if self.security.is_fund else 0.0
        return round(self.advance_lump_sum_cent * (1 - ex))


@dataclass
class DepotSummary:
    depot: object
    positions: list[PositionRow]
    value_cent: int
    invested_cent: int          # Käufe inkl. Gebühren
    returned_cent: int          # Verkaufserlöse + Ausschüttungen
    realized_cent: int
    distributions_cent: int
    xirr: float | None

    @property
    def net_invested_cent(self) -> int:
        """Käufe inkl. Gebühren minus Verkaufserlöse (Ausschüttungen zählen als Ertrag)."""
        return self.invested_cent - (self.returned_cent - self.distributions_cent)

    @property
    def gain_cent(self) -> int:
        """Gesamterfolg: Wert + alle Rückflüsse − Einzahlungen."""
        return self.value_cent + self.returned_cent - self.invested_cent

    @property
    def gain_ratio(self) -> float | None:
        return self.gain_cent / self.invested_cent if self.invested_cent else None

    @property
    def ter(self) -> float:
        return (
            sum(p.security.ter * p.value_cent for p in self.positions) / self.value_cent
            if self.value_cent else 0.0
        )


def summarize_depot(depot, transactions: list[TxLike], book: PriceBook, as_of: date, base_rate: float) -> DepotSummary:
    txs = [t for t in transactions if t.trade_date <= as_of]
    hs = holdings(txs)
    year_start = date(as_of.year, 1, 2)
    prev_year_end = date(as_of.year - 1, 12, 31)
    rows: list[PositionRow] = []
    for sid, h in hs.items():
        if h.quantity <= 0:
            continue
        sec_txs = [t for t in txs if t.security_id == sid]
        sec = sec_txs[0].security if hasattr(sec_txs[0], "security") else None
        price, price_date = book.at(sid, as_of)
        value = round(h.quantity * price)
        row = PositionRow(
            depot_id=depot.id, security=sec, quantity=h.quantity, cost_cent=h.cost_cent,
            price_cent=price, price_date=price_date, value_cent=value,
            realized_cent=h.realized_cent, distributions_cent=h.distributions_cent,
        )
        if sec is not None and sec.is_fund:
            held_before = holdings(sec_txs, prev_year_end).get(sid)
            purchase_month = None
            start_price = book.at(sid, year_start)[0] if held_before and held_before.quantity > 0 else None
            if not held_before or held_before.quantity <= 0:
                buys = [t for t in sec_txs if t.kind == TransactionKind.BUY and t.trade_date.year == as_of.year]
                if buys:
                    first = min(buys, key=_sort_key)
                    purchase_month = first.trade_date.month
                    start_price = round(first.amount_cent / first.quantity) if first.quantity else None
            dist = sum(
                t.amount_cent for t in sec_txs
                if t.kind == TransactionKind.DIVIDEND and t.trade_date.year == as_of.year
            )
            row.advance_lump_sum_cent = advance_lump_sum_cent(
                h.quantity, start_price, price, dist, base_rate, purchase_month
            )
        rows.append(row)
    value = sum(r.value_cent for r in rows)
    for r in rows:
        r.share = r.value_cent / value if value else 0.0
    invested = sum(t.amount_cent + t.fee_cent for t in txs if t.kind == TransactionKind.BUY)
    returned = sum(cash_flow_cent(t) for t in txs if t.kind != TransactionKind.BUY)
    flows = [(t.trade_date, cash_flow_cent(t) / 100) for t in txs] + [(as_of, value / 100)]
    return DepotSummary(
        depot=depot,
        positions=sorted(rows, key=lambda r: -r.value_cent),
        value_cent=value,
        invested_cent=invested,
        returned_cent=returned,
        realized_cent=sum(h.realized_cent for h in hs.values()),
        distributions_cent=sum(h.distributions_cent for h in hs.values()),
        xirr=xirr(flows) if value > 0 else None,
    )


def value_by_class_cent(transactions: list[TxLike], book: PriceBook, as_of: date) -> dict[AssetClass, int]:
    """Depotwert je Anlageklasse zum Stichtag (für die Übernahme in Positionen)."""
    out: dict[AssetClass, int] = defaultdict(int)
    sec_of = {t.security_id: t.security for t in transactions if hasattr(t, "security")}
    for sid, h in holdings(transactions, as_of).items():
        if h.quantity > 0:
            out[sec_of[sid].asset_class] += round(h.quantity * book.at(sid, as_of)[0])
    return dict(out)


def value_series(transactions: list[TxLike], book: PriceBook, as_of: date, max_points: int = 300) -> list[tuple[date, int, int]]:
    """Zeitreihe (Datum, Wert, Netto-Einzahlungen) an allen Kurs- und Transaktionsdaten."""
    txs = sorted((t for t in transactions if t.trade_date <= as_of), key=_sort_key)
    if not txs:
        return []
    start = txs[0].trade_date
    days = {t.trade_date for t in txs} | book.dates({t.security_id for t in txs}, start, as_of) | {as_of}
    days = sorted(days)
    if len(days) > max_points:
        step = len(days) / max_points
        days = sorted({days[int(i * step)] for i in range(max_points)} | {days[-1]})
    out = []
    for d in days:
        hs = holdings(txs, d)
        value = sum(round(h.quantity * book.at(sid, d)[0]) for sid, h in hs.items() if h.quantity > 0)
        net = sum(
            -cash_flow_cent(t) for t in txs
            if t.trade_date <= d and t.kind != TransactionKind.DIVIDEND
        )
        out.append((d, value, net))
    return out


@dataclass
class Hint:
    level: str   # "warn" | "info" | "ok"
    title: str
    text: str


def _de(x: float, digits: int = 1) -> str:
    return f"{x:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def hints(
    positions: list[PositionRow],
    total_cent: int,
    by_class: dict[AssetClass, int],
    concentration_limit: float,
    equity_target: tuple[float, float] | None,
    ter: float,
    ter_warn: float,
    monthly_savings_cent: int,
    as_of: date,
) -> list[Hint]:
    """Lücken und Auffälligkeiten auf Wertpapierebene."""
    out: list[Hint] = []
    if not total_cent:
        return out
    by_sec: dict[int, list[PositionRow]] = defaultdict(list)
    for p in positions:
        by_sec[p.security.id].append(p)
    for rows in by_sec.values():
        sec = rows[0].security
        share = sum(r.value_cent for r in rows) / total_cent
        if share <= concentration_limit or sec.asset_class == AssetClass.LIQUIDITY:
            continue
        broad = sec.is_fund and sec.asset_class in (AssetClass.EQUITIES, AssetClass.BONDS) and any(
            w in (sec.region or "") for w in ("Welt", "Global", "All-World")
        )
        if broad:
            out.append(Hint("info", "Große Einzelposition, breit gestreut",
                            f"{sec.name}: {_de(share * 100)} % des Depotvermögens. Als globaler Indexfonds intern diversifiziert."))
        else:
            out.append(Hint("warn", "Klumpenrisiko",
                            f"{sec.name}: {_de(share * 100)} % des Depotvermögens (Grenze {_de(concentration_limit * 100, 0)} %)."))
    if equity_target is not None:
        target, band = equity_target
        eq = by_class.get(AssetClass.EQUITIES, 0) / total_cent
        diff = eq - target
        if abs(diff) > band:
            out.append(Hint("warn", "Allokationslücke (Depot)",
                            f"Aktienquote {_de(eq * 100)} % vs. Ziel {_de(target * 100, 0)} % ± {_de(band * 100, 0)} "
                            f"({'+' if diff > 0 else '−'}{_de(abs(diff) * total_cent / 100, 0)} €)."))
    if ter > ter_warn:
        out.append(Hint("warn", "Kosten", f"Gewichtete TER {_de(ter * 100, 2)} % p. a. über {_de(ter_warn * 100, 2)} %."))
    crypto = by_class.get(AssetClass.CRYPTO, 0) / total_cent
    if crypto > 0.10:
        out.append(Hint("warn", "Hochvolatile Beimischung", f"Krypto-Anteil {_de(crypto * 100)} % (> 10 %)."))
    stale = [p for p in positions if p.price_date and (as_of - p.price_date).days > 100]
    if stale:
        out.append(Hint("info", "Kurse veraltet",
                        f"{len(stale)} Position(en) mit Kurs älter als 100 Tage – unter „Kurse“ aktualisieren."))
    if monthly_savings_cent <= 0:
        out.append(Hint("info", "Keine Sparrate hinterlegt", "Die Prognose rechnet ohne laufende Einzahlungen."))
    if not out:
        out.append(Hint("ok", "Keine Auffälligkeiten", "Alle Prüfungen bestanden."))
    return out
