from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import init_db, make_engine
from app.models import (
    PensionContract,
    PensionSettings,
    PensionSnapshot,
    PensionType,
    ValueBasis,
)
from app.pension import (
    Assumptions,
    annuity_factor,
    capital_to_monthly,
    deflate,
    estimate,
    future_value,
    gap_analysis,
    pension_value_at,
    years_until_retirement,
)

A = Assumptions(
    years_to_retirement=30,
    inflation=0.02,
    accumulation_real_return=0.03,
    payout_real_return=0.01,
    payout_years=25,
    current_pension_value_cent=4252,
)


def grv(net=0.78):
    return PensionContract(name="DRV", pension_type=PensionType.STATUTORY, net_ratio=net)


def bav(vested=True, net=0.70):
    return PensionContract(
        name="DV", pension_type=PensionType.OCCUPATIONAL, net_ratio=net, is_vested=vested
    )


# ------------------------------------------------------------------ Grundformeln


def test_annuity_factor():
    assert annuity_factor(0.0, 25) == 25
    assert annuity_factor(0.01, 25) == pytest.approx(22.0232, rel=1e-4)
    assert annuity_factor(0.05, 0) == 0


def test_deflate():
    # 500 € in 30 Jahren bei 2 % Inflation ~ 276 € heute
    assert deflate(500_00, 0.02, 30) == pytest.approx(276_04, abs=2)
    assert deflate(500_00, 0.02, 0) == 500_00


def test_capital_to_monthly_zero_rate():
    assert capital_to_monthly(300_000_00, 0.0, 25) == 1_000_00


def test_future_value():
    assert future_value(10_000_00, 0, 0.0, 10) == 10_000_00
    assert future_value(0, 100_00, 0.0, 10) == 12_000_00
    # 10.000 € bei 3 % über 10 Jahre
    assert future_value(10_000_00, 0, 0.03, 10) == pytest.approx(13_439_16, abs=1)


def test_years_until_retirement():
    assert years_until_retirement(None, 67, date(2026, 9, 24)) is None
    y = years_until_retirement(date(1990, 1, 1), 67, date(2026, 9, 24))
    assert 30 < y < 31
    assert years_until_retirement(date(1950, 1, 1), 67, date(2026, 9, 24)) == 0.0


def test_pension_value_history():
    assert pension_value_at(date(2026, 6, 30)) == 4079
    assert pension_value_at(date(2026, 7, 1)) == 4252
    assert pension_value_at(date(2010, 1, 1)) is None


# ------------------------------------------------------------------ GRV


def test_grv_projection_rescaled_to_current_pension_value():
    # Renteninformation von 2025 (Rentenwert 40,79 €): Hochrechnung 1.631,60 € = 40 EP
    snap = PensionSnapshot(as_of=date(2025, 10, 1), projected_monthly_cent=1_631_60)
    est = estimate(grv(), snap, A)
    assert est.earning_points == pytest.approx(40.0)
    assert est.expected_gross_cent == 40 * 4252
    assert est.conservative_gross_cent == est.expected_gross_cent
    assert est.warnings == []


def test_grv_uses_explicit_pension_value_over_table():
    snap = PensionSnapshot(as_of=date(2025, 10, 1), projected_monthly_cent=2_000_00, pension_value_cent=4000)
    assert estimate(grv(), snap, A).earning_points == pytest.approx(50.0)


def test_grv_falls_back_to_earning_points_with_warning():
    snap = PensionSnapshot(as_of=date(2026, 10, 1), earning_points=12.5)
    est = estimate(grv(), snap, A)
    assert est.expected_gross_cent == round(12.5 * 4252)
    assert est.warnings


def test_grv_without_values():
    est = estimate(grv(), PensionSnapshot(as_of=date(2026, 10, 1)), A)
    assert est.expected_gross_cent == 0 and est.warnings


# ------------------------------------------------------------------ bAV


def test_bav_nominal_is_deflated():
    snap = PensionSnapshot(
        as_of=date(2026, 1, 1), value_basis=ValueBasis.NOMINAL,
        guaranteed_monthly_cent=300_00, projected_monthly_cent=500_00,
    )
    est = estimate(bav(), snap, A)
    assert est.conservative_gross_cent == deflate(300_00, 0.02, 30)
    assert est.expected_gross_cent == deflate(500_00, 0.02, 30)


def test_bav_real_is_not_deflated():
    snap = PensionSnapshot(as_of=date(2026, 1, 1), value_basis=ValueBasis.REAL, guaranteed_monthly_cent=300_00)
    est = estimate(bav(), snap, A)
    assert est.conservative_gross_cent == est.expected_gross_cent == 300_00


