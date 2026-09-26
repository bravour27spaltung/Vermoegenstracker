"""Investierbares und ruhestandsrelevantes Vermögen aus Depots und Positionen.

Verbindet das Depotmodul (Wertpapierebene) mit Positionen/Snapshots, ohne doppelt zu
zählen: Positionen, die per „Stichtag übernehmen“ mit einem Depot verknüpft sind
(DepotPositionLink), werden durch die tagesaktuellen Depotwerte ersetzt.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import depot as calc
from app.models import (
    AssetClass,
    Depot,
    DepotPositionLink,
    DepotSettings,
    DepotTransaction,
    Position,
    PositionKind,
    ReturnAssumption,
    SecurityPrice,
    Snapshot,
)
from app.retirement import TaxParams


@dataclass
class DepotTotals:
    value_cent: int
    basis_cent: int
    savings_monthly_cent: int
    mu: float                 # Log-Drift real, nach TER
    sigma: float
    tax: TaxParams
    weights: dict


@dataclass
class OtherAsset:
    name: str
    value_cent: int
    asset_class: AssetClass
    as_of: date


@dataclass
class Wealth:
    depots: DepotTotals
    other: list[OtherAsset]          # ruhestandsrelevante Positionen ohne Depot-Verknüpfung
    linked_position_names: list[str]

    @property
    def other_cent(self) -> int:
        return sum(o.value_cent for o in self.other)

    @property
    def other_accessible_cent(self) -> int:
        """Vor Rentenbeginn verfügbar (ohne Altersvorsorgeverträge der Klasse PENSION)."""
        return sum(o.value_cent for o in self.other if o.asset_class != AssetClass.PENSION)


def depot_totals(session: Session, as_of: date | None = None) -> DepotTotals:
    as_of = as_of or date.today()
    depots = list(session.execute(select(Depot).where(Depot.is_active.is_(True))).scalars())
    ids = [d.id for d in depots]
    txs = list(session.execute(
        select(DepotTransaction).options(selectinload(DepotTransaction.security))
        .where(DepotTransaction.depot_id.in_(ids))
    ).scalars()) if ids else []
    book = calc.PriceBook(session.execute(select(SecurityPrice)).scalars(), txs)
    settings = session.get(DepotSettings, 1) or DepotSettings(church_tax_rate=0.0, saver_allowance_cent=100000)
    value = basis = 0
    weights: dict[AssetClass, int] = {}
    ter_sum = exempt_sum = 0.0
    for d in depots:
        s = calc.summarize_depot(d, [t for t in txs if t.depot_id == d.id], book, as_of, settings.base_rate or 0.0)
        for p in s.positions:
            value += p.value_cent
            basis += p.cost_cent
            weights[p.security.asset_class] = weights.get(p.security.asset_class, 0) + p.value_cent
            ter_sum += p.security.ter * p.value_cent
            exempt_sum += (p.security.partial_exemption if p.security.is_fund else 0.0) * p.value_cent
    assumptions = dict(calc.DEFAULT_ASSUMPTIONS)
    for row in session.execute(select(ReturnAssumption)).scalars():
        assumptions[row.asset_class] = (row.real_return, row.volatility)
    mu, sigma = calc.portfolio_parameters(weights or {AssetClass.EQUITIES: 1}, assumptions,
                                          ter_sum / value if value else 0.0)
    tax = TaxParams(
        rate=calc.capital_gains_tax_rate(settings.church_tax_rate or 0.0),
        exemption=exempt_sum / value if value else 0.3,
        allowance_eur=(settings.saver_allowance_cent if settings.saver_allowance_cent is not None else 100000) / 100,
    )
    return DepotTotals(value, basis, sum(d.monthly_savings_cent for d in depots), mu, sigma, tax, weights)


def wealth(session: Session, as_of: date | None = None) -> Wealth:
    linked = set(session.execute(select(DepotPositionLink.position_id)).scalars())
    latest = (
        select(Snapshot.position_id, func.max(Snapshot.snapshot_date).label("d"))
        .group_by(Snapshot.position_id).subquery()
    )
    rows = session.execute(
        select(Position, Snapshot)
        .join(Snapshot, Snapshot.position_id == Position.id)
        .join(latest, (latest.c.position_id == Snapshot.position_id) & (latest.c.d == Snapshot.snapshot_date))
        .where(Position.kind == PositionKind.ASSET, Position.is_active.is_(True),
               Position.retirement_relevant.is_(True))
        .order_by(Position.name)
    ).all()
    other = [OtherAsset(p.name, s.value_cent, p.asset_class, s.snapshot_date)
             for p, s in rows if p.id not in linked and s.value_cent > 0]
    names = [p.name for p, _ in rows if p.id in linked]
    return Wealth(depot_totals(session, as_of), other, names)
