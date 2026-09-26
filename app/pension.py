"""Rechenlogik für Rentenansprüche und Rentenlücke.

Grundsätze
- Alle Ergebnisse in heutiger Kaufkraft (real) und netto, damit sie mit dem
  Wunscheinkommen aus dem Profil (ebenfalls real) vergleichbar sind.
- Gesetzliche Rente (GRV): Entgeltpunkte x aktueller Rentenwert (§ 64 SGB VI,
  Zugangs- und Rentenartfaktor = 1, d. h. Regelaltersrente ohne Abschläge).
  Weil der Rentenwert langfristig etwa den Löhnen folgt, ist das näherungsweise real.
  Die Hochrechnungen MIT 1 % / 2 % Anpassung aus der Renteninformation sind nominal
  und werden hier bewusst nicht verwendet.
- Betriebsrente (bAV): Standmitteilungen sind meist nominal (Euro des Auszahlungsjahres)
  und werden mit der Inflationsannahme auf heute abgezinst.
- Netto: Steuer und Kranken-/Pflegeversicherung werden berechnet (income_breakdown),
  je nach Krankenversicherung im Alter:
  PKV: kein KV/PV-Abzug von den Renten; dafür zahlt die DRV einen Zuschuss zur KV
  (§ 106 SGB VI: halber allgemeiner Beitragssatz + halber durchschnittlicher
  Zusatzbeitrag auf die gesetzliche Rente, höchstens die Hälfte des KV-Beitrags; keiner
  zur Pflegeversicherung). Der PKV/PPV-Beitrag wird vom verfügbaren Einkommen abgezogen.
  Betriebsrenten sind bei PKV beitragsfrei (§ 229 SGB V gilt nur für GKV-Mitglieder).
  GKV: halber KV-Satz + halber Zusatzbeitrag + voller PV-Satz auf die gesetzliche Rente;
  voller KV-Satz auf die bAV abzüglich Freibetrag (§ 226 Abs. 2 SGB V), PV mit Freigrenze.
  Einkommensteuer: Tarif 2026 (§ 32a EStG), Grundtarif (Einzelveranlagung), ohne Kirchen-
  steuer und Soli. Annahme: der Tarif wird mit der Inflation angepasst, deshalb wird er
  direkt auf reale Beträge angewendet.
- Kapitalbedarf: Rentenbarwertfaktor mit jährlicher Zahlung (Näherung für Monatsrenten).

Dieses Modul greift nicht auf die Datenbank zu; es arbeitet mit einfachen Werten,
damit es isoliert testbar ist.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from app.models import PensionContract, PensionSnapshot, PensionType, ValueBasis

# Aktueller Rentenwert (Cent) mit Gültigkeitsbeginn. Seit 1.7.2023 bundeseinheitlich,
# davor Westwert. Quelle: DRV / Rentenwertbestimmungsverordnungen (BMAS).
# Dient nur zum Vorbelegen des Felds "Rentenwert zum Stichtag"; der Nutzer kann ihn überschreiben.
PENSION_VALUE_HISTORY: list[tuple[date, int]] = [
    (date(2019, 7, 1), 3305),
    (date(2020, 7, 1), 3419),
    (date(2021, 7, 1), 3419),
    (date(2022, 7, 1), 3602),
    (date(2023, 7, 1), 3760),
    (date(2024, 7, 1), 3932),
    (date(2025, 7, 1), 4079),
    (date(2026, 7, 1), 4252),
]


def pension_value_at(d: date) -> Optional[int]:
    """Rentenwert in Cent, der am Datum d galt (None vor Beginn der Tabelle)."""
    result = None
    for valid_from, cent in PENSION_VALUE_HISTORY:
        if valid_from <= d:
            result = cent
    return result


# --------------------------------------------------------------------------- Grundformeln


def annuity_factor(rate: float, years: int) -> float:
    """Rentenbarwertfaktor (nachschüssig): Barwert einer Zahlung von 1 pro Jahr über n Jahre."""
    if years <= 0:
        return 0.0
    if abs(rate) < 1e-12:
        return float(years)
    return (1 - (1 + rate) ** -years) / rate


def deflate(cent: int, inflation: float, years: float) -> int:
    """Nominalen Betrag in 'years' Jahren auf heutige Kaufkraft abzinsen."""
    if years <= 0:
        return cent
    return round(cent / (1 + inflation) ** years)


def capital_to_monthly(capital_cent: int, rate: float, years: int) -> int:
    """Kapital in eine gleichbleibende Monatsrente über 'years' Jahre umrechnen (Verrentung)."""
    af = annuity_factor(rate, years)
    return round(capital_cent / (12 * af)) if af > 0 else 0


def future_value(present_cent: int, monthly_saving_cent: int, rate: float, years: float) -> int:
    """Endwert von Startkapital plus Sparrate (jährliche Verzinsung, Sparrate als Jahressumme)."""
    if years <= 0:
        return present_cent
    growth = (1 + rate) ** years
    annual = monthly_saving_cent * 12
    savings_fv = annual * years if abs(rate) < 1e-12 else annual * (growth - 1) / rate
    return round(present_cent * growth + savings_fv)


def years_until_retirement(birth_date: Optional[date], retirement_age: int, today: date) -> Optional[float]:
    if birth_date is None:
        return None
    try:
        start = birth_date.replace(year=birth_date.year + retirement_age)
    except ValueError:  # 29. Februar
        start = date(birth_date.year + retirement_age, 3, 1)
    return max(0.0, (start - today).days / 365.25)


# --------------------------------------------------------------------------- Einzelverträge


@dataclass(frozen=True)
class Assumptions:
    years_to_retirement: float
    inflation: float
    accumulation_real_return: float
    payout_real_return: float
    payout_years: int
    current_pension_value_cent: int


@dataclass
class PensionEstimate:
    """Monatsrente brutto in heutiger Kaufkraft (vor Steuer und KV/PV)."""

    contract_name: str
    pension_type: PensionType
    as_of: Optional[date]
    manual_net: bool = False
    net_ratio: float = 1.0
    conservative_gross_cent: int = 0
    expected_gross_cent: int = 0
    earning_points: Optional[float] = None
    warnings: list[str] = field(default_factory=list)

    def gross(self, scenario: str) -> int:
        return self.conservative_gross_cent if scenario == CONSERVATIVE else self.expected_gross_cent


CONSERVATIVE, EXPECTED = "conservative", "expected"


def _new_estimate(contract: PensionContract, snap: PensionSnapshot) -> PensionEstimate:
    return PensionEstimate(
        contract.name, contract.pension_type, snap.as_of,
        manual_net=bool(contract.manual_net),
        net_ratio=contract.net_ratio if contract.net_ratio is not None else 1.0,
    )


def estimate_statutory(contract: PensionContract, snap: PensionSnapshot, a: Assumptions) -> PensionEstimate:
    est = _new_estimate(contract, snap)
    rw_snap = snap.pension_value_cent or pension_value_at(snap.as_of)

    ep: Optional[float] = None
    if snap.projected_monthly_cent and rw_snap:
        ep = snap.projected_monthly_cent / rw_snap
    elif snap.earning_points:
        ep = snap.earning_points
        est.warnings.append("Keine Hochrechnung erfasst: nur bisher erworbene Entgeltpunkte berücksichtigt.")
    elif snap.accrued_monthly_cent and rw_snap:
        ep = snap.accrued_monthly_cent / rw_snap
        est.warnings.append("Keine Hochrechnung erfasst: nur bisher erreichte Rente berücksichtigt.")

    if ep is None:
        est.warnings.append("Keine verwertbaren Werte (Hochrechnung, Entgeltpunkte oder Rentenwert fehlen).")
        return est

    est.earning_points = ep
    monthly = round(ep * a.current_pension_value_cent)
    est.conservative_gross_cent = est.expected_gross_cent = monthly
    return est


def estimate_occupational(contract: PensionContract, snap: PensionSnapshot, a: Assumptions) -> PensionEstimate:
    est = _new_estimate(contract, snap)

    def monthly_from(monthly: Optional[int], capital: Optional[int]) -> Optional[int]:
        if monthly:
            return monthly
        if capital:
            return capital_to_monthly(capital, a.payout_real_return, a.payout_years)
        return None

    guaranteed = monthly_from(snap.guaranteed_monthly_cent, snap.guaranteed_capital_cent)
    projected = monthly_from(snap.projected_monthly_cent, snap.projected_capital_cent)

    if guaranteed is None and projected is None:
        est.warnings.append("Weder garantierte noch prognostizierte Leistung erfasst.")
        return est
    if guaranteed is None:
        guaranteed = projected
        est.warnings.append("Keine Garantie erfasst: konservatives Szenario nutzt die Prognose.")
    if projected is None:
        projected = guaranteed

    if snap.value_basis == ValueBasis.NOMINAL:
        guaranteed = deflate(guaranteed, a.inflation, a.years_to_retirement)
        projected = deflate(projected, a.inflation, a.years_to_retirement)

    if not contract.is_vested:
        est.warnings.append("Noch nicht unverfallbar (§ 1b BetrAVG): entfällt ggf. bei Jobwechsel.")

    est.conservative_gross_cent = guaranteed
    est.expected_gross_cent = projected
    return est


def estimate(contract: PensionContract, snap: PensionSnapshot, a: Assumptions) -> PensionEstimate:
    if contract.pension_type == PensionType.STATUTORY:
        return estimate_statutory(contract, snap, a)
    return estimate_occupational(contract, snap, a)


# --------------------------------------------------------------------------- Steuer, KV/PV

# Rechengrößen 2026. Jährlich prüfen.
KV_GENERAL_RATE = 0.146          # allgemeiner Beitragssatz GKV (§ 241 SGB V)
KV_AVG_ADDITIONAL_RATE = 0.029   # durchschnittlicher Zusatzbeitrag 2026 (BMG)
CARE_RATE = 0.036                # Pflegeversicherung mit Kind; kinderlos 4,2 %
BAV_KV_ALLOWANCE_CENT = 197_75   # Freibetrag/Freigrenze bAV 2026 = 1/20 der Bezugsgröße (3.955 €)
PENSION_ALLOWANCE_EUR = 102      # Werbungskosten-Pauschbetrag für Renten (§ 9a Satz 1 Nr. 3 EStG)
SPECIAL_EXPENSES_EUR = 36        # Sonderausgaben-Pauschbetrag (§ 10c EStG)


def taxable_share(retirement_year: int) -> float:
    """Besteuerungsanteil der gesetzlichen Rente nach Jahr des Rentenbeginns (§ 22 Nr. 1 EStG).

    Bis 2020 +2 Prozentpunkte/Jahr ab 50 % (2005), 2021/2022 +1, ab 2023 +0,5
    (Wachstumschancengesetz), 100 % ab 2058.
    """
    if retirement_year <= 2005:
        return 0.50
    if retirement_year <= 2020:
        return 0.50 + 0.02 * (retirement_year - 2005)
    if retirement_year <= 2022:
        return 0.80 + 0.01 * (retirement_year - 2020)
    return min(1.0, 0.825 + 0.005 * (retirement_year - 2023))


def income_tax_2026(taxable_income_eur: float) -> int:
    """Einkommensteuer nach Grundtarif § 32a Abs. 1 EStG (Veranlagungszeitraum 2026), in Euro."""
    x = int(taxable_income_eur)  # auf volle Euro abrunden
    if x <= 12_348:
        return 0
    if x <= 17_799:
        y = (x - 12_348) / 10_000
        return int((914.51 * y + 1_400) * y)
    if x <= 69_878:
        z = (x - 17_799) / 10_000
        return int((173.10 * z + 2_397) * z + 1_034.87)
    if x <= 277_825:
        return int(0.42 * x - 11_135.63)
    return int(0.45 * x - 19_470.38)


@dataclass(frozen=True)
class PkvToday:
    """Heutige PKV-Beiträge pro Monat (Gesamtbeitrag) und Beitragsentlastung laut Vertrag."""

    total_cent: int                 # Krankenversicherung gesamt, alle Bausteine
    sick_pay_cent: int = 0          # davon Krankentagegeld (entfällt im Ruhestand)
    surcharge_cent: int = 0         # davon gesetzlicher 10-%-Zuschlag (entfällt ab 60, § 149 VAG)
    relief_contribution_cent: int = 0  # davon Beitrag zum Beitragsentlastungstarif (endet mit Rentenbeginn)
    relief_benefit_cent: int = 0    # Beitragsentlastung pro Monat ab Rentenbeginn, nominal
    care_cent: int = 0              # private Pflegepflichtversicherung
    real_increase: float = 0.015    # reale Beitragssteigerung p. a.


@dataclass(frozen=True)
class PkvProjection:
    """PKV-Beiträge im Ruhestand pro Monat in heutiger Kaufkraft, mit Zwischenschritten."""

    base_cent: int          # heutiger Beitrag ohne entfallende Bestandteile
    grown_cent: int         # ... nach realer Steigerung bis Rentenbeginn
    relief_real_cent: int   # Beitragsentlastung in heutiger Kaufkraft
    health_cent: int        # KV-Beitrag im Ruhestand (nach Entlastung, >= 0)
    care_cent: int          # PPV im Ruhestand


def project_pkv(p: PkvToday, years: float, inflation: float) -> PkvProjection:
    """Rechnet den PKV-Beitrag im Ruhestand aus dem heutigen Beitrag hoch.

    - Krankentagegeld, 10-%-Zuschlag und Beitrag zur Beitragsentlastung fallen weg.
    - Der Rest steigt real um p.real_increase pro Jahr (Medizininflation über der
      allgemeinen Inflation; alterungsbedingte Kosten deckt die Alterungsrückstellung).
    - Die Beitragsentlastung ist ein nominal fester Betrag; sie verliert bis zum
      Rentenbeginn an Kaufkraft und wird deshalb abgezinst.
    Die Wirkung des angesparten 10-%-Zuschlags (Beitragsdämpfung ab 65) steckt nur
    implizit in der Steigerungsannahme.
    """
    years = max(0.0, years)
    base = max(0, p.total_cent - p.sick_pay_cent - p.surcharge_cent - p.relief_contribution_cent)
    growth = (1 + p.real_increase) ** years
    grown = round(base * growth)
    relief_real = deflate(p.relief_benefit_cent, inflation, years)
    return PkvProjection(
        base_cent=base,
        grown_cent=grown,
        relief_real_cent=relief_real,
        health_cent=max(0, grown - relief_real),
        care_cent=round(p.care_cent * growth),
    )


@dataclass(frozen=True)
class HealthTaxSettings:
    health_insurance: str             # "GKV" oder "PKV"
    retirement_year: int
    pkv_health_monthly_cent: int = 0
    pkv_care_monthly_cent: int = 0
    pkv_basic_share: float = 0.8


@dataclass(frozen=True)
class IncomeBreakdown:
    """Vom Brutto zum verfügbaren Einkommen, pro Monat, heutige Kaufkraft, in Cent."""

    gross_statutory_cent: int
    gross_occupational_cent: int
    manual_net_cent: int         # Verträge mit manueller Netto-Quote (bereits netto)
    kv_subsidy_cent: int         # + Zuschuss der DRV zur PKV
    health_cent: int             # - KV/PV (GKV-Abzüge oder PKV/PPV-Beitrag)
    income_tax_cent: int         # - Einkommensteuer
    taxable_share: float

    @property
    def gross_cent(self) -> int:
        return self.gross_statutory_cent + self.gross_occupational_cent

    @property
    def available_cent(self) -> int:
        return (
            self.gross_cent + self.manual_net_cent + self.kv_subsidy_cent
            - self.health_cent - self.income_tax_cent
        )


def income_breakdown(estimates: list[PensionEstimate], scenario: str, h: HealthTaxSettings) -> IncomeBreakdown:
    grv = sum(e.gross(scenario) for e in estimates if not e.manual_net and e.pension_type == PensionType.STATUTORY)
    bav = sum(e.gross(scenario) for e in estimates if not e.manual_net and e.pension_type == PensionType.OCCUPATIONAL)
    manual = sum(round(e.gross(scenario) * e.net_ratio) for e in estimates if e.manual_net)

    if h.health_insurance == "PKV":
        subsidy = min(round(grv * (KV_GENERAL_RATE + KV_AVG_ADDITIONAL_RATE) / 2), h.pkv_health_monthly_cent // 2)
        health = h.pkv_health_monthly_cent + h.pkv_care_monthly_cent
        # Absetzbar: Basisanteil KV + PPV, gekürzt um den steuerfreien Zuschuss (§ 3 Nr. 14 EStG).
        deductible = max(0, round(h.pkv_health_monthly_cent * h.pkv_basic_share) + h.pkv_care_monthly_cent - subsidy)
    else:
        subsidy = 0
        kv_grv = grv * (KV_GENERAL_RATE + KV_AVG_ADDITIONAL_RATE) / 2
        kv_bav = max(0, bav - BAV_KV_ALLOWANCE_CENT) * (KV_GENERAL_RATE + KV_AVG_ADDITIONAL_RATE)
        care = (grv + (bav if bav > BAV_KV_ALLOWANCE_CENT else 0)) * CARE_RATE
        health = round(kv_grv + kv_bav + care)
        deductible = health

    share = taxable_share(h.retirement_year)
    annual_eur = (grv * share + bav) * 12 / 100
    if grv + bav > 0:
        annual_eur -= PENSION_ALLOWANCE_EUR
    annual_eur -= SPECIAL_EXPENSES_EUR + deductible * 12 / 100
    tax = round(income_tax_2026(max(0.0, annual_eur)) * 100 / 12)

    return IncomeBreakdown(grv, bav, manual, subsidy, health, tax, share)


# --------------------------------------------------------------------------- Lückenanalyse


@dataclass(frozen=True)
class Scenario:
    income: IncomeBreakdown
    gap_monthly_cent: int          # Wunscheinkommen minus verfügbares Renteneinkommen (>= 0)
    capital_need_cent: int         # Kapital zum Rentenbeginn, das die Lücke schließt (real)
    coverage: Optional[float]      # voraussichtliches Vermögen / Kapitalbedarf


@dataclass(frozen=True)
class GapAnalysis:
    conservative: Scenario
    expected: Scenario
    projected_assets_cent: int             # ruhestandsrelevantes Vermögen zum Rentenbeginn (real)
    claims_value_at_retirement_cent: int   # Barwert der erwarteten Renten zum Rentenbeginn
    claims_value_today_cent: int           # ... auf heute abgezinst (nur Info, kein Vermögen!)


def gap_analysis(
    estimates: list[PensionEstimate],
    desired_income_cent: int,
    retirement_assets_cent: int,
    monthly_savings_cent: int,
    a: Assumptions,
    h: HealthTaxSettings,
) -> GapAnalysis:
    """Wunscheinkommen = verfügbares Einkommen nach Steuer und Kranken-/Pflegeversicherung."""
    af = annuity_factor(a.payout_real_return, a.payout_years)
    fv = future_value(
        retirement_assets_cent, monthly_savings_cent, a.accumulation_real_return, a.years_to_retirement
    )

    def scenario(name: str) -> Scenario:
        income = income_breakdown(estimates, name, h)
        gap = max(0, desired_income_cent - income.available_cent)
        need = round(gap * 12 * af)
        return Scenario(income, gap, need, (fv / need) if need > 0 else None)

    cons, exp = scenario(CONSERVATIVE), scenario(EXPECTED)
    value_at_ret = round(max(0, exp.income.available_cent) * 12 * af)
    value_today = round(value_at_ret / (1 + a.accumulation_real_return) ** a.years_to_retirement)
    return GapAnalysis(cons, exp, fv, value_at_ret, value_today)
