from dataclasses import dataclass
from datetime import date

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import PensionContract, PensionSnapshot, Position, PositionKind, Snapshot


@dataclass(frozen=True)
class NetWorthPoint:
    snapshot_date: date
    assets_cent: int
    liabilities_cent: int

    @property
    def net_cent(self) -> int:
        return self.assets_cent - self.liabilities_cent


def net_worth_series(session: Session) -> list[NetWorthPoint]:
    """Vermögen, Verbindlichkeiten und Nettovermögen je Stichtag (aufsteigend)."""
    assets = func.coalesce(
        func.sum(case((Position.kind == PositionKind.ASSET, Snapshot.value_cent), else_=0)), 0
    )
    liabilities = func.coalesce(
        func.sum(case((Position.kind == PositionKind.LIABILITY, Snapshot.value_cent), else_=0)), 0
    )
    stmt = (
        select(Snapshot.snapshot_date, assets, liabilities)
        .join(Position, Position.id == Snapshot.position_id)
        .group_by(Snapshot.snapshot_date)
        .order_by(Snapshot.snapshot_date)
    )
    return [NetWorthPoint(d, int(a), int(l)) for d, a, l in session.execute(stmt)]


def retirement_assets_cent(session: Session) -> int:
    """Summe des jeweils letzten Werts aller aktiven, ruhestandsrelevanten Vermögenspositionen."""
    latest = (
        select(Snapshot.position_id, func.max(Snapshot.snapshot_date).label("d"))
        .group_by(Snapshot.position_id)
        .subquery()
    )
    stmt = (
        select(func.coalesce(func.sum(Snapshot.value_cent), 0))
        .join(latest, (latest.c.position_id == Snapshot.position_id) & (latest.c.d == Snapshot.snapshot_date))
        .join(Position, Position.id == Snapshot.position_id)
        .where(
            Position.kind == PositionKind.ASSET,
            Position.is_active.is_(True),
            Position.retirement_relevant.is_(True),
        )
    )
    return int(session.execute(stmt).scalar_one())


def active_pensions_with_latest(session: Session) -> list[tuple[PensionContract, PensionSnapshot | None]]:
    """Aktive Rentenverträge mit ihrem jeweils jüngsten Stand (oder None)."""
    contracts = (
        session.execute(
            select(PensionContract)
            .where(PensionContract.is_active.is_(True))
            .order_by(PensionContract.pension_type.desc(), PensionContract.name)
        )
        .scalars()
        .all()
    )
    result = []
    for c in contracts:
        snap = session.execute(
            select(PensionSnapshot)
            .where(PensionSnapshot.contract_id == c.id)
            .order_by(PensionSnapshot.as_of.desc())
            .limit(1)
        ).scalar_one_or_none()
        result.append((c, snap))
    return result
