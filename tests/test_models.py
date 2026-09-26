from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import init_db, make_engine
from app.models import (
    AssetClass,
    Position,
    PositionKind,
    Profile,
    Snapshot,
    TargetAllocation,
)
from app.queries import net_worth_series


@pytest.fixture()
def session():
    engine = make_engine("sqlite://")
    init_db(engine)
    with Session(engine) as s:
        yield s


def _asset(name="ETF-Depot", cls=AssetClass.EQUITIES):
    return Position(name=name, kind=PositionKind.ASSET, asset_class=cls)


def _loan(name="Baudarlehen"):
    return Position(name=name, kind=PositionKind.LIABILITY)


def test_asset_requires_class(session):
    session.add(Position(name="X", kind=PositionKind.ASSET))
    with pytest.raises(IntegrityError):
        session.commit()


def test_liability_must_not_have_class(session):
    session.add(Position(name="X", kind=PositionKind.LIABILITY, asset_class=AssetClass.OTHER))
    with pytest.raises(IntegrityError):
        session.commit()


def test_snapshot_unique_per_date_and_position(session):
    p = _asset()
    session.add(p)
    session.flush()
    session.add(Snapshot(snapshot_date=date(2026, 3, 31), position_id=p.id, value_cent=100))
    session.commit()
    session.add(Snapshot(snapshot_date=date(2026, 3, 31), position_id=p.id, value_cent=200))
    with pytest.raises(IntegrityError):
        session.commit()


def test_snapshot_rejects_negative_value(session):
    p = _asset()
    session.add(p)
    session.flush()
    session.add(Snapshot(snapshot_date=date(2026, 3, 31), position_id=p.id, value_cent=-1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_foreign_key_enforced(session):
    session.add(Snapshot(snapshot_date=date(2026, 3, 31), position_id=999, value_cent=1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_profile_single_row(session):
    session.add(Profile())
    session.commit()
    session.add(Profile(id=2))
    with pytest.raises(IntegrityError):
        session.commit()


def test_target_share_range(session):
    session.add(TargetAllocation(asset_class=AssetClass.EQUITIES, target_share=1.2))
    with pytest.raises(IntegrityError):
        session.commit()


def test_net_worth_series(session):
    etf, cash, loan = _asset(), _asset("Tagesgeld", AssetClass.LIQUIDITY), _loan()
    session.add_all([etf, cash, loan])
    session.flush()
    d1, d2 = date(2025, 12, 31), date(2026, 3, 31)
    session.add_all(
        [
            Snapshot(snapshot_date=d1, position_id=etf.id, value_cent=10_000_00),
            Snapshot(snapshot_date=d1, position_id=cash.id, value_cent=2_000_00),
            Snapshot(snapshot_date=d1, position_id=loan.id, value_cent=5_000_00),
            Snapshot(snapshot_date=d2, position_id=etf.id, value_cent=11_000_00),
        ]
    )
    session.commit()

    series = net_worth_series(session)
    assert [p.snapshot_date for p in series] == [d1, d2]
    assert series[0].assets_cent == 12_000_00
    assert series[0].liabilities_cent == 5_000_00
    assert series[0].net_cent == 7_000_00
    assert series[1].net_cent == 11_000_00