def test_bav_capital_only():
    snap = PensionSnapshot(as_of=date(2026, 1, 1), value_basis=ValueBasis.REAL, projected_capital_cent=100_000_00)
    est = estimate(bav(), snap, A)
    assert est.expected_gross_cent == capital_to_monthly(100_000_00, 0.01, 25)
    assert any("Garantie" in w for w in est.warnings)


def test_bav_not_vested_warns():
    snap = PensionSnapshot(as_of=date(2026, 1, 1), value_basis=ValueBasis.REAL, guaranteed_monthly_cent=100_00)
    assert any("unverfallbar" in w for w in estimate(bav(vested=False), snap, A).warnings)


# ------------------------------------------------------------------ Steuer, KV/PV

from app.pension import (  # noqa: E402
    CONSERVATIVE,
    EXPECTED,
    HealthTaxSettings,
    PensionEstimate,
    income_breakdown,
    income_tax_2026,
    taxable_share,
)


def _est(kind, cons, exp=None, manual=False, ratio=1.0):
    e = PensionEstimate("x", kind, date(2026, 1, 1), manual_net=manual, net_ratio=ratio)
    e.conservative_gross_cent, e.expected_gross_cent = cons, exp if exp is not None else cons
    return e


PKV = HealthTaxSettings("PKV", 2062, pkv_health_monthly_cent=600_00, pkv_care_monthly_cent=80_00, pkv_basic_share=0.8)
GKV = HealthTaxSettings("GKV", 2062)


def test_income_tax_2026_zones():
    assert income_tax_2026(12_348) == 0
    assert income_tax_2026(17_799) == 1_034           # Zonengrenze, stetiger Übergang
    assert income_tax_2026(17_800) == 1_035
    assert income_tax_2026(30_000) == 4_217
    # Stetigkeit an den Zonengrenzen: Formeln der Nachbarzonen treffen sich
    assert income_tax_2026(69_878) == int(0.42 * 69_878 - 11_135.63) == 18_213
    assert income_tax_2026(100_000) == 30_864
    assert income_tax_2026(300_000) == int(0.45 * 300_000 - 19_470.38)


def test_taxable_share():
    assert taxable_share(2005) == 0.50
    assert taxable_share(2020) == pytest.approx(0.80)
    assert taxable_share(2026) == pytest.approx(0.84)
    assert taxable_share(2058) == pytest.approx(1.0)
    assert taxable_share(2070) == 1.0


def test_pkv_subsidy_and_no_contributions_on_bav():
    ests = [_est(PensionType.STATUTORY, 2_000_00), _est(PensionType.OCCUPATIONAL, 400_00)]
    b = income_breakdown(ests, EXPECTED, PKV)
    assert b.kv_subsidy_cent == 175_00          # 8,75 % von 2.000 €, unter der Kappung (300 €)
    assert b.health_cent == 680_00              # PKV + PPV, keine Beiträge auf die bAV
    # zvE = (2.400 * 12) - 102 - 36 - (480 + 80 - 175) * 12 = 24.042 €
    assert b.income_tax_cent == round(income_tax_2026(24_042) * 100 / 12)
    assert b.available_cent == 2_400_00 + 175_00 - 680_00 - b.income_tax_cent


def test_pkv_subsidy_capped_at_half_premium():
    b = income_breakdown([_est(PensionType.STATUTORY, 5_000_00)], EXPECTED,
                         HealthTaxSettings("PKV", 2062, pkv_health_monthly_cent=300_00))
    assert b.kv_subsidy_cent == 150_00


def test_gkv_contributions():
    ests = [_est(PensionType.STATUTORY, 2_000_00), _est(PensionType.OCCUPATIONAL, 400_00)]
    b = income_breakdown(ests, EXPECTED, GKV)
    kv_grv = 2_000_00 * 0.0875
    kv_bav = (400_00 - 197_75) * 0.175
    care = 2_400_00 * 0.036
    assert b.health_cent == round(kv_grv + kv_bav + care)
    assert b.kv_subsidy_cent == 0


def test_gkv_small_bav_below_threshold_is_free():
    b = income_breakdown([_est(PensionType.OCCUPATIONAL, 150_00)], EXPECTED, GKV)
    assert b.health_cent == 0


def test_manual_net_bypasses_calculation():
    b = income_breakdown([_est(PensionType.OCCUPATIONAL, 1_000_00, manual=True, ratio=0.7)], EXPECTED, GKV)
    assert (b.manual_net_cent, b.health_cent, b.income_tax_cent) == (700_00, 0, 0)


