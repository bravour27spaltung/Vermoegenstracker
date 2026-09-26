from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import init_db, make_engine
from app.models import ExpenseCategory, ExpenseCategoryRecord


@pytest.fixture()
def session():
    engine = make_engine("sqlite://")
    init_db(engine)
    with Session(engine) as s:
        yield s


def test_category_name_unique(session):
    session.add(ExpenseCategory(name="Miete"))
    session.commit()
    session.add(ExpenseCategory(name="Miete"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_record_unique_per_category_and_date(session):
    cat = ExpenseCategory(name="Fitnessstudio")
    session.add(cat)
    session.flush()
    session.add(ExpenseCategoryRecord(category_id=cat.id, snapshot_date=date(2026, 3, 31), monthly_amount_cent=4990))
    session.commit()
    session.add(ExpenseCategoryRecord(category_id=cat.id, snapshot_date=date(2026, 3, 31), monthly_amount_cent=5990))
    with pytest.raises(IntegrityError):
        session.commit()


def test_record_rejects_negative_amount(session):
    cat = ExpenseCategory(name="Handyvertrag")
    session.add(cat)
    session.flush()
    session.add(ExpenseCategoryRecord(category_id=cat.id, snapshot_date=date(2026, 3, 31), monthly_amount_cent=-1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_foreign_key_enforced(session):
    session.add(ExpenseCategoryRecord(category_id=999, snapshot_date=date(2026, 3, 31), monthly_amount_cent=100))
    with pytest.raises(IntegrityError):
        session.commit()


def test_record_history_per_category(session):
    cat = ExpenseCategory(name="KFZ-Versicherung")
    session.add(cat)
    session.flush()
    session.add_all([
        ExpenseCategoryRecord(category_id=cat.id, snapshot_date=date(2025, 12, 31), monthly_amount_cent=3500),
        ExpenseCategoryRecord(category_id=cat.id, snapshot_date=date(2026, 12, 31), monthly_amount_cent=3800),
    ])
    session.commit()
    session.refresh(cat)
    assert [r.monthly_amount_cent for r in cat.records] == [3500, 3800]


def test_deactivated_category_keeps_records(session):
    cat = ExpenseCategory(name="Streaming")
    session.add(cat)
    session.flush()
    session.add(ExpenseCategoryRecord(category_id=cat.id, snapshot_date=date(2026, 3, 31), monthly_amount_cent=1299))
    session.commit()
    cat.is_active = False
    session.commit()
    session.refresh(cat)
    assert cat.is_active is False
    assert len(cat.records) == 1
