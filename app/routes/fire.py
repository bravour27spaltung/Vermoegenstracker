"""FIRE (Financial Independence, Retire Early): Ab wann reicht das Vermögen?

Kern ist eine Monte-Carlo-Simulation statt einer festen Entnahmeregel: Für jedes mögliche
Ausstiegsalter wird geprüft, in welchem Anteil der Renditepfade das Vermögen bis zum
Planungshorizont reicht. Berücksichtigt werden
- volle Ausgaben inkl. Kranken-/Pflegeversicherung bis zum Rentenbeginn (bei PKV der volle
  Beitrag ohne Arbeitgeberzuschuss, ohne Krankentagegeld, mit realer Beitragssteigerung),
- ab Rentenbeginn nur noch die Rentenlücke, wobei die gesetzliche Rente und die bAV bei
  früherem Ausstieg anteilig gekürzt werden (app.retirement.pension_share_at),
- Abgeltungsteuer auf den Gewinnanteil der Entnahmen.
Die klassische FIRE-Zahl (Ausgaben ÷ Entnahmerate) wird zum Vergleich angezeigt.
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.deps import get_session
from app.models import FireSettings, HealthInsurance, PensionType
from app.money import format_euro, parse_flexible_amount
from app.pension import EXPECTED, estimate, income_breakdown
from app.queries import active_pensions_with_latest
from app.retirement import (
    FireInputs,
    earliest_fire_age,
    fire_curve,
    gross_withdrawal,
    pension_share_at,
)
from app.routes.pensions import _assumptions, _get_singletons, _pct
from app.svg_charts import fire_curve_svg
from app.wealth import wealth

router = APIRouter(prefix="/fire", tags=["fire"])
templates = Jinja2Templates(directory="app/templates")


def _settings(session: Session) -> FireSettings:
    s = session.get(FireSettings, 1)
    if s is None:
        s = FireSettings(id=1)
        session.add(s)
        session.commit()
    return s


def _eur(cent: float) -> str:
    return format_euro(round(cent)).rsplit(",", 1)[0] + " €"


def _age(birth: date, today: date) -> float:
    return (today - birth).days / 365.25


def fire_context(session: Session, today: date | None = None) -> dict:
    today = today or date.today()
    profile, psettings = _get_singletons(session)
    fs = _settings(session)
    ctx: dict = dict(fs=fs, profile=profile, ready=False)
    if profile.birth_date is None:
        ctx["missing"] = "Geburtsdatum fehlt"
        return ctx
    a, h, _, _ = _assumptions(profile, psettings)
    current_age = _age(profile.birth_date, today)
    w = wealth(session, today)
    d = w.depots

    # ---- Ausgaben
    spending = fs.spending_monthly_cent if fs.spending_monthly_cent is not None else profile.desired_income_monthly_cent
    if fs.health_monthly_cent is not None:
        health = fs.health_monthly_cent
        health_source = "eigene Angabe"
    elif psettings.health_insurance == HealthInsurance.PKV:
        health = max(psettings.pkv_today_total_cent - psettings.pkv_today_sick_pay_cent, 0) + psettings.pkv_today_care_cent
        health_source = "heutiger PKV-Beitrag ohne Krankentagegeld, plus Pflege, voll selbst getragen"
    else:
        health = 0
        health_source = "bitte eintragen (freiwillige GKV)"
    growth = psettings.pkv_real_increase if psettings.health_insurance == HealthInsurance.PKV else 0.0
    if spending <= 0:
        ctx["missing"] = "Wunscheinkommen bzw. Ausgaben fehlen"
        return ctx

    # ---- Renten bei früherem Ausstieg
    estimates, accrued_share = [], None
    for contract, snap in active_pensions_with_latest(session):
        if snap is None:
            continue
        estimates.append(estimate(contract, snap, a))
        if (contract.pension_type == PensionType.STATUTORY and snap.accrued_monthly_cent
                and snap.projected_monthly_cent):
            accrued_share = min(1.0, snap.accrued_monthly_cent / snap.projected_monthly_cent)
    gap_cache: dict[int, tuple[int, int]] = {}

    def gap_after_pension(fire_age: int) -> tuple[int, int]:
        """(verfügbare Rente, Lücke) pro Monat in Cent bei Ausstieg mit fire_age."""
        if fire_age not in gap_cache:
            share = pension_share_at(fire_age, current_age, profile.retirement_age, accrued_share, fs.career_start_age)
            scaled = [replace(e, conservative_gross_cent=round(e.conservative_gross_cent * share),
                              expected_gross_cent=round(e.expected_gross_cent * share)) for e in estimates]
            available = income_breakdown(scaled, EXPECTED, h).available_cent
            gap_cache[fire_age] = (available, max(profile.desired_income_monthly_cent - available, 0))
        return gap_cache[fire_age]

    def spending_factory(factor: float = 1.0):
        """Bedarf je Alter; factor skaliert die Lebenshaltung (nicht die Krankenversicherung)."""
        def spending_for(fire_age: int):
            available, _ = gap_after_pension(fire_age)
            gap = max(profile.desired_income_monthly_cent * factor - available, 0)

            def need(age: int) -> float:
                if age >= profile.retirement_age:
                    return gap * 12 / 100
                return (spending * factor + health * (1 + growth) ** max(age - current_age, 0)) * 12 / 100
            return need
        return spending_for

    spending_for = spending_factory()

    # ---- Vermögen und Simulation
    start = (d.value_cent + w.other_accessible_cent) / 100
    basis = (d.basis_cent + w.other_accessible_cent) / 100
    savings = (profile.planned_savings_monthly_cent or d.savings_monthly_cent) * 12 / 100
    inp = FireInputs(current_age=current_age, retirement_age=profile.retirement_age, horizon_age=fs.horizon_age,
                     start_eur=start, basis_eur=basis, savings_yearly_eur=savings, mu=d.mu, sigma=d.sigma, tax=d.tax)
    curve = fire_curve(inp, spending_for, paths=1000)
    target, median_age = curve.earliest(fs.target_success), curve.earliest(0.5)

    # Klassische FIRE-Zahl: heutige Jahresausgaben (inkl. KV, brutto vor Steuer) ÷ Entnahmerate
    gain_share = 1 - basis / start if start > 0 else 0.0
    annual_net = (spending + health) * 12 / 100
    annual_gross = gross_withdrawal(annual_net, gain_share, d.tax)
    classic = annual_gross / profile.withdrawal_rate if profile.withdrawal_rate > 0 else 0.0
    # Coast FIRE: Kapital heute, das ohne weiteres Sparen bis Rentenbeginn die Lücke bei voller Rente deckt
    years_to_ret = max(profile.retirement_age - current_age, 0)
    coast_gain_share = 1 - math.exp(-d.mu * years_to_ret) if d.mu > 0 else 0.0  # Wachstum ab heute ohne Nachkauf
    full_gap_gross = gross_withdrawal(gap_after_pension(profile.retirement_age)[1] * 12 / 100, coast_gain_share, d.tax)
    coast_at_ret = full_gap_gross / profile.withdrawal_rate if profile.withdrawal_rate > 0 else 0.0
    coast_today = coast_at_ret / math.exp(d.mu * years_to_ret)

    factors = (1.0, 0.85, 0.7)
    variants = []
    for extra in (0, 250, 500):
        v_inp = replace(inp, savings_yearly_eur=savings + extra * 12)
        variants.append(dict(extra=extra, savings=_eur((savings / 12 + extra) * 100),
                             ages=[earliest_fire_age(v_inp, spending_factory(f), fs.target_success, paths=600)
                                   for f in factors]))
    factor_labels = [_eur(spending * f) for f in factors]

    pension_rows = []
    for age in sorted({a_ for a_ in (target, median_age, 45, 50, 55, 60, profile.retirement_age) if a_}):
        avail, gap = gap_after_pension(age)
        pension_rows.append(dict(age=age, available=_eur(avail), gap=_eur(gap),
                                 share=pension_share_at(age, current_age, profile.retirement_age, accrued_share,
                                                        fs.career_start_age)))

    ctx.update(
        ready=True,
        current_age=current_age,
        start=_eur(start * 100), depot_value=_eur(d.value_cent), other_value=_eur(w.other_accessible_cent),
        savings=_eur(savings / 12 * 100),
        spending=_eur(spending), health=_eur(health), health_source=health_source, health_growth=_pct(growth),
        spending_default=fs.spending_monthly_cent is None,
        classic=_eur(classic * 100), classic_progress=start / classic if classic else None,
        annual_net=_eur(annual_net * 100), annual_gross=_eur(annual_gross * 100),
        coast=_eur(coast_today * 100), coast_reached=start >= coast_today,
        target=target, median_age=median_age, target_success=fs.target_success,
        target_year=profile.birth_date.year + target if target else None,
        median_return=_pct(math.exp(d.mu) - 1), sigma=_pct(d.sigma),
        curve_svg=fire_curve_svg(curve.ages, curve.success, fs.target_success),
        variants=variants, factor_labels=factor_labels, pension_rows=pension_rows,
        success_at_retirement=curve.success[-1] if curve.success else None, accrued_known=accrued_share is not None,
        withdrawal_rate=profile.withdrawal_rate, retirement_age=profile.retirement_age,
        tax_rate=_pct(d.tax.rate, 3), exemption=_pct(d.tax.exemption, 0),
        allowance=_eur(d.tax.allowance_eur * 100), withdrawal=_pct(profile.withdrawal_rate, 2),
        horizon=fs.horizon_age,
    )
    return ctx


@router.get("")
def fire_page(request: Request, session: Session = Depends(get_session)):
    ctx = fire_context(session)
    ctx["error"] = request.query_params.get("error")
    ctx["ok"] = request.query_params.get("ok")
    return templates.TemplateResponse(request, "fire.html", ctx)


def _ratio(text: str | None, lo: float, hi: float) -> float:
    s = (text or "").strip().replace("%", "").replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    v = float(s)
    if not lo <= v <= hi:
        raise ValueError(f"Wert zwischen {lo:g} und {hi:g} % erwartet")
    return v / 100


@router.post("/settings")
async def save_settings(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    fs = _settings(session)
    profile, _ = _get_singletons(session)
    try:
        spend = (form.get("spending") or "").strip()
        fs.spending_monthly_cent = parse_flexible_amount(spend) if spend else None
        health = (form.get("health") or "").strip()
        fs.health_monthly_cent = parse_flexible_amount(health) if health else None
        fs.horizon_age = int(form.get("horizon_age") or 95)
        if not 70 <= fs.horizon_age <= 110:
            raise ValueError("Planungshorizont zwischen 70 und 110 Jahren")
        fs.target_success = _ratio(form.get("target_success"), 50, 99)
        fs.career_start_age = int(form.get("career_start_age") or 22)
        profile.withdrawal_rate = _ratio(form.get("withdrawal_rate"), 1, 10)
        session.commit()
    except (ValueError, TypeError) as exc:
        session.rollback()
        return RedirectResponse(url=f"/fire?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(url="/fire?ok=Gespeichert", status_code=303)
