"""Entnahme aus Depots im Ruhestand und FIRE-Rechnung – ohne Datenbankbezug, testbar.

Alle Beträge real (heutige Kaufkraft) in Euro als float: Hier werden Szenarien simuliert,
keine Buchungen erzeugt (Geld als Cent bleibt in Modellen und Formularen).

Steuer auf Entnahmen: Nur der Gewinnanteil einer Entnahme ist steuerpflichtig
(§ 20 Abs. 2 und 4 EStG), bei Fonds nach Teilfreistellung (§ 20 InvStG), abzüglich
Sparerpauschbetrag (§ 20 Abs. 9 EStG), zum Abgeltungsteuersatz (§ 32d EStG). Der
Gewinnanteil folgt aus Einstand und Wert (Durchschnittsmethode als Näherung für FIFO).
Kapitalerträge sind bei PKV beitragsfrei; bei freiwilliger GKV wären sie beitragspflichtig
(§ 240 SGB V) – das wird nicht modelliert.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class TaxParams:
    rate: float = 0.26375          # Abgeltungsteuer inkl. Soli (+ KiSt)
    exemption: float = 0.3         # gewichtete Teilfreistellung des Depots
    allowance_eur: float = 1000.0  # Sparerpauschbetrag pro Jahr


def gross_withdrawal(net_eur: float, gain_share: float, tax: TaxParams) -> float:
    """Brutto-Entnahme pro Jahr, damit nach Abgeltungsteuer net_eur übrig bleiben.

    Steuer = max(W · g · (1 − TF) − Pauschbetrag, 0) · s  →  nach W aufgelöst.
    """
    if net_eur <= 0:
        return 0.0
    k = max(0.0, min(1.0, gain_share)) * (1 - tax.exemption) * tax.rate
    if k <= 0:
        return net_eur
    w = (net_eur - tax.allowance_eur * tax.rate) / (1 - k)
    # Liegt der steuerpflichtige Teil unter dem Pauschbetrag, fällt keine Steuer an.
    if w * max(0.0, gain_share) * (1 - tax.exemption) <= tax.allowance_eur:
        return net_eur
    return w


def tax_on_withdrawal(gross_eur: float, gain_share: float, tax: TaxParams) -> float:
    taxable = gross_eur * max(0.0, min(1.0, gain_share)) * (1 - tax.exemption) - tax.allowance_eur
    return max(taxable, 0.0) * tax.rate


def annuity_factor(rate: float, years: int) -> float:
    if years <= 0:
        return 0.0
    if abs(rate) < 1e-12:
        return float(years)
    return (1 - (1 + rate) ** -years) / rate


# --------------------------------------------------------------------------- Rente: Entnahme


@dataclass
class Drawdown:
    """Monatliche Entnahme aus einem Kapital zum Rentenbeginn (Kapitalverzehr, real)."""

    capital_eur: float
    basis_eur: float
    gross_monthly_eur: float
    tax_monthly_eur: float

    @property
    def net_monthly_eur(self) -> float:
        return self.gross_monthly_eur - self.tax_monthly_eur

    @property
    def gain_share(self) -> float:
        return 1 - self.basis_eur / self.capital_eur if self.capital_eur > 0 else 0.0


def drawdown(capital_eur: float, basis_eur: float, payout_real_return: float, payout_years: int,
             tax: TaxParams) -> Drawdown:
    """Gleichbleibende reale Entnahme, die das Kapital über payout_years aufbraucht.

    Gleiche Methode wie der Kapitalbedarf der Rentenlücke (Rentenbarwertfaktor), damit
    Bedarf und Entnahme zusammenpassen.
    """
    af = annuity_factor(payout_real_return, payout_years)
    gross = capital_eur / af / 12 if af > 0 else 0.0
    g = 1 - basis_eur / capital_eur if capital_eur > 0 else 0.0
    return Drawdown(capital_eur, basis_eur, gross, tax_on_withdrawal(gross * 12, g, tax) / 12)


# --------------------------------------------------------------------------- FIRE


@dataclass(frozen=True)
class FireInputs:
    current_age: float
    retirement_age: int             # Beginn der gesetzlichen Rente
    horizon_age: int                # Vermögen soll mindestens bis zu diesem Alter reichen
    start_eur: float                # investierbares Vermögen heute
    basis_eur: float                # Einstand davon
    savings_yearly_eur: float       # Sparleistung pro Jahr bis FIRE
    mu: float                       # Log-Drift real p. a. (nach Kosten)
    sigma: float                    # Volatilität p. a.
    tax: TaxParams = field(default_factory=TaxParams)


def simulate_returns(years: int, paths: int, mu: float, sigma: float, seed: int = 7) -> list[list[float]]:
    """Jährliche Bruttorenditefaktoren (log-normal), fester Seed → stabile Anzeige."""
    rng = random.Random(seed)
    return [[math.exp(rng.gauss(mu, sigma)) for _ in range(years)] for _ in range(paths)]


def fire_success(
    inp: FireInputs,
    fire_age: int,
    spending: Callable[[int], float],
    returns: list[list[float]],
) -> float:
    """Anteil der Pfade, in denen das Vermögen bei FIRE mit fire_age bis horizon_age reicht.

    Ablauf je Jahr: bis FIRE Sparrate am Jahresende; ab FIRE Entnahme am Jahresanfang
    (Netto-Bedarf spending(alter), per gross_withdrawal um die Steuer erhöht), danach Rendite.
    Einstand wird bei Entnahmen anteilig reduziert (Durchschnittsmethode).
    """
    start_age = int(math.floor(inp.current_age))
    years = inp.horizon_age - start_age
    need = [spending(start_age + y) if start_age + y >= fire_age else 0.0 for y in range(years)]
    ok = 0
    for path in returns:
        v, b = inp.start_eur, inp.basis_eur
        alive = True
        for y in range(years):
            r = path[y]
            if start_age + y < fire_age:
                v = v * r + inp.savings_yearly_eur
                b += inp.savings_yearly_eur
                continue
            n = need[y]
            if n > 0:
                if v <= 0:
                    alive = False
                    break
                w = gross_withdrawal(n, 1 - b / v, inp.tax)
                if w > v:
                    alive = False
                    break
                b -= b * w / v
                v -= w
            v *= r
        ok += alive
    return ok / len(returns) if returns else 0.0


@dataclass
class FireCurve:
    ages: list[int]
    success: list[float]

    def earliest(self, threshold: float) -> int | None:
        for a, s in zip(self.ages, self.success):
            if s >= threshold:
                return a
        return None


def fire_curve(inp: FireInputs, spending_for: Callable[[int], Callable[[int], float]], paths: int = 500) -> FireCurve:
    """Erfolgswahrscheinlichkeit für jedes mögliche FIRE-Alter bis zum Rentenbeginn.

    spending_for(fire_age) liefert die Bedarfsfunktion für dieses FIRE-Alter – nötig, weil
    ein früherer Ausstieg die gesetzliche Rente und damit den Bedarf ab Rentenbeginn ändert.
    Alle FIRE-Alter nutzen dieselben Renditepfade (gemeinsame Zufallszahlen), damit die
    Kurve glatt ist und Unterschiede nur aus dem Alter kommen.
    """
    start_age = int(math.floor(inp.current_age))
    returns = simulate_returns(inp.horizon_age - start_age, paths, inp.mu, inp.sigma)
    ages = list(range(start_age + 1, inp.retirement_age + 1))
    return FireCurve(ages, [fire_success(inp, a, spending_for(a), returns) for a in ages])


def pension_share_at(fire_age: float, current_age: float, retirement_age: int,
                     accrued_share: float | None, career_start_age: int = 22) -> float:
    """Anteil der hochgerechneten gesetzlichen Rente, der bei Ausstieg mit fire_age bleibt.

    Entgeltpunkte wachsen mit jedem Beitragsjahr etwa linear (§ 70 SGB VI). Bekannt ist der
    bisher erreichte Anteil (Renteninformation: „bisher erreichte Rente“ / Hochrechnung);
    fehlt er, wird ein linearer Aufbau ab career_start_age angenommen.
    Beitragsfreie Zeiten und freiwillige Beiträge nach dem Ausstieg bleiben unberücksichtigt.
    """
    if fire_age >= retirement_age:
        return 1.0
    if accrued_share is None:
        span = max(retirement_age - career_start_age, 1)
        accrued_share = max(0.0, min(1.0, (current_age - career_start_age) / span))
    remaining = max(retirement_age - current_age, 1e-9)
    worked = max(0.0, min(fire_age - current_age, remaining))
    return accrued_share + (1 - accrued_share) * worked / remaining


def earliest_fire_age(inp: FireInputs, spending_for: Callable[[int], Callable[[int], float]],
                      threshold: float, paths: int = 400) -> int | None:
    """Frühestes FIRE-Alter mit Erfolgswahrscheinlichkeit ≥ threshold (Bisektion).

    Setzt voraus, dass die Erfolgswahrscheinlichkeit mit späterem Ausstieg nicht sinkt – das
    gilt, solange die Sparrate positiv ist (mehr Kapital, kürzere Entnahmephase).
    """
    start_age = int(math.floor(inp.current_age))
    returns = simulate_returns(inp.horizon_age - start_age, paths, inp.mu, inp.sigma)
    lo, hi = start_age + 1, inp.retirement_age
    if fire_success(inp, hi, spending_for(hi), returns) < threshold:
        return None
    while lo < hi:
        mid = (lo + hi) // 2
        if fire_success(inp, mid, spending_for(mid), returns) >= threshold:
            hi = mid
        else:
            lo = mid + 1
    return lo
