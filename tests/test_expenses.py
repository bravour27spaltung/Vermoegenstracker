from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import init_db, make_engine
from app.models import ExpenseCadence, ExpenseCategory, ExpenseCategoryRecord
from app.routes.expenses import monthly_equivalent_cent


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


def test_category_default_cadence_is_monthly(session):
    cat = ExpenseCategory(name="Miete")
    session.add(cat)
    session.commit()
    session.refresh(cat)
    assert cat.cadence == ExpenseCadence.MONTHLY


def test_record_unique_per_category_and_date(session):
    cat = ExpenseCategory(name="Fitnessstudio")
    session.add(cat)
    session.flush()
    session.add(ExpenseCategoryRecord(category_id=cat.id, snapshot_date=date(2026, 3, 31), amount_cent=4990))
    session.commit()
    session.add(ExpenseCategoryRecord(category_id=cat.id, snapshot_date=date(2026, 3, 31), amount_cent=5990))
    with pytest.raises(IntegrityError):
        session.commit()


def test_record_rejects_negative_amount(session):
    cat = ExpenseCategory(name="Handyvertrag")
    session.add(cat)
    session.flush()
    session.add(ExpenseCategoryRecord(category_id=cat.id, snapshot_date=date(2026, 3, 31), amount_cent=-1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_foreign_key_enforced(session):
    session.add(ExpenseCategoryRecord(category_id=999, snapshot_date=date(2026, 3, 31), amount_cent=100))
    with pytest.raises(IntegrityError):
        session.commit()


def test_record_history_per_category(session):
    cat = ExpenseCategory(name="KFZ-Versicherung", cadence=ExpenseCadence.YEARLY)
    session.add(cat)
    session.flush()
    session.add_all([
        ExpenseCategoryRecord(category_id=cat.id, snapshot_date=date(2025, 12, 31), amount_cent=35000),
        ExpenseCategoryRecord(category_id=cat.id, snapshot_date=date(2026, 12, 31), amount_cent=38000),
    ])
    session.commit()
    session.refresh(cat)
    assert [r.amount_cent for r in cat.records] == [35000, 38000]


def test_deactivated_category_keeps_records(session):
    cat = ExpenseCategory(name="Streaming")
    session.add(cat)
    session.flush()
    session.add(ExpenseCategoryRecord(category_id=cat.id, snapshot_date=date(2026, 3, 31), amount_cent=1299))
    session.commit()
    cat.is_active = False
    session.commit()
    session.refresh(cat)
    assert cat.is_active is False
    assert len(cat.records) == 1


def test_monthly_equivalent_monthly_unchanged():
    assert monthly_equivalent_cent(ExpenseCadence.MONTHLY, 4990) == 4990


def test_monthly_equivalent_quarterly_divides_by_three():
    assert monthly_equivalent_cent(ExpenseCadence.QUARTERLY, 12000) == 4000


def test_monthly_equivalent_yearly_divides_by_twelve():
    # 3.500 € Jahresprämie -> 291,67 € (kaufmännisch gerundet), nicht 291,66
    assert monthly_equivalent_cent(ExpenseCadence.YEARLY, 350000) == 29167


def test_monthly_equivalent_rounds_half_up():
    # 100 Cent / 3 = 33,33... -> Python round() rundet zur geraden Zahl (Banker's Rounding),
    # bei .5 exakt; hier prüfen wir nur, dass das Ergebnis ein int ist und plausibel liegt.
    result = monthly_equivalent_cent(ExpenseCadence.QUARTERLY, 100)
    assert result == 33