def test_scenarios_use_their_own_gross():
    ests = [_est(PensionType.OCCUPATIONAL, 100_00, 300_00)]
    assert income_breakdown(ests, CONSERVATIVE, PKV).gross_occupational_cent == 100_00
    assert income_breakdown(ests, EXPECTED, PKV).gross_occupational_cent == 300_00


# ------------------------------------------------------------------ Lücke


NO_DEDUCTIONS = HealthTaxSettings("PKV", 2062)  # PKV-Beitrag 0: keine Steuer bei kleinen Renten


def test_gap_analysis():
    ests = [_est(PensionType.STATUTORY, 800_00), _est(PensionType.OCCUPATIONAL, 100_00, 150_00)]
    res = gap_analysis(ests, 2_500_00, 0, 0, A, NO_DEDUCTIONS)
    # unter dem Grundfreibetrag, kein Zuschuss ohne PKV-Beitrag
    assert res.conservative.income.available_cent == 900_00
    assert res.expected.income.available_cent == 950_00
    assert res.conservative.gap_monthly_cent == 1_600_00
    assert res.conservative.capital_need_cent == round(1_600_00 * 12 * annuity_factor(0.01, 25))
    assert res.conservative.coverage == 0.0


def test_gap_zero_when_pensions_exceed_target():
    res = gap_analysis([_est(PensionType.STATUTORY, 1_000_00)], 800_00, 0, 0, A, NO_DEDUCTIONS)
    assert res.expected.gap_monthly_cent == 0
    assert res.expected.coverage is None


# ------------------------------------------------------------------ Modell


@pytest.fixture()
def session():
    engine = make_engine("sqlite://")
    init_db(engine)
    with Session(engine) as s:
        yield s


def test_pension_snapshot_unique_per_date(session):
    c = grv()
    session.add(c)
    session.flush()
    session.add(PensionSnapshot(contract_id=c.id, as_of=date(2026, 1, 1)))
    session.commit()
    session.add(PensionSnapshot(contract_id=c.id, as_of=date(2026, 1, 1)))
    with pytest.raises(IntegrityError):
        session.commit()


def test_net_ratio_range(session):
    session.add(PensionContract(name="X", pension_type=PensionType.STATUTORY, net_ratio=1.5))
    with pytest.raises(IntegrityError):
        session.commit()


def test_pension_settings_defaults(session):
    session.add(PensionSettings())
    session.commit()
    s = session.get(PensionSettings, 1)
    assert (s.current_pension_value_cent, s.payout_years) == (4252, 25)


def test_init_db_adds_missing_columns(tmp_path):
    """Alte Datenbank ohne neue Spalten wird beim Start ergänzt, Daten bleiben erhalten."""
    import sqlite3

    from app.models import HealthInsurance

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE pension_settings (id INTEGER PRIMARY KEY, current_pension_value_cent INTEGER,"
                " payout_years INTEGER, payout_real_return FLOAT)")
    con.execute("INSERT INTO pension_settings VALUES (1, 4252, 30, 0.01)")
    con.commit()
    con.close()

    engine = make_engine(f"sqlite:///{db}")
    init_db(engine)
    init_db(engine)  # idempotent
    with Session(engine) as s:
        row = s.get(PensionSettings, 1)
        assert row.payout_years == 30
        assert row.health_insurance == HealthInsurance.PKV
        assert row.pkv_basic_share == 0.8
        assert row.pkv_health_monthly_cent == 0


# ------------------------------------------------------------------ PKV-Hochrechnung

from app.pension import PkvToday, project_pkv  # noqa: E402


def test_project_pkv_removes_components_and_grows():
    p = PkvToday(total_cent=720_00, sick_pay_cent=45_00, surcharge_cent=60_00,
                 relief_contribution_cent=50_00, care_cent=80_00, real_increase=0.015)
    r = project_pkv(p, 30, 0.02)
    assert r.base_cent == 565_00
    assert r.grown_cent == round(565_00 * 1.015 ** 30)
    assert r.health_cent == r.grown_cent
    assert r.care_cent == round(80_00 * 1.015 ** 30)


def test_project_pkv_relief_is_deflated():
    p = PkvToday(total_cent=600_00, relief_benefit_cent=300_00, real_increase=0.0)
    r = project_pkv(p, 30, 0.02)
    assert r.relief_real_cent == deflate(300_00, 0.02, 30)      # ~166 €
    assert r.health_cent == 600_00 - r.relief_real_cent


def test_project_pkv_never_negative_and_zero_years():
    r = project_pkv(PkvToday(total_cent=100_00, relief_benefit_cent=500_00), 0, 0.02)
    assert r.health_cent == 0
    assert r.grown_cent == 100_00
