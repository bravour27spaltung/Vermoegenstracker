"""Rentenansprüche: gesetzliche Rente und Betriebsrente.

Getrennt von den Positionen, weil Anwartschaften kein verfügbares Vermögen sind
(siehe Kommentar in app/models.py). Pro Vertrag wird einmal im Jahr der Stand aus der
Renteninformation bzw. Standmitteilung erfasst; die Übersicht rechnet alles auf
heutige Kaufkraft um, zieht Steuer und Kranken-/Pflegeversicherung ab (PKV oder GKV)
und zeigt die Rentenlücke.

Bedienung: Eingabefehler werden im Formular angezeigt (keine Fehlerseite), Beträge
dürfen als '1.631,60', '1631,60' oder '1631.60' eingegeben werden, und das
Stichtagsformular zeigt beim Tippen eine Vorschau des Ergebnisses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.depot import forecast as depot_forecast
from app.deps import get_session
from app.models import (
    DepotSettings,
    HealthInsurance,
    PensionContract,
    PkvEstimateMode,
    PensionSettings,
    PensionSnapshot,
    PensionType,
    Profile,
    ValueBasis,
)
from app.money import format_euro, parse_flexible_amount
from app.pension import (
    CONSERVATIVE,
    EXPECTED,
    Assumptions,
    HealthTaxSettings,
    IncomeBreakdown,
    PkvProjection,
    PkvToday,
    annuity_factor,
    estimate,
    future_value,
    gap_analysis,
    pension_value_at,
    project_pkv,
    years_until_retirement,
)
from app.queries import active_pensions_with_latest, retirement_assets_cent
from app.retirement import drawdown as asset_drawdown
from app.wealth import wealth

router = APIRouter(prefix="/pensions", tags=["pensions"])
templates = Jinja2Templates(directory="app/templates")

VEHICLES = ["Direktversicherung", "Pensionskasse", "Pensionsfonds", "Direktzusage", "Unterstützungskasse"]
STATUTORY_NAME = "Deutsche Rentenversicherung"

# Felder des Stichtagsformulars: (Formularname, Modellattribut, Art)
STATUTORY_FIELDS = [
    ("projected_monthly", "projected_monthly_cent", "money"),
    ("accrued_monthly", "accrued_monthly_cent", "money"),
    ("earning_points", "earning_points", "number"),
    ("pension_value", "pension_value_cent", "money"),
]
OCCUPATIONAL_FIELDS = [
    ("guaranteed_monthly", "guaranteed_monthly_cent", "money"),
    ("projected_monthly", "projected_monthly_cent", "money"),
    ("guaranteed_capital", "guaranteed_capital_cent", "money"),
    ("projected_capital", "projected_capital_cent", "money"),
    ("current_value", "current_value_cent", "money"),
    ("employee_contribution_pa", "employee_contribution_pa_cent", "money"),
    ("employer_contribution_pa", "employer_contribution_pa_cent", "money"),
]
MONTHLY_FIELDS = {"guaranteed_monthly", "projected_monthly"}
CAPITAL_FIELDS = {"guaranteed_capital", "projected_capital"}


# --------------------------------------------------------------------------- Eingabe


@dataclass
class FormInput:
    """Sammelt geparste Werte und Fehlermeldungen je Feld, damit das Formular mit den
    eingegebenen Werten und Hinweisen neu angezeigt werden kann."""

    raw: dict[str, str]
    errors: dict[str, str] = field(default_factory=dict)

    def text(self, name: str) -> str:
        return (self.raw.get(name) or "").strip()

    def money(self, name: str) -> int | None:
        s = self.text(name)
        if not s:
            return None
        try:
            cent = parse_flexible_amount(s)
        except (ValueError, ArithmeticError):
            self.errors[name] = "Bitte einen Betrag eingeben, z. B. 1.234,56"
            return None
        if cent < 0:
            self.errors[name] = "Der Betrag darf nicht negativ sein."
            return None
        return cent

    def number(self, name: str) -> float | None:
        s = self.text(name).replace("%", "").replace(" ", "")
        if not s:
            return None
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        try:
            return float(s)
        except ValueError:
            self.errors[name] = "Bitte eine Zahl eingeben, z. B. 12,5"
            return None

    def percent(self, name: str, lo: float = -50, hi: float = 100) -> float | None:
        value = self.number(name)
        if value is None:
            return None
        if not lo <= value <= hi:
            self.errors[name] = f"Bitte einen Wert zwischen {lo:g} und {hi:g} % eingeben."
            return None
        return value / 100

    def integer(self, name: str, lo: int, hi: int) -> int | None:
        value = self.number(name)
        if value is None:
            return None
        if value != int(value) or not lo <= value <= hi:
            self.errors[name] = f"Bitte eine ganze Zahl zwischen {lo} und {hi} eingeben."
            return None
        return int(value)

    def iso_date(self, name: str) -> date | None:
        s = self.text(name)
        if not s:
            return None
        try:
            return date.fromisoformat(s)
        except ValueError:
            self.errors[name] = "Bitte ein gültiges Datum eingeben."
            return None


async def _form_input(request: Request) -> FormInput:
    form = await request.form()
    return FormInput({k: v for k, v in form.items() if isinstance(v, str)})


def _money_input(cent: int | None) -> str:
    """Betrag fürs Eingabefeld, ohne €-Zeichen (das steht neben dem Feld)."""
    return "" if cent is None else format_euro(cent).removesuffix(" €")


def _num_input(value: float | None, digits: int = 4) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}".rstrip("0").rstrip(".").replace(".", ",")


def _pct_input(ratio: float) -> str:
    return _num_input(ratio * 100, 2)


def _pct(ratio: float, digits: int = 1) -> str:
    return f"{ratio * 100:.{digits}f}".replace(".", ",") + " %"


# --------------------------------------------------------------------------- Laden/Berechnen


def _get_singletons(session: Session) -> tuple[Profile, PensionSettings]:
    profile = session.get(Profile, 1)
    settings = session.get(PensionSettings, 1)
    if profile is None or settings is None:
        if profile is None:
            profile = Profile(id=1)
            session.add(profile)
        if settings is None:
            settings = PensionSettings(id=1)
            session.add(settings)
        session.commit()  # Standardwerte werden beim INSERT gesetzt
    return profile, settings


def _pkv_today(settings: PensionSettings) -> PkvToday:
    return PkvToday(
        total_cent=settings.pkv_today_total_cent,
        sick_pay_cent=settings.pkv_today_sick_pay_cent,
        surcharge_cent=settings.pkv_today_surcharge_cent,
        relief_contribution_cent=settings.pkv_today_relief_contribution_cent,
        relief_benefit_cent=settings.pkv_relief_benefit_cent,
        care_cent=settings.pkv_today_care_cent,
        real_increase=settings.pkv_real_increase,
    )


def _assumptions(
    profile: Profile, settings: PensionSettings
) -> tuple[Assumptions, HealthTaxSettings, float | None, PkvProjection | None]:
    today = date.today()
    years = years_until_retirement(profile.birth_date, profile.retirement_age, today)
    a = Assumptions(
        years_to_retirement=years or 0.0,
        inflation=profile.inflation_assumption,
        accumulation_real_return=profile.expected_real_return,
        payout_real_return=settings.payout_real_return,
        payout_years=settings.payout_years,
        current_pension_value_cent=settings.current_pension_value_cent,
    )
    retirement_year = (
        profile.birth_date.year + profile.retirement_age if profile.birth_date else today.year
    )
    projection = None
    if settings.pkv_mode == PkvEstimateMode.PROJECTED:
        projection = project_pkv(_pkv_today(settings), a.years_to_retirement, a.inflation)
        health, care = projection.health_cent, projection.care_cent
    else:
        health, care = settings.pkv_health_monthly_cent, settings.pkv_care_monthly_cent
    h = HealthTaxSettings(
        health_insurance=settings.health_insurance.value,
        retirement_year=retirement_year,
        pkv_health_monthly_cent=health,
        pkv_care_monthly_cent=care,
        pkv_basic_share=settings.pkv_basic_share,
    )
    return a, h, years, projection


def _pkv_configured(settings: PensionSettings) -> bool:
    if settings.health_insurance == HealthInsurance.GKV:
        return True
    if settings.pkv_mode == PkvEstimateMode.PROJECTED:
        return settings.pkv_today_total_cent > 0
    return settings.pkv_health_monthly_cent > 0


def _get_contract(session: Session, contract_id: int) -> PensionContract:
    contract = session.get(PensionContract, contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="Rentenvertrag nicht gefunden")
    return contract


def _statutory_contract(session: Session) -> PensionContract | None:
    return session.execute(
        select(PensionContract).where(
            PensionContract.pension_type == PensionType.STATUTORY,
            PensionContract.is_active.is_(True),
        )
    ).scalars().first()


# --------------------------------------------------------------------------- Übersicht


@router.get("")
def overview(request: Request, session: Session = Depends(get_session)):
    profile, settings = _get_singletons(session)
    a, h, years, pkv_projection = _assumptions(profile, settings)

    rows, estimates = [], []
    has_statutory_snapshot = has_occupational = False
    for contract, snap in active_pensions_with_latest(session):
        est = estimate(contract, snap, a) if snap else None
        if est:
            estimates.append(est)
        is_statutory = contract.pension_type == PensionType.STATUTORY
        has_statutory_snapshot |= is_statutory and snap is not None
        has_occupational |= not is_statutory
        rows.append(
            {
                "contract": contract,
                "type_label": "Gesetzlich" if is_statutory else "Betrieblich",
                "as_of": snap.as_of.strftime("%d.%m.%Y") if snap else None,
                "stale": bool(snap and (date.today() - snap.as_of).days > 400),
                "ep": _num_input(est.earning_points, 2) if est and est.earning_points else "",
                "cons": format_euro(est.conservative_gross_cent) if est else "–",
                "exp": format_euro(est.expected_gross_cent) if est else "–",
                "same": bool(est and est.conservative_gross_cent == est.expected_gross_cent),
                "warnings": est.warnings if est else [],
            }
        )

    analysis = gap_analysis(
        estimates,
        profile.desired_income_monthly_cent,
        retirement_assets_cent(session),
        profile.planned_savings_monthly_cent,
        a,
        h,
    )

    def income_rows(c: IncomeBreakdown, e: IncomeBreakdown) -> list[dict]:
        rows_ = [
            ("Gesetzliche Rente brutto", c.gross_statutory_cent, e.gross_statutory_cent, ""),
            ("Betriebsrenten brutto", c.gross_occupational_cent, e.gross_occupational_cent, ""),
        ]
        if c.manual_net_cent or e.manual_net_cent:
            rows_.append(("Verträge mit manueller Netto-Quote", c.manual_net_cent, e.manual_net_cent, ""))
        if h.health_insurance == "PKV":
            rows_.append(("+ Zuschuss der DRV zur PKV", c.kv_subsidy_cent, e.kv_subsidy_cent,
                          "§ 106 SGB VI: 8,75 % der gesetzlichen Rente, max. halber PKV-Beitrag"))
            hint = "direkt geschätzt"
            if pkv_projection is not None:
                hint = "hochgerechnet aus heutigem Beitrag"
                if pkv_projection.relief_real_cent:
                    hint += f", nach Beitragsentlastung von {format_euro(pkv_projection.relief_real_cent)}"
            rows_.append(("− PKV- und Pflegebeitrag", c.health_cent, e.health_cent, hint))
        else:
            rows_.append(("− Kranken- und Pflegeversicherung", c.health_cent, e.health_cent, "Beitragssätze 2026"))
        rows_.append(("− Einkommensteuer", c.income_tax_cent, e.income_tax_cent,
                      f"Tarif 2026, Besteuerungsanteil gesetzl. Rente {_pct(c.taxable_share)}"))
        return [
            {"label": label, "cons": format_euro(cv), "exp": format_euro(ev), "hint": hint}
            for label, cv, ev, hint in rows_
        ]

    cons, exp = analysis.conservative, analysis.expected
    drawdown = _asset_drawdown(session, profile, settings, a, cons.gap_monthly_cent, exp.gap_monthly_cent)

    def coverage(s):
        if drawdown is not None:
            s = drawdown["cons" if s is cons else "exp"]
            return "–" if s["coverage"] is None else f"{s['coverage'] * 100:.0f} %"
        return "–" if s.coverage is None else f"{s.coverage * 100:.0f} %"

    def coverage_ok(s):
        if drawdown is not None:
            c = drawdown["cons" if s is cons else "exp"]["coverage"]
            return c is None or c >= 1
        return s.coverage is None or s.coverage >= 1

    steps = [
        {"done": bool(profile.birth_date and profile.desired_income_monthly_cent),
         "label": "Geburtsdatum und Wunscheinkommen eintragen", "href": "/pensions/settings"},
        {"done": _pkv_configured(settings),
         "label": "Heutigen PKV-Beitrag und Beitragsentlastung eintragen", "href": "/pensions/settings#kv"},
        {"done": has_statutory_snapshot,
         "label": "Renteninformation der gesetzlichen Rente erfassen", "href": None},
        {"done": has_occupational, "optional": True,
         "label": "Betriebsrente erfassen (falls vorhanden)", "href": "/pensions/new-occupational"},
    ]
    statutory = _statutory_contract(session)

    return templates.TemplateResponse(
        request,
        "pensions.html",
        {
            "rows": rows,
            "steps": steps,
            "setup_incomplete": not all(s["done"] for s in steps if not s.get("optional")),
            "statutory": statutory,
            "years": None if years is None else f"{years:.0f}",
            "retirement_year": h.retirement_year,
            "health": h.health_insurance,
            "desired": format_euro(profile.desired_income_monthly_cent),
            "has_desired": profile.desired_income_monthly_cent > 0,
            "income_rows": income_rows(cons.income, exp.income),
            "available": {"cons": format_euro(cons.income.available_cent), "exp": format_euro(exp.income.available_cent)},
            "gap": {"cons": format_euro(cons.gap_monthly_cent), "exp": format_euro(exp.gap_monthly_cent),
                    "exp_zero": exp.gap_monthly_cent == 0},
            "need": {"cons": format_euro(cons.capital_need_cent), "exp": format_euro(exp.capital_need_cent)},
            "coverage": {"cons": coverage(cons), "exp": coverage(exp),
                         "cons_ok": coverage_ok(cons), "exp_ok": coverage_ok(exp)},
            "drawdown": drawdown,
            "projected_assets": format_euro(analysis.projected_assets_cent),
            "claims_at_ret": format_euro(analysis.claims_value_at_retirement_cent),
            "claims_today": format_euro(analysis.claims_value_today_cent),
        },
    )


# --------------------------------------------------------------------------- Entnahme aus Vermögen


def _asset_drawdown(session: Session, profile: Profile, settings: PensionSettings, a: Assumptions,
                    gap_cons_cent: int, gap_exp_cent: int) -> dict | None:
    """Monatliche Entnahme aus Depots und übrigem ruhestandsrelevanten Vermögen ab Rentenbeginn.

    Depots: Monte-Carlo aus Allokation und Rendite-Annahmen der Depotseite (konservativ = P10,
    erwartet = Median). Übrige Positionen: deterministisch mit der Renditeannahme des Profils.
    Entnahme als Kapitalverzehr über die Bezugsdauer (wie der Kapitalbedarf), Depotanteil nach
    Abgeltungsteuer auf den Gewinnanteil. Verknüpfte Positionen werden nicht doppelt gezählt.
    """
    if profile.birth_date is None:
        return None
    w = wealth(session)
    years = max(0, round(a.years_to_retirement))
    d = w.depots
    depot_savings = d.savings_monthly_cent
    other_savings = max(profile.planned_savings_monthly_cent - depot_savings, 0)
    paths = (session.get(DepotSettings, 1).mc_paths if session.get(DepotSettings, 1) else 1000) or 1000
    fc = depot_forecast(d.value_cent / 100, depot_savings / 100, d.mu, d.sigma, years, min(paths, 2000))
    basis = d.basis_cent / 100 + depot_savings / 100 * 12 * years
    other_fv = future_value(w.other_cent, other_savings, profile.expected_real_return, a.years_to_retirement) / 100
    af = annuity_factor(a.payout_real_return, a.payout_years)
    other_monthly = other_fv / af / 12 if af > 0 else 0.0
    out = {}
    for key, depot_cap, gap in (("cons", fc.p10[-1], gap_cons_cent), ("exp", fc.p50[-1], gap_exp_cent)):
        dd = asset_drawdown(depot_cap, basis, a.payout_real_return, a.payout_years, d.tax)
        net_cent = round((dd.net_monthly_eur + other_monthly) * 100)
        out[key] = {
            "capital": format_euro(round((depot_cap + other_fv) * 100)),
            "depot_capital": format_euro(round(depot_cap * 100)),
            "net": format_euro(net_cent),
            "tax": format_euro(round(dd.tax_monthly_eur * 100)),
            "remaining": format_euro(max(gap - net_cent, 0)),
            "surplus": format_euro(max(net_cent - gap, 0)) if net_cent > gap else None,
            "coverage": (net_cent / gap) if gap > 0 else None,
        }
    out.update(
        depot_today=format_euro(d.value_cent), other_today=format_euro(w.other_cent),
        depot_savings=format_euro(depot_savings), other_savings=format_euro(other_savings),
        other_return=_pct(profile.expected_real_return), linked=w.linked_position_names,
        other_names=[o.name for o in w.other], median_return=_pct(pow(2.718281828459045, d.mu) - 1),
        payout_years=a.payout_years, payout_return=_pct(a.payout_real_return),
    )
    return out


# --------------------------------------------------------------------------- Annahmen


def _settings_values(profile: Profile, settings: PensionSettings) -> dict[str, str]:
    return {
        "birth_date": profile.birth_date.isoformat() if profile.birth_date else "",
        "retirement_age": str(profile.retirement_age),
        "desired": _money_input(profile.desired_income_monthly_cent or None),
        "savings": _money_input(profile.planned_savings_monthly_cent or None),
        "health_insurance": settings.health_insurance.value,
        "pkv_mode": settings.pkv_mode.value,
        "pkv_today_total": _money_input(settings.pkv_today_total_cent or None),
        "pkv_today_sick_pay": _money_input(settings.pkv_today_sick_pay_cent or None),
        "pkv_today_surcharge": _money_input(settings.pkv_today_surcharge_cent or None),
        "pkv_today_relief_contribution": _money_input(settings.pkv_today_relief_contribution_cent or None),
        "pkv_relief_benefit": _money_input(settings.pkv_relief_benefit_cent or None),
        "pkv_today_care": _money_input(settings.pkv_today_care_cent or None),
        "pkv_real_increase": _pct_input(settings.pkv_real_increase),
        "pkv_health": _money_input(settings.pkv_health_monthly_cent or None),
        "pkv_care": _money_input(settings.pkv_care_monthly_cent or None),
        "pkv_basic_share": _pct_input(settings.pkv_basic_share),
        "inflation": _pct_input(profile.inflation_assumption),
        "real_return": _pct_input(profile.expected_real_return),
        "payout_return": _pct_input(settings.payout_real_return),
        "payout_years": str(settings.payout_years),
        "pension_value": _money_input(settings.current_pension_value_cent),
    }


def _render_settings(request: Request, values: dict, errors: dict, status_code: int = 200):
    return templates.TemplateResponse(
        request, "pension_settings.html", {"v": values, "errors": errors}, status_code=status_code
    )


@router.get("/settings")
def settings_form(request: Request, session: Session = Depends(get_session)):
    profile, settings = _get_singletons(session)
    return _render_settings(request, _settings_values(profile, settings), {})


PKV_TODAY_FIELDS = [
    "pkv_today_total", "pkv_today_sick_pay", "pkv_today_surcharge",
    "pkv_today_relief_contribution", "pkv_relief_benefit", "pkv_today_care",
]


def _parse_settings(f: FormInput, settings: PensionSettings) -> dict:
    """Liest das Annahmen-Formular. Fehler landen in f.errors; None heißt 'nicht angegeben'."""
    d: dict = {}
    d["birth_date"] = f.iso_date("birth_date")
    if d["birth_date"] and not date(1930, 1, 1) <= d["birth_date"] <= date.today():
        f.errors["birth_date"] = "Bitte ein plausibles Geburtsdatum eingeben."
    d["retirement_age"] = f.integer("retirement_age", 50, 75)
    d["desired"], d["savings"] = f.money("desired"), f.money("savings")
    d["health"] = f.text("health_insurance") or settings.health_insurance.value
    if d["health"] not in ("GKV", "PKV"):
        f.errors["health_insurance"] = "Bitte GKV oder PKV wählen."
    d["pkv_mode"] = f.text("pkv_mode") or settings.pkv_mode.value
    if d["pkv_mode"] not in PkvEstimateMode.__members__:
        d["pkv_mode"] = PkvEstimateMode.PROJECTED.value
    for name in PKV_TODAY_FIELDS:
        d[name] = f.money(name)
    parts = sum(d[n] or 0 for n in ("pkv_today_sick_pay", "pkv_today_surcharge", "pkv_today_relief_contribution"))
    if d["pkv_mode"] == "PROJECTED" and d["health"] == "PKV" and parts > (d["pkv_today_total"] or 0):
        f.errors["pkv_today_total"] = (
            "Der Gesamtbeitrag ist kleiner als die Summe aus Krankentagegeld, Zuschlag und "
            "Beitragsentlastung. Bitte den Gesamtbeitrag laut Rechnung eintragen."
        )
    d["pkv_real_increase"] = f.percent("pkv_real_increase", -5, 10)
    d["pkv_health"], d["pkv_care"] = f.money("pkv_health"), f.money("pkv_care")
    d["basic_share"] = f.percent("pkv_basic_share", 0, 100)
    d["inflation"] = f.percent("inflation", -5, 20)
    d["real_return"] = f.percent("real_return", -10, 20)
    d["payout_return"] = f.percent("payout_return", -10, 20)
    d["payout_years"] = f.integer("payout_years", 1, 50)
    d["pension_value"] = f.money("pension_value")
    return d


@router.post("/settings")
async def save_settings(request: Request, session: Session = Depends(get_session)):
    profile, settings = _get_singletons(session)
    f = await _form_input(request)
    d = _parse_settings(f, settings)
    if f.errors:
        return _render_settings(request, f.raw, f.errors, status_code=422)

    profile.birth_date = d["birth_date"]
    profile.retirement_age = d["retirement_age"] or profile.retirement_age
    profile.desired_income_monthly_cent = d["desired"] or 0
    profile.planned_savings_monthly_cent = d["savings"] or 0
    settings.health_insurance = HealthInsurance(d["health"])
    settings.pkv_mode = PkvEstimateMode(d["pkv_mode"])
    settings.pkv_today_total_cent = d["pkv_today_total"] or 0
    settings.pkv_today_sick_pay_cent = d["pkv_today_sick_pay"] or 0
    settings.pkv_today_surcharge_cent = d["pkv_today_surcharge"] or 0
    settings.pkv_today_relief_contribution_cent = d["pkv_today_relief_contribution"] or 0
    settings.pkv_relief_benefit_cent = d["pkv_relief_benefit"] or 0
    settings.pkv_today_care_cent = d["pkv_today_care"] or 0
    settings.pkv_health_monthly_cent = d["pkv_health"] or 0
    settings.pkv_care_monthly_cent = d["pkv_care"] or 0
    for key, obj, attr in [
        ("pkv_real_increase", settings, "pkv_real_increase"),
        ("basic_share", settings, "pkv_basic_share"),
        ("inflation", profile, "inflation_assumption"),
        ("real_return", profile, "expected_real_return"),
        ("payout_return", settings, "payout_real_return"),
        ("payout_years", settings, "payout_years"),
    ]:
        if d[key] is not None:
            setattr(obj, attr, d[key])
    if d["pension_value"]:
        settings.current_pension_value_cent = d["pension_value"]
    session.commit()
    return RedirectResponse(url="/pensions", status_code=303)


@router.post("/settings/pkv-preview", response_class=HTMLResponse)
async def pkv_preview(request: Request, session: Session = Depends(get_session)):
    """Live-Vorschau der PKV-Hochrechnung auf der Annahmen-Seite (HTML-Fragment)."""
    profile, settings = _get_singletons(session)
    f = await _form_input(request)
    d = _parse_settings(f, settings)
    relevant = {k for k in f.errors if k.startswith("pkv_") or k in ("birth_date", "retirement_age", "inflation")}
    if relevant:
        return "<p>Vorschau erscheint, sobald die Eingaben gültig sind.</p>"
    if not d["pkv_today_total"]:
        return "<p>Trag deinen heutigen Gesamtbeitrag ein, dann erscheint hier die Hochrechnung.</p>"

    birth = d["birth_date"] or profile.birth_date
    age = d["retirement_age"] or profile.retirement_age
    years = years_until_retirement(birth, age, date.today())
    inflation = d["inflation"] if d["inflation"] is not None else profile.inflation_assumption
    increase = d["pkv_real_increase"] if d["pkv_real_increase"] is not None else settings.pkv_real_increase
    p = project_pkv(
        PkvToday(
            total_cent=d["pkv_today_total"], sick_pay_cent=d["pkv_today_sick_pay"] or 0,
            surcharge_cent=d["pkv_today_surcharge"] or 0,
            relief_contribution_cent=d["pkv_today_relief_contribution"] or 0,
            relief_benefit_cent=d["pkv_relief_benefit"] or 0, care_cent=d["pkv_today_care"] or 0,
            real_increase=increase,
        ),
        years or 0.0, inflation,
    )
    y = "?" if years is None else f"{years:.0f}"
    rows = [
        ("Heutiger Beitrag ohne entfallende Bestandteile", format_euro(p.base_cent)),
        (f"× reale Steigerung {_pct(increase)} über {y} Jahre", format_euro(p.grown_cent)),
    ]
    if d["pkv_relief_benefit"]:
        rows.append((f"− Beitragsentlastung {format_euro(d['pkv_relief_benefit'])}, "
                     f"in heutiger Kaufkraft ({_pct(inflation)} Inflation)", format_euro(p.relief_real_cent)))
    rows.append(("<strong>= Krankenversicherung im Ruhestand</strong>", f"<strong>{format_euro(p.health_cent)}</strong>"))
    if d["pkv_today_care"]:
        rows.append(("+ Pflegepflichtversicherung im Ruhestand", format_euro(p.care_cent)))
    html = "<table>" + "".join(f'<tr><td>{a}</td><td class="right">{b}</td></tr>' for a, b in rows) + "</table>"
    html += "<p><small>Pro Monat in heutiger Kaufkraft. Den Zuschuss der Rentenversicherung rechnet die Übersicht an.</small></p>"
    if years is None:
        html += '<p><small class="warn">Geburtsdatum fehlt: ohne Hochrechnung über die Zeit.</small></p>'
    if p.relief_real_cent > p.grown_cent:
        html += '<p><small class="warn">Die Entlastung ist größer als der Beitrag. Bitte die Werte prüfen.</small></p>'
    return html


# --------------------------------------------------------------------------- Verträge


@router.post("/statutory")
def create_statutory(session: Session = Depends(get_session)):
    """Ein Klick: gesetzliche Rente anlegen (oder die vorhandene öffnen)."""
    contract = _statutory_contract(session)
    if contract is None:
        contract = PensionContract(name=STATUTORY_NAME, pension_type=PensionType.STATUTORY, is_vested=True)
        existing_name = session.execute(
            select(PensionContract).where(PensionContract.name == STATUTORY_NAME)
        ).scalar_one_or_none()
        if existing_name is not None:  # früher ausgeblendet: wieder aktivieren
            contract = existing_name
            contract.is_active = True
        else:
            session.add(contract)
        session.commit()
    return RedirectResponse(url=f"/pensions/{contract.id}/snapshots/new", status_code=303)


def _render_contract_form(request: Request, values: dict, errors: dict, contract=None, status_code=200):
    return templates.TemplateResponse(
        request,
        "pension_contract_form.html",
        {"v": values, "errors": errors, "contract": contract, "vehicles": VEHICLES},
        status_code=status_code,
    )


@router.get("/new-occupational")
def new_occupational_form(request: Request):
    return _render_contract_form(
        request, {"name": "", "vehicle": VEHICLES[0], "is_vested": "on", "manual_net": "", "net_ratio": "70"}, {}
    )


def _apply_contract_form(f: FormInput, session: Session, contract: PensionContract | None) -> dict:
    name = f.text("name")
    if not name:
        f.errors["name"] = "Bitte einen Namen eingeben, z. B. „Direktversicherung Firma X“."
    else:
        clash = session.execute(select(PensionContract).where(PensionContract.name == name)).scalar_one_or_none()
        if clash is not None and clash is not contract:
            f.errors["name"] = "Diesen Namen gibt es schon."
    vehicle = f.text("vehicle")
    if vehicle and vehicle not in VEHICLES:
        f.errors["vehicle"] = "Bitte einen Durchführungsweg aus der Liste wählen."
    manual = bool(f.text("manual_net"))
    ratio = f.percent("net_ratio", 1, 100) if manual else None
    return {"name": name, "vehicle": vehicle or None, "is_vested": bool(f.text("is_vested")),
            "manual_net": manual, "net_ratio": ratio}


@router.post("")
async def create_occupational(request: Request, session: Session = Depends(get_session)):
    f = await _form_input(request)
    data = _apply_contract_form(f, session, None)
    if f.errors:
        return _render_contract_form(request, f.raw, f.errors, status_code=422)
    contract = PensionContract(
        name=data["name"], pension_type=PensionType.OCCUPATIONAL, vehicle=data["vehicle"],
        is_vested=data["is_vested"], manual_net=data["manual_net"], net_ratio=data["net_ratio"] or 0.75,
    )
    session.add(contract)
    session.commit()
    return RedirectResponse(url=f"/pensions/{contract.id}/snapshots/new", status_code=303)


@router.get("/{contract_id}/edit")
def edit_contract_form(request: Request, contract_id: int, session: Session = Depends(get_session)):
    c = _get_contract(session, contract_id)
    values = {
        "name": c.name, "vehicle": c.vehicle or "", "is_vested": "on" if c.is_vested else "",
        "manual_net": "on" if c.manual_net else "", "net_ratio": _pct_input(c.net_ratio),
    }
    return _render_contract_form(request, values, {}, contract=c)


@router.post("/{contract_id}/edit")
async def edit_contract(request: Request, contract_id: int, session: Session = Depends(get_session)):
    c = _get_contract(session, contract_id)
    f = await _form_input(request)
    data = _apply_contract_form(f, session, c)
    if f.errors:
        return _render_contract_form(request, f.raw, f.errors, contract=c, status_code=422)
    c.name = data["name"]
    c.manual_net = data["manual_net"]
    if data["net_ratio"] is not None:
        c.net_ratio = data["net_ratio"]
    if c.pension_type == PensionType.OCCUPATIONAL:
        c.vehicle = data["vehicle"]
        c.is_vested = data["is_vested"]
    session.commit()
    return RedirectResponse(url="/pensions", status_code=303)


@router.post("/{contract_id}/deactivate")
def deactivate_contract(contract_id: int, session: Session = Depends(get_session)):
    contract = _get_contract(session, contract_id)
    contract.is_active = False
    session.commit()
    return RedirectResponse(url="/pensions", status_code=303)


# --------------------------------------------------------------------------- Stichtage


def _fields(contract: PensionContract):
    return STATUTORY_FIELDS if contract.pension_type == PensionType.STATUTORY else OCCUPATIONAL_FIELDS


def _read_snapshot_form(f: FormInput, contract: PensionContract, snap: PensionSnapshot) -> None:
    """Überträgt die Formularwerte auf snap und sammelt Fehler in f.errors."""
    benefit_form = f.text("benefit_form") or "rente"
    for form_name, attr, kind in _fields(contract):
        value = f.number(form_name) if kind == "number" else f.money(form_name)
        # Nur die gewählte Leistungsform übernehmen, die andere leeren.
        if (benefit_form == "rente" and form_name in CAPITAL_FIELDS) or (
            benefit_form == "kapital" and form_name in MONTHLY_FIELDS
        ):
            value = None
        setattr(snap, attr, value)

    if contract.pension_type == PensionType.STATUTORY:
        snap.value_basis = ValueBasis.REAL
        if snap.pension_value_cent is not None and not 1000 <= snap.pension_value_cent <= 20000:
            f.errors["pension_value"] = "Der Rentenwert liegt bei rund 30–50 €. Bitte prüfen."
        if not (snap.projected_monthly_cent or snap.accrued_monthly_cent or snap.earning_points):
            f.errors.setdefault("projected_monthly", "Bitte mindestens die hochgerechnete Rente eintragen.")
    else:
        basis = f.text("value_basis") or ValueBasis.NOMINAL.value
        snap.value_basis = ValueBasis(basis) if basis in ValueBasis.__members__ else ValueBasis.NOMINAL
        main = ("guaranteed_monthly", "projected_monthly") if benefit_form == "rente" else ("guaranteed_capital", "projected_capital")
        if not any(getattr(snap, f"{m}_cent") for m in main):
            f.errors.setdefault(main[0], "Bitte mindestens die garantierte oder die prognostizierte Leistung eintragen.")


def _snapshot_context(contract: PensionContract, target: date, values: dict, errors: dict, is_update: bool):
    history = [
        {
            "as_of": s.as_of.isoformat(),
            "as_of_de": s.as_of.strftime("%d.%m.%Y"),
            "ep": _num_input(s.earning_points, 4) or "–",
            "projected": format_euro(s.projected_monthly_cent) if s.projected_monthly_cent is not None else "–",
            "guaranteed": format_euro(s.guaranteed_monthly_cent) if s.guaranteed_monthly_cent is not None else "–",
            "capital": format_euro(s.projected_capital_cent or s.guaranteed_capital_cent)
            if (s.projected_capital_cent or s.guaranteed_capital_cent) else "–",
            "current": format_euro(s.current_value_cent) if s.current_value_cent is not None else "–",
        }
        for s in reversed(contract.snapshots)
    ]
    return {
        "contract": contract,
        "is_statutory": contract.pension_type == PensionType.STATUTORY,
        "target_date": target.isoformat(),
        "v": values,
        "errors": errors,
        "is_update": is_update,
        "history": history,
    }


@router.get("/{contract_id}/snapshots/new")
def snapshot_form(
    request: Request,
    contract_id: int,
    as_of: str | None = None,
    session: Session = Depends(get_session),
):
    contract = _get_contract(session, contract_id)
    try:
        target = date.fromisoformat(as_of) if as_of else date.today()
    except ValueError:
        target = date.today()
    snaps = list(contract.snapshots)
    existing = next((s for s in snaps if s.as_of == target), None)
    # Vorbelegung: vorhandener Stand am Datum, sonst letzter Stand davor (nur ändern, was neu ist).
    source = existing or next((s for s in reversed(snaps) if s.as_of < target), None)

    values: dict[str, str] = {}
    for form_name, attr, kind in _fields(contract):
        raw = getattr(source, attr) if source else None
        values[form_name] = _num_input(raw) if kind == "number" else _money_input(raw)
    if contract.pension_type == PensionType.STATUTORY and (existing is None or existing.pension_value_cent is None):
        # Der Rentenwert gehört zum Datum des Schreibens, nicht vom Vorjahr übernehmen.
        values["pension_value"] = _money_input(pension_value_at(target))
    has_capital = bool(source and (source.guaranteed_capital_cent or source.projected_capital_cent))
    has_monthly = bool(source and (source.guaranteed_monthly_cent or source.projected_monthly_cent))
    values["benefit_form"] = "kapital" if has_capital and not has_monthly else "rente"
    values["value_basis"] = source.value_basis.value if source and source.value_basis else ValueBasis.NOMINAL.value

    return templates.TemplateResponse(
        request, "pension_snapshot_form.html",
        _snapshot_context(contract, target, values, {}, existing is not None),
    )


@router.post("/{contract_id}/snapshots")
async def save_snapshot(request: Request, contract_id: int, session: Session = Depends(get_session)):
    contract = _get_contract(session, contract_id)
    f = await _form_input(request)
    target = f.iso_date("as_of")
    if target is None:
        f.errors.setdefault("as_of", "Bitte das Datum des Schreibens eingeben.")
        target = date.today()
    elif target > date.today():
        f.errors["as_of"] = "Das Datum liegt in der Zukunft."

    snap = session.execute(
        select(PensionSnapshot).where(
            PensionSnapshot.contract_id == contract.id, PensionSnapshot.as_of == target
        )
    ).scalar_one_or_none()
    is_update = snap is not None
    with session.no_autoflush:
        work = PensionSnapshot(contract_id=contract.id, as_of=target)
        _read_snapshot_form(f, contract, work)

    if f.errors:
        return templates.TemplateResponse(
            request, "pension_snapshot_form.html",
            _snapshot_context(contract, target, f.raw, f.errors, is_update), status_code=422,
        )

    if snap is None:
        session.add(work)
    else:
        for _, attr, _ in _fields(contract):
            setattr(snap, attr, getattr(work, attr))
        snap.value_basis = work.value_basis
    session.commit()
    return RedirectResponse(url="/pensions", status_code=303)


@router.post("/{contract_id}/preview", response_class=HTMLResponse)
async def preview(request: Request, contract_id: int, session: Session = Depends(get_session)):
    """Live-Vorschau fürs Stichtagsformular (HTML-Fragment)."""
    contract = _get_contract(session, contract_id)
    profile, settings = _get_singletons(session)
    a, _, years, _ = _assumptions(profile, settings)
    f = await _form_input(request)
    snap = PensionSnapshot(contract_id=contract.id, as_of=f.iso_date("as_of") or date.today())
    _read_snapshot_form(f, contract, snap)
    parse_errors = {k: v for k, v in f.errors.items() if "mindestens" not in v}
    if parse_errors:
        return "<p>Vorschau erscheint, sobald die Eingaben gültig sind.</p>"
    est = estimate(contract, snap, a)
    if est.expected_gross_cent == 0:
        return "<p>Trag einen Betrag ein, dann erscheint hier das Ergebnis.</p>"

    rw = format_euro(a.current_pension_value_cent)
    if contract.pension_type == PensionType.STATUTORY:
        html = (
            f"<p><strong>≈ {_num_input(est.earning_points, 2)} Entgeltpunkte</strong> → "
            f"<strong>{format_euro(est.expected_gross_cent)}</strong> brutto im Monat "
            f"in heutiger Kaufkraft (× Rentenwert {rw}).</p>"
        )
    else:
        if est.conservative_gross_cent == est.expected_gross_cent:
            amount = f"<strong>{format_euro(est.expected_gross_cent)}</strong>"
        else:
            amount = (f"garantiert <strong>{format_euro(est.conservative_gross_cent)}</strong>, "
                      f"erwartet <strong>{format_euro(est.expected_gross_cent)}</strong>")
        html = f"<p>≈ {amount} brutto im Monat in heutiger Kaufkraft.</p>"
        notes = []
        if snap.value_basis == ValueBasis.NOMINAL:
            if years is None:
                notes.append("Geburtsdatum fehlt: noch nicht auf heutige Kaufkraft abgezinst.")
            else:
                notes.append(f"Abgezinst über {years:.0f} Jahre mit {_pct(a.inflation)} Inflation.")
        if snap.guaranteed_capital_cent or snap.projected_capital_cent:
            notes.append(f"Kapital verrentet über {a.payout_years} Jahre.")
        if notes:
            html += "<p><small>" + " ".join(notes) + "</small></p>"
    for w in est.warnings:
        html += f'<p><small class="warn">⚠ {escape(w)}</small></p>'
    html += "<p><small>Steuer und Krankenversicherung zieht die Übersicht ab.</small></p>"
    return html
