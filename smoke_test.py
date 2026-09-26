"""Manueller Test mit der echten SQLite-Datei (nicht in-memory wie die pytest-Tests).

Legt zwei Positionen mit je zwei Stichtagen an und gibt den Nettovermögensverlauf aus.
Beliebig oft ausführbar: vorhandene Testpositionen werden per Name wiederverwendet,
nicht dupliziert.
"""
from datetime import date

from sqlalchemy.orm import Session

from app.db import init_db, make_engine, make_session_factory
from app.models import AssetClass, Position, PositionKind, Snapshot
from app.money import format_euro, parse_german_amount
from app.queries import net_worth_series


def get_or_create_position(session: Session, name: str, **kwargs) -> Position:
    pos = session.query(Position).filter_by(name=name).one_or_none()
    if pos is None:
        pos = Position(name=name, **kwargs)
        session.add(pos)
        session.flush()
    return pos


def get_or_create_snapshot(session: Session, snapshot_date: date, position: Position, value_text: str, contribution_text: str = "0"):
    snap = (
        session.query(Snapshot)
        .filter_by(snapshot_date=snapshot_date, position_id=position.id)
        .one_or_none()
    )
    value_cent = parse_german_amount(value_text)
    contribution_cent = parse_german_amount(contribution_text)
    if snap is None:
        snap = Snapshot(
            snapshot_date=snapshot_date,
            position_id=position.id,
            value_cent=value_cent,
            net_contribution_cent=contribution_cent,
        )
        session.add(snap)
    else:
        snap.value_cent = value_cent
        snap.net_contribution_cent = contribution_cent


def main():
    engine = make_engine()  # sqlite:///data/tracker.db
    init_db(engine)
    Session = make_session_factory(engine)

    with Session() as session:
        etf = get_or_create_position(
            session, "Test-ETF-Depot", kind=PositionKind.ASSET, asset_class=AssetClass.EQUITIES
        )
        tagesgeld = get_or_create_position(
            session,
            "Test-Tagesgeld",
            kind=PositionKind.ASSET,
            asset_class=AssetClass.LIQUIDITY,
            is_liquid=True,
        )

        get_or_create_snapshot(session, date(2025, 12, 31), etf, "20.000,00", "0")
        get_or_create_snapshot(session, date(2025, 12, 31), tagesgeld, "5.000,00", "0")
        get_or_create_snapshot(session, date(2026, 3, 31), etf, "21.500,00", "1.000,00")
        get_or_create_snapshot(session, date(2026, 3, 31), tagesgeld, "5.200,00", "200,00")

        session.commit()

        print(f"Datenbank: {engine.url}\n")
        print("Nettovermögensverlauf:")
        for point in net_worth_series(session):
            print(
                f"  {point.snapshot_date}: "
                f"Vermögen {format_euro(point.assets_cent):>14}  "
                f"Verbindlichkeiten {format_euro(point.liabilities_cent):>12}  "
                f"Netto {format_euro(point.net_cent):>14}"
            )


if __name__ == "__main__":
    main()
