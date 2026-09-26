"""Depots: Positionen je Wertpapier, Einzahlungen, Rendite, Steuer und Prognose.

Ergänzt die Positionen/Stichtage um die Wertpapierebene. Erfasst werden Käufe,
Verkäufe und Ausschüttungen sowie Kurse je Stichtag; alles Weitere wird berechnet
(app/depot.py). Über „Stichtag übernehmen“ landen Wert und Netto-Einzahlungen je
Anlageklasse als Snapshot in den Positionen, damit Nettovermögen und Rentenlücke
die Depotdaten verwenden.

Bedienung wie im Rest der App: Beträge als '1.234,56' oder '1234.56', Fehler werden
über der Seite angezeigt, Formulare funktionieren ohne JavaScript. Die Prognose
aktualisiert sich beim Tippen per fetch (wie die Vorschau bei den Renten).
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import depot as calc
from app.broker_import import ParsedImport, is_trade_republic, parse_trade_republic
from app.deps import get_session
from app.models import (
    AssetClass,
    Depot,
    DepotPositionLink,
    DepotSettings,
    DepotTransaction,
    Position,
    PositionKind,
    Profile,
    ReturnAssumption,
    Security,
    SecurityPrice,
    Snapshot,
    TransactionKind,
)
from app.money import format_euro, parse_flexible_amount
from app.overview import due_year_end
from app.svg_charts import forecast_svg, value_series_svg

router = APIRouter(prefix="/depots", tags=["depots"])
templates = Jinja2Templates(directory="app/templates")

KIND_LABELS = {TransactionKind.BUY: "Kauf", TransactionKind.SELL: "Verkauf", TransactionKind.DIVIDEND: "Ausschüttung"}
KIND_CSV = {"kauf": TransactionKind.BUY, "verkauf": TransactionKind.SELL, "dividende": TransactionKind.DIVIDEND,
            "ausschuettung": TransactionKind.DIVIDEND, "ausschüttung": TransactionKind.DIVIDEND}
EXEMPTIONS = [(0.3, "30 % Aktienfonds"), (0.15, "15 % Mischfonds"), (0.6, "60 % Immobilienfonds"),
              (0.8, "80 % Immobilienfonds Ausland"), (0.0, "0 % sonstige Fonds")]


# --------------------------------------------------------------------------- Formatierung


def _eur(cent: int | None, digits: int = 2) -> str:
    if cent is None:
        return "–"
    s = format_euro(round(cent))
    if digits == 0:
        s = s.rsplit(",", 1)[0] + " €"
    return s.replace("-", "−")


def _eur_f(value: float, digits: int = 0) -> str:
    return _eur(round(value * 100), digits)


def _pct(ratio: float | None, digits: int = 1, sign: bool = False) -> str:
    if ratio is None:
        return "–"
    s = f"{ratio * 100:+.{digits}f}" if sign else f"{ratio * 100:.{digits}f}"
    return s.replace(".", ",").replace("-", "−") + " %"


def _num(value: float | None, digits: int = 4) -> str:
    if value is None:
        return ""
    s = f"{value:,.{digits}f}".rstrip("0").rstrip(".")
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _dt(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else "–"


def _tone(x: float | int | None) -> str:
    if x is None or abs(x) < 0.5e-2:
        return ""
    return "good" if x > 0 else "bad"


templates.env.filters.update(eur=_eur, eur_f=_eur_f, pct=_pct, num=_num, dt=_dt, tone=_tone)
templates.env.globals.update(CLASS_LABELS=calc.ASSET_CLASS_LABELS, KIND_LABELS=KIND_LABELS, EXEMPTIONS=EXEMPTIONS)


# --------------------------------------------------------------------------- Eingabe


def _parse_quantity(text: str) -> float:
    s = (text or "").strip().replace(" ", "")
    if not s:
        raise ValueError("Stückzahl fehlt")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        q = float(s)
    except ValueError as exc:
        raise ValueError(f"Keine gültige Stückzahl: {text!r}") from exc
    if q <= 0:
        raise ValueError("Stückzahl muss größer als 0 sein")
    return q


def _parse_ratio(text: str, lo: float = 0, hi: float = 100) -> float:
    s = (text or "").strip().replace("%", "").replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    v = float(s)
    if not lo <= v <= hi:
        raise ValueError(f"Wert zwischen {lo:g} und {hi:g} % erwartet")
    return v / 100


def _check_isin(text: str | None) -> str:
    """ISIN (12 Zeichen) oder Krypto-Kürzel des Brokers (z. B. BTC)."""
    isin = (text or "").strip().upper()
    if not (isin.isalnum() and (len(isin) == 12 or 2 <= len(isin) <= 6)):
        raise ValueError("Bitte eine ISIN (12 Zeichen) oder ein Krypto-Kürzel wie BTC angeben")
    return isin


def _amount_from(quantity: float, price_text: str) -> int:
    """Kurswert in Cent aus Stück × Kurs, ohne float-Rundung beim Kurs."""
    price_cent = parse_flexible_amount(price_text)
    return int((Decimal(str(quantity)) * price_cent).quantize(Decimal("1")))


def _back(url: str, error: str | None = None, ok: str | None = None) -> RedirectResponse:
    """Zurück zur Seite, Meldung als Query-Parameter (vor einem #Anker)."""
    url, _, anchor = url.partition("#")
    sep = "&" if "?" in url else "?"
    if error:
        url += f"{sep}error={quote(error)}"
    elif ok:
        url += f"{sep}ok={quote(ok)}"
    return RedirectResponse(url=url + (f"#{anchor}" if anchor else ""), status_code=303)


# --------------------------------------------------------------------------- Laden


def _settings(session: Session) -> DepotSettings:
    s = session.get(DepotSettings, 1)
    if s is None:
        s = DepotSettings(id=1)
        session.add(s)
        session.commit()
    return s


def _profile(session: Session) -> Profile:
    p = session.get(Profile, 1)
    if p is None:
        p = Profile(id=1)
        session.add(p)
        session.commit()
    return p


def _assumptions(session: Session) -> dict[AssetClass, tuple[float, float]]:
    a = dict(calc.DEFAULT_ASSUMPTIONS)
    for row in session.execute(select(ReturnAssumption)).scalars():
        a[row.asset_class] = (row.real_return, row.volatility)
    return a


def _transactions(session: Session, depot_ids: list[int] | None = None) -> list[DepotTransaction]:
    stmt = select(DepotTransaction).options(selectinload(DepotTransaction.security)).order_by(
        DepotTransaction.trade_date, DepotTransaction.id
    )
    if depot_ids is not None:
        stmt = stmt.where(DepotTransaction.depot_id.in_(depot_ids))
    return list(session.execute(stmt).scalars())


def _prices(session: Session) -> list[SecurityPrice]:
    return list(session.execute(select(SecurityPrice)).scalars())


def _active_depots(session: Session) -> list[Depot]:
    return list(session.execute(select(Depot).where(Depot.is_active.is_(True)).order_by(Depot.name)).scalars())


def _get_depot(session: Session, depot_id: int) -> Depot:
    d = session.get(Depot, depot_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Depot nicht gefunden")
    return d


def _securities(session: Session) -> list[Security]:
    return list(session.execute(select(Security).order_by(Security.name)).scalars())


def _analysis(session: Session, depots: list[Depot], as_of: date) -> dict:
    """Alles, was Übersicht, Detailseite und API brauchen."""
    settings = _settings(session)
    profile = _profile(session)
    ids = [d.id for d in depots]
    txs = _transactions(session, ids)
    book = calc.PriceBook(_prices(session), txs)
    by_depot: dict[int, list] = defaultdict(list)
    for t in txs:
        by_depot[t.depot_id].append(t)
    summaries = [calc.summarize_depot(d, by_depot[d.id], book, as_of, settings.base_rate) for d in depots]
    total = sum(s.value_cent for s in summaries)
    positions = [p for s in summaries for p in s.positions]

    # Über Depots zusammengefasst (gleiches Wertpapier)
    merged: dict[int, dict] = {}
    for p in positions:
        m = merged.setdefault(p.security.id, dict(security=p.security, quantity=0.0, cost_cent=0, value_cent=0,
                                                  price_cent=p.price_cent, price_date=p.price_date, depots=[]))
        m["quantity"] += p.quantity
        m["cost_cent"] += p.cost_cent
        m["value_cent"] += p.value_cent
        m["depots"].append(next(s.depot.name for s in summaries if s.depot.id == p.depot_id))
    merged_rows = sorted(merged.values(), key=lambda m: -m["value_cent"])
    for m in merged_rows:
        m["gain_cent"] = m["value_cent"] - m["cost_cent"]
        m["gain_ratio"] = m["gain_cent"] / m["cost_cent"] if m["cost_cent"] else None
        m["share"] = m["value_cent"] / total if total else 0.0

    by_class: dict[AssetClass, int] = defaultdict(int)
    by_region: dict[str, int] = defaultdict(int)
    for p in positions:
        by_class[p.security.asset_class] += p.value_cent
        by_region[p.security.region or "–"] += p.value_cent
    ter = sum(p.security.ter * p.value_cent for p in positions) / total if total else 0.0
    savings = sum(d.monthly_savings_cent for d in depots)

    rate = calc.capital_gains_tax_rate(settings.church_tax_rate)
    taxable = sum(p.taxable_gain_cent for p in positions)
    latent_tax = round(max(taxable - settings.saver_allowance_cent, 0) * rate)
    advance = sum(p.taxable_advance_cent for p in positions)

    invested = sum(s.invested_cent for s in summaries)
    returned = sum(s.returned_cent for s in summaries)
    flows = [(t.trade_date, calc.cash_flow_cent(t) / 100) for t in txs if t.trade_date <= as_of]
    equity_target = (settings.equity_target, settings.equity_band) if settings.equity_target is not None else None
    hint_list = calc.hints(positions, total, by_class, profile.concentration_limit, equity_target,
                           ter, settings.ter_warn, savings, as_of)
    estimated = [t for t in txs if t.note and t.note.startswith("Einlieferung –")]
    if estimated:
        hint_list = [h for h in hint_list if h.level != "ok"]
        hint_list.insert(0, calc.Hint("warn", "Einstand fehlt",
                                      f"{len(estimated)} Einlieferung(en) mit geschätztem Einstand – Steuer und G/V sind dadurch ungenau. "
                                      "Im Depot unter Transaktionen → Bearbeiten korrigieren."))
    missing_ter = [p for p in positions if p.security.is_fund and p.security.ter == 0]
    if missing_ter:
        hint_list.insert(0 if not estimated else 1, calc.Hint("info", "TER fehlt",
                         f"{len({p.security.id for p in missing_ter})} Fonds ohne laufende Kosten – unter „Wertpapiere“ ergänzen, sonst ist die Prognose zu optimistisch."))
        hint_list = [h for h in hint_list if h.level != "ok"]

    return dict(
        as_of=as_of,
        settings=settings,
        profile=profile,
        summaries=summaries,
        positions=merged_rows,
        total_cent=total,
        invested_cent=invested,
        net_invested_cent=sum(s.net_invested_cent for s in summaries),
        gain_cent=total + returned - invested,
        gain_ratio=(total + returned - invested) / invested if invested else None,
        xirr=calc.xirr(flows + [(as_of, total / 100)]) if total else None,
        realized_cent=sum(s.realized_cent for s in summaries),
        distributions_cent=sum(s.distributions_cent for s in summaries),
        by_class=sorted(by_class.items(), key=lambda kv: -kv[1]),
        by_region=sorted(by_region.items(), key=lambda kv: -kv[1]),
        equity_share=by_class.get(AssetClass.EQUITIES, 0) / total if total else 0.0,
        ter=ter,
        savings_cent=savings,
        tax_rate=rate,
        taxable_cent=taxable,
        latent_tax_cent=latent_tax,
        after_tax_cent=total - latent_tax,
        advance_taxable_cent=advance,
        advance_tax_cent=round(advance * rate),
        hints=hint_list,
        series_svg=value_series_svg(calc.value_series(txs, book, as_of)),
        weights={c: v for c, v in by_class.items()},
    )


def _forecast_context(session: Session, a: dict, params, depot_id: int | None) -> dict:
    def num(name, default):
        raw = (params.get(name) or "").strip().replace(".", "").replace(",", ".")
        try:
            return float(raw) if raw else default
        except ValueError:
            return default

    years = int(max(1, min(num("years", 20), 50)))
    savings = max(num("savings", a["savings_cent"] / 100), 0.0)
    nominal = params.get("nominal") == "1"
    inflation = a["profile"].inflation_assumption
    weights = a["weights"] or {AssetClass.EQUITIES: 1}
    mu, sigma = calc.portfolio_parameters(weights, _assumptions(session), a["ter"])
    fc = calc.forecast(a["total_cent"] / 100, savings, mu, sigma, years, a["settings"].mc_paths)
    factor = (lambda y: (1 + inflation) ** y) if nominal else (lambda y: 1.0)
    milestones = [y for y in (5, 10, 15, 20, 25, 30, 40, 50) if y <= years]
    if years not in milestones:
        milestones.append(years)
    rows = [dict(years=y, year=a["as_of"].year + y, contributions=fc.contributions[y],
                 p10=fc.p10[y] * factor(y), p50=fc.p50[y] * factor(y), p90=fc.p90[y] * factor(y))
            for y in milestones]
    pg = {"jahre": fc.years, "p10": fc.p10, "p25": fc.p25, "p50": fc.p50, "p75": fc.p75, "p90": fc.p90,
          "einzahlungen": fc.contributions}
    return dict(
        fc=fc, years=years, savings=savings, nominal=nominal, rows=rows, depot_id=depot_id,
        inflation=inflation, median_return=pow(2.718281828459045, mu) - 1, sigma=sigma,
        paths=a["settings"].mc_paths, has_data=a["total_cent"] > 0 or savings > 0,
        svg=forecast_svg(pg, nominal, inflation * 100),
    )


def _msg(request: Request) -> dict:
    return dict(error=request.query_params.get("error"), ok=request.query_params.get("ok"))


# --------------------------------------------------------------------------- Übersicht


@router.get("")
def overview(request: Request, session: Session = Depends(get_session)):
    depots = _active_depots(session)
    a = _analysis(session, depots, date.today())
    return templates.TemplateResponse(request, "depots/overview.html", dict(
        a=a, fc=_forecast_context(session, a, request.query_params, None), **_msg(request),
    ))


@router.get("/forecast", response_class=HTMLResponse)
def forecast_partial(request: Request, depot: int | None = None, session: Session = Depends(get_session)):
    depots = [_get_depot(session, depot)] if depot else _active_depots(session)
    a = _analysis(session, depots, date.today())
    return templates.TemplateResponse(request, "depots/_forecast.html", dict(
        fc=_forecast_context(session, a, request.query_params, depot),
    ))


@router.post("")
async def create_depot(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return _back("/depots", "Bitte einen Namen angeben.")
    if session.execute(select(Depot).where(Depot.name == name)).scalar_one_or_none():
        return _back("/depots", f"Ein Depot „{name}“ gibt es schon.")
    try:
        savings = parse_flexible_amount(form.get("monthly_savings") or "0")
    except ValueError:
        return _back("/depots", "Sparrate: bitte einen Betrag eingeben, z. B. 500 oder 1.234,56.")
    d = Depot(name=name, broker=(form.get("broker") or "").strip() or None, monthly_savings_cent=savings)
    session.add(d)
    session.commit()
    return RedirectResponse(url=f"/depots/{d.id}", status_code=303)


# --------------------------------------------------------------------------- Depot


@router.get("/{depot_id:int}")
def detail(request: Request, depot_id: int, session: Session = Depends(get_session)):
    d = _get_depot(session, depot_id)
    a = _analysis(session, [d], date.today())
    txs = sorted(_transactions(session, [d.id]), key=lambda t: (t.trade_date, t.id), reverse=True)
    return templates.TemplateResponse(request, "depots/detail.html", dict(
        d=d, a=a, s=a["summaries"][0], txs=txs, securities=_securities(session),
        fc=_forecast_context(session, a, request.query_params, d.id), today=date.today().isoformat(),
        **_msg(request),
    ))


@router.post("/{depot_id:int}/edit")
async def edit_depot(request: Request, depot_id: int, session: Session = Depends(get_session)):
    d = _get_depot(session, depot_id)
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return _back(f"/depots/{d.id}", "Bitte einen Namen angeben.")
    try:
        d.monthly_savings_cent = parse_flexible_amount(form.get("monthly_savings") or "0")
    except ValueError:
        return _back(f"/depots/{d.id}", "Sparrate: bitte einen Betrag eingeben.")
    d.name = name
    d.broker = (form.get("broker") or "").strip() or None
    d.note = (form.get("note") or "").strip() or None
    session.commit()
    return _back(f"/depots/{d.id}", ok="Gespeichert.")


@router.post("/{depot_id:int}/deactivate")
def deactivate_depot(depot_id: int, session: Session = Depends(get_session)):
    d = _get_depot(session, depot_id)
    d.is_active = False
    session.commit()
    return _back("/depots", ok=f"Depot „{d.name}“ ausgeblendet. Transaktionen bleiben erhalten.")


def _get_or_create_security(session: Session, form) -> Security:
    sid = form.get("security_id")
    if sid and sid != "new":
        sec = session.get(Security, int(sid))
        if sec is None:
            raise ValueError("Wertpapier nicht gefunden")
        return sec
    isin = _check_isin(form.get("isin"))
    existing = session.execute(select(Security).where(Security.isin == isin)).scalar_one_or_none()
    if existing:
        return existing
    sec = Security(
        isin=isin,
        name=(form.get("security_name") or "").strip() or isin,
        asset_class=AssetClass(form.get("asset_class") or "EQUITIES"),
        region=(form.get("region") or "").strip() or "Welt",
        ter=_parse_ratio(form.get("ter") or "0", 0, 10),
        is_fund=bool(form.get("is_fund")),
        partial_exemption=float(form.get("partial_exemption") or 0.3) if form.get("is_fund") else 0.0,
    )
    session.add(sec)
    session.flush()
    return sec


@router.post("/{depot_id:int}/transactions")
async def add_transaction(request: Request, depot_id: int, session: Session = Depends(get_session)):
    d = _get_depot(session, depot_id)
    form = await request.form()
    try:
        kind = TransactionKind(form.get("kind"))
        trade_date = date.fromisoformat(form.get("trade_date") or "")
        sec = _get_or_create_security(session, form)
        if kind == TransactionKind.DIVIDEND:
            qty, amount, fee = 0.0, parse_flexible_amount(form.get("amount") or ""), 0
        else:
            qty = _parse_quantity(form.get("quantity") or "")
            amount = _amount_from(qty, form.get("price") or "")
            fee = parse_flexible_amount(form.get("fee") or "0")
        if kind == TransactionKind.SELL:
            held = calc.holdings(_transactions(session, [d.id]), trade_date).get(sec.id)
            if not held or held.quantity + 1e-9 < qty:
                raise ValueError(f"Verkauf von {_num(qty)} Stück, im Bestand am {_dt(trade_date)} aber nur "
                                 f"{_num(held.quantity if held else 0)}.")
        if amount < 0 or fee < 0:
            raise ValueError("Beträge dürfen nicht negativ sein.")
    except (ValueError, InvalidOperation, ArithmeticError) as exc:
        session.rollback()
        return _back(f"/depots/{d.id}", f"Transaktion nicht gespeichert: {exc}")
    session.add(DepotTransaction(depot_id=d.id, security_id=sec.id, trade_date=trade_date, kind=kind,
                                 quantity=qty, amount_cent=amount, fee_cent=fee,
                                 note=(form.get("note") or "").strip() or None))
    session.commit()
    return _back(f"/depots/{d.id}#transactions", ok=f"{KIND_LABELS[kind]} gebucht: {sec.name}.")


@router.post("/transactions/{tx_id:int}/delete")
def delete_transaction(tx_id: int, session: Session = Depends(get_session)):
    t = session.get(DepotTransaction, tx_id)
    if t is None:
        raise HTTPException(status_code=404)
    depot_id = t.depot_id
    session.delete(t)
    session.commit()
    return _back(f"/depots/{depot_id}#transactions", ok="Transaktion gelöscht.")


@router.post("/transactions/{tx_id:int}/edit")
async def edit_transaction(request: Request, tx_id: int, session: Session = Depends(get_session)):
    """Korrektur einer Buchung, z. B. des Einstands einer Einlieferung (Depotübertrag)."""
    t = session.get(DepotTransaction, tx_id)
    if t is None:
        raise HTTPException(status_code=404)
    form = await request.form()
    try:
        t.trade_date = date.fromisoformat(form.get("trade_date") or "")
        if t.kind != TransactionKind.DIVIDEND:
            t.quantity = _parse_quantity(form.get("quantity") or "")
            t.fee_cent = parse_flexible_amount(form.get("fee") or "0")
        old_amount = t.amount_cent
        t.amount_cent = parse_flexible_amount(form.get("amount") or "")
        t.note = (form.get("note") or "").strip() or None
        if t.note and t.note.startswith("Einlieferung –") and t.amount_cent != old_amount:
            t.note = "Einlieferung (Depotübertrag)"
        if t.amount_cent < 0 or t.fee_cent < 0:
            raise ValueError("Beträge dürfen nicht negativ sein.")
    except (ValueError, InvalidOperation, ArithmeticError) as exc:
        session.rollback()
        return _back(f"/depots/{t.depot_id}#transactions", f"Nicht gespeichert: {exc}")
    session.commit()
    return _back(f"/depots/{t.depot_id}#transactions", ok="Buchung geändert.")


# --------------------------------------------------------------------------- Wertpapiere


@router.get("/securities")
def securities(request: Request, session: Session = Depends(get_session)):
    used = set(session.execute(select(DepotTransaction.security_id).distinct()).scalars())
    counts: dict[int, int] = defaultdict(int)
    for p in _prices(session):
        counts[p.security_id] += 1
    return templates.TemplateResponse(request, "depots/securities.html", dict(
        securities=_securities(session), used=used, price_counts=counts,
        classes=calc.SECURITY_CLASSES, **_msg(request),
    ))


@router.post("/securities")
async def save_security(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    try:
        isin = _check_isin(form.get("isin"))
        sec = session.get(Security, int(form["id"])) if form.get("id") else Security()
        clash = session.execute(select(Security).where(Security.isin == isin)).scalar_one_or_none()
        if clash is not None and clash is not sec:
            raise ValueError(f"ISIN {isin} ist schon als „{clash.name}“ angelegt")
        sec.isin = isin
        sec.name = (form.get("name") or "").strip() or isin
        sec.asset_class = AssetClass(form.get("asset_class"))
        sec.region = (form.get("region") or "").strip() or "Welt"
        sec.ter = _parse_ratio(form.get("ter") or "0", 0, 10)
        sec.is_fund = bool(form.get("is_fund"))
        sec.partial_exemption = float(form.get("partial_exemption") or 0) if sec.is_fund else 0.0
        if sec.id is None:
            session.add(sec)
        session.commit()
    except (ValueError, KeyError) as exc:
        session.rollback()
        return _back("/depots/securities", f"Nicht gespeichert: {exc}")
    return _back("/depots/securities", ok=f"„{sec.name}“ gespeichert.")


@router.post("/securities/{security_id:int}/delete")
def delete_security(security_id: int, session: Session = Depends(get_session)):
    used = session.execute(
        select(DepotTransaction.id).where(DepotTransaction.security_id == security_id).limit(1)
    ).first()
    if used:
        return _back("/depots/securities", "Wertpapier hat Transaktionen und kann nicht gelöscht werden.")
    sec = session.get(Security, security_id)
    if sec:
        session.delete(sec)
        session.commit()
    return _back("/depots/securities", ok="Gelöscht.")


# --------------------------------------------------------------------------- Kurse


@router.get("/prices")
def prices(request: Request, price_date: str | None = None, session: Session = Depends(get_session)):
    target = date.fromisoformat(price_date) if price_date else date.today()
    txs = _transactions(session)
    book = calc.PriceBook(_prices(session), txs)
    held = {sid for sid, h in calc.holdings(txs).items() if h.quantity > 0}
    stored = defaultdict(dict)
    for p in _prices(session):
        stored[p.security_id][p.price_date] = p.price_cent
    rows = []
    for sec in _securities(session):
        if sec.id not in held:
            continue
        last, last_date = book.at(sec.id, target)
        rows.append(dict(security=sec, last=last, last_date=last_date,
                         existing=stored[sec.id].get(target)))
    history_dates = sorted({d for m in stored.values() for d in m}, reverse=True)[:8]
    return templates.TemplateResponse(request, "depots/prices.html", dict(
        rows=rows, target=target.isoformat(), history_dates=history_dates, stored=stored,
        securities={s.id: s for s in _securities(session)}, **_msg(request),
    ))


@router.post("/prices")
async def save_prices(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    try:
        target = date.fromisoformat(form.get("price_date") or "")
    except ValueError:
        return _back("/depots/prices", "Bitte ein gültiges Datum angeben.")
    n, errors = 0, []
    for key, raw in form.multi_items():
        if not key.startswith("price_") or key == "price_date" or not str(raw).strip():
            continue
        sid = int(key.removeprefix("price_"))
        try:
            cent = parse_flexible_amount(str(raw))
        except ValueError:
            errors.append(str(raw))
            continue
        row = session.get(SecurityPrice, (sid, target))
        if row:
            row.price_cent = cent
        else:
            session.add(SecurityPrice(security_id=sid, price_date=target, price_cent=cent))
        n += 1
    session.commit()
    url = f"/depots/prices?price_date={target.isoformat()}"
    if errors:
        return _back(url, f"{n} Kurs(e) gespeichert, nicht lesbar: {', '.join(errors)}")
    return _back(url, ok=f"{n} Kurs(e) zum {_dt(target)} gespeichert.")


# --------------------------------------------------------------------------- Annahmen


@router.get("/assumptions")
def assumptions(request: Request, session: Session = Depends(get_session)):
    s = _settings(session)
    return templates.TemplateResponse(request, "depots/assumptions.html", dict(
        s=s, profile=_profile(session), assumptions=_assumptions(session), defaults=calc.DEFAULT_ASSUMPTIONS,
        classes=calc.SECURITY_CLASSES, tax_rate=calc.capital_gains_tax_rate(s.church_tax_rate), **_msg(request),
    ))


@router.post("/assumptions")
async def save_assumptions(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    s = _settings(session)
    if form.get("reset"):
        for row in session.execute(select(ReturnAssumption)).scalars():
            session.delete(row)
        session.commit()
        return _back("/depots/assumptions", ok="Rendite-Annahmen auf Standard zurückgesetzt.")
    try:
        s.church_tax_rate = float(form.get("church_tax_rate") or 0)
        s.saver_allowance_cent = parse_flexible_amount(form.get("saver_allowance") or "0")
        s.base_rate = _parse_ratio(form.get("base_rate") or "0", -5, 20)
        s.base_rate_year = int(form.get("base_rate_year") or date.today().year)
        s.mc_paths = int(max(200, min(int(float(form.get("mc_paths") or 2000)), 20000)))
        s.ter_warn = _parse_ratio(form.get("ter_warn") or "0,5", 0, 10)
        target = (form.get("equity_target") or "").strip()
        s.equity_target = _parse_ratio(target) if target else None
        s.equity_band = _parse_ratio(form.get("equity_band") or "5")
        for cls in calc.SECURITY_CLASSES:
            r, v = form.get(f"ret_{cls.value}"), form.get(f"vol_{cls.value}")
            if r is None or v is None:
                continue
            row = session.get(ReturnAssumption, cls) or ReturnAssumption(asset_class=cls)
            row.real_return = _parse_ratio(r, -20, 30)
            row.volatility = _parse_ratio(v, 0, 200)
            session.add(row)
        session.commit()
    except (ValueError, TypeError) as exc:
        session.rollback()
        return _back("/depots/assumptions", f"Nicht gespeichert: {exc}")
    return _back("/depots/assumptions", ok="Gespeichert.")


# --------------------------------------------------------------------------- Stichtag übernehmen


def _sync_rows(session: Session, target: date) -> list[dict]:
    rows = []
    positions = list(session.execute(
        select(Position).where(Position.is_active.is_(True), Position.kind == PositionKind.ASSET)
        .order_by(Position.name)
    ).scalars())
    links = {(l.depot_id, l.asset_class): l.position_id for l in session.execute(select(DepotPositionLink)).scalars()}
    prices = _prices(session)
    for d in _active_depots(session):
        txs = _transactions(session, [d.id])
        book = calc.PriceBook(prices, txs)
        values = calc.value_by_class_cent(txs, book, target)
        classes = sorted(set(values) | {c for (did, c) in links if did == d.id},
                         key=lambda c: calc.SECURITY_CLASSES.index(c))
        for cls in classes:
            pid = links.get((d.id, cls))
            rows.append(dict(
                depot=d, asset_class=cls, value_cent=values.get(cls, 0), linked_id=pid,
                candidates=[p for p in positions if p.asset_class == cls],
                new_name=f"{d.name} – {calc.ASSET_CLASS_LABELS[cls]}",
                txs=[t for t in txs if t.security.asset_class == cls],
            ))
    return rows


def _previous_snapshot(session: Session, position_id: int, before: date) -> Snapshot | None:
    return session.execute(
        select(Snapshot).where(Snapshot.position_id == position_id, Snapshot.snapshot_date < before)
        .order_by(Snapshot.snapshot_date.desc()).limit(1)
    ).scalar_one_or_none()


@router.get("/sync")
def sync_form(request: Request, snapshot_date: str | None = None, session: Session = Depends(get_session)):
    target = date.fromisoformat(snapshot_date) if snapshot_date else due_year_end(session)
    rows = _sync_rows(session, target)
    for r in rows:
        pid = r["linked_id"]
        prev = _previous_snapshot(session, pid, target) if pid else None
        r["contribution_cent"] = calc.net_contribution_cent(r["txs"], prev.snapshot_date, target) if prev else 0
        r["prev"] = prev
        r["existing"] = session.execute(
            select(Snapshot).where(Snapshot.position_id == pid, Snapshot.snapshot_date == target)
        ).scalar_one_or_none() if pid else None
    return templates.TemplateResponse(request, "depots/sync.html", dict(
        rows=rows, target=target.isoformat(), future=target > date.today(), **_msg(request),
    ))


@router.post("/sync")
async def sync_save(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    try:
        target = date.fromisoformat(form.get("snapshot_date") or "")
    except ValueError:
        return _back("/depots/sync", "Bitte ein gültiges Datum angeben.")
    grouped: dict[int, dict] = {}
    for r in _sync_rows(session, target):
        key = f"target_{r['depot'].id}_{r['asset_class'].value}"
        choice = form.get(key, "skip")
        if choice == "skip":
            continue
        if choice == "new":
            pos = session.execute(select(Position).where(Position.name == r["new_name"])).scalar_one_or_none()
            if pos is None:
                pos = Position(name=r["new_name"], kind=PositionKind.ASSET, asset_class=r["asset_class"],
                               is_liquid=r["asset_class"] == AssetClass.LIQUIDITY, retirement_relevant=True)
                session.add(pos)
                session.flush()
        else:
            pos = session.get(Position, int(choice))
            if pos is None or pos.kind != PositionKind.ASSET:
                continue
        link = session.get(DepotPositionLink, (r["depot"].id, r["asset_class"]))
        if link:
            link.position_id = pos.id
        else:
            session.add(DepotPositionLink(depot_id=r["depot"].id, asset_class=r["asset_class"], position_id=pos.id))
        g = grouped.setdefault(pos.id, dict(position=pos, value=0, txs=[]))
        g["value"] += r["value_cent"]
        g["txs"] += r["txs"]
    for pid, g in grouped.items():
        prev = _previous_snapshot(session, pid, target)
        contribution = calc.net_contribution_cent(g["txs"], prev.snapshot_date, target) if prev else 0
        snap = session.execute(
            select(Snapshot).where(Snapshot.position_id == pid, Snapshot.snapshot_date == target)
        ).scalar_one_or_none()
        if snap:
            snap.value_cent, snap.net_contribution_cent = g["value"], contribution
        else:
            session.add(Snapshot(snapshot_date=target, position_id=pid, value_cent=g["value"],
                                 net_contribution_cent=contribution))
    session.commit()
    return _back(f"/depots/sync?snapshot_date={target.isoformat()}",
                 ok=f"{len(grouped)} Position(en) zum {_dt(target)} übernommen.")


# --------------------------------------------------------------------------- Import/Export

TX_FIELDS = ["datum", "depot", "isin", "name", "typ", "stueck", "kurs", "gebuehren", "betrag", "notiz"]
PRICE_FIELDS = ["datum", "isin", "kurs"]


def import_csv(session: Session, text: str) -> tuple[int, list[str]]:
    """Importiert Transaktionen oder Kurse; das Format wird an den Spaltenköpfen erkannt.

    Transaktionen: betrag ist bei Käufen/Verkäufen optional (sonst Stück × Kurs),
    bei Ausschüttungen Pflicht (brutto, vor Steuern). Fehlerhafte Zeilen werden übersprungen und gemeldet.
    """
    text = text.lstrip("﻿")
    head = text.split("\n", 1)[0]
    reader = csv.DictReader(io.StringIO(text), delimiter=";" if head.count(";") >= head.count(",") else ",")
    fields = {f.strip().lower() for f in (reader.fieldnames or []) if f}
    is_tx = {"datum", "depot", "isin", "typ"} <= fields
    is_price = not is_tx and {"datum", "isin", "kurs"} <= fields
    if not (is_tx or is_price):
        return 0, ["Spalten nicht erkannt. Erwartet: " + ";".join(TX_FIELDS) + " oder " + ";".join(PRICE_FIELDS)]
    n, errors = 0, []
    for line, raw in enumerate(reader, start=2):
        row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        try:
            with session.begin_nested():
                isin = row["isin"].upper()
                sec = session.execute(select(Security).where(Security.isin == isin)).scalar_one_or_none()
                if sec is None:
                    if len(isin) != 12:
                        raise ValueError(f"ungültige ISIN {isin!r}")
                    sec = Security(isin=isin, name=row.get("name") or isin)
                    session.add(sec)
                    session.flush()
                d = date.fromisoformat(row["datum"])
                if is_price:
                    cent = parse_flexible_amount(row["kurs"])
                    p = session.get(SecurityPrice, (sec.id, d))
                    if p:
                        p.price_cent = cent
                    else:
                        session.add(SecurityPrice(security_id=sec.id, price_date=d, price_cent=cent))
                else:
                    kind = KIND_CSV.get(row["typ"].lower())
                    if kind is None:
                        raise ValueError(f"unbekannter Typ {row['typ']!r} (kauf/verkauf/dividende)")
                    depot = session.execute(select(Depot).where(Depot.name == row["depot"])).scalar_one_or_none()
                    if depot is None:
                        depot = Depot(name=row["depot"])
                        session.add(depot)
                        session.flush()
                    fee = parse_flexible_amount(row.get("gebuehren") or "0")
                    if kind == TransactionKind.DIVIDEND:
                        qty, amount, fee = 0.0, parse_flexible_amount(row["betrag"]), 0
                    else:
                        qty = _parse_quantity(row["stueck"])
                        amount = (parse_flexible_amount(row["betrag"]) if row.get("betrag")
                                  else _amount_from(qty, row["kurs"]))
                    session.add(DepotTransaction(depot_id=depot.id, security_id=sec.id, trade_date=d, kind=kind,
                                                 quantity=qty, amount_cent=amount, fee_cent=fee,
                                                 note=row.get("notiz") or None))
                    session.flush()
            n += 1
        except (ValueError, KeyError, InvalidOperation, ArithmeticError) as exc:
            errors.append(f"Zeile {line}: {exc}")
    session.commit()
    return n, errors


def import_broker(session: Session, parsed: ParsedImport, depot: Depot) -> dict:
    """Übernimmt einen geparsten Broker-Export in ein Depot. Bereits importierte Buchungen
    (gleiche Broker-ID) werden übersprungen, sodass derselbe Export mehrfach geladen werden kann."""
    known = set(session.execute(
        select(DepotTransaction.external_id).where(DepotTransaction.external_id.is_not(None))
    ).scalars())
    secs = {s.isin: s for s in session.execute(select(Security)).scalars()}
    new_secs, estimated, added, duplicates = [], [], 0, 0
    for t in parsed.transactions:
        if t.external_id in known:
            duplicates += 1
            continue
        sec = secs.get(t.isin)
        if sec is None:
            info = parsed.securities.get(t.isin)
            sec = Security(isin=t.isin, name=info.name if info else t.isin,
                           asset_class=info.asset_class if info else AssetClass.EQUITIES,
                           region=(info.region if info else "") or "Welt",
                           is_fund=info.is_fund if info else True,
                           partial_exemption=info.partial_exemption if info else 0.3, ter=0.0)
            session.add(sec)
            session.flush()
            secs[t.isin] = sec
            new_secs.append(sec)
        session.add(DepotTransaction(
            depot_id=depot.id, security_id=sec.id, trade_date=t.trade_date, kind=t.kind,
            quantity=t.quantity, amount_cent=t.amount_cent, fee_cent=t.fee_cent, tax_cent=t.tax_cent,
            note=t.note, external_id=t.external_id))
        known.add(t.external_id)
        added += 1
        if t.estimated:
            estimated.append(t)
    session.commit()
    return dict(broker=parsed.broker, depot=depot, added=added, duplicates=duplicates, new_securities=new_secs,
                estimated=estimated, securities=secs, cash_balance_cent=parsed.cash_balance_cent,
                first_date=parsed.first_date, last_date=parsed.last_date, ignored=parsed.ignored,
                problems=parsed.problems)


def _import_context(session: Session, **extra) -> dict:
    ctx = dict(tx_fields=TX_FIELDS, price_fields=PRICE_FIELDS, result=None, broker_result=None,
               depots=_active_depots(session))
    ctx.update(extra)
    return ctx


@router.get("/import")
def import_page(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "depots/import.html", _import_context(session))


@router.post("/import")
async def import_upload(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    upload = form.get("file")
    raw = await upload.read() if upload is not None and hasattr(upload, "read") else b""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")  # Excel unter Windows/macOS speichert CSV oft so
    header = next(csv.reader(io.StringIO(text.lstrip("\ufeff"))), [])
    if is_trade_republic(header):
        target = form.get("depot_id") or "new"
        if target == "new":
            name = (form.get("new_depot_name") or "").strip() or "Trade Republic"
            depot = session.execute(select(Depot).where(Depot.name == name)).scalar_one_or_none()
            if depot is None:
                depot = Depot(name=name, broker="Trade Republic")
                session.add(depot)
                session.flush()
        else:
            depot = _get_depot(session, int(target))
        result = import_broker(session, parse_trade_republic(text), depot)
        return templates.TemplateResponse(request, "depots/import.html", _import_context(session, broker_result=result))
    n, errors = import_csv(session, text)
    return templates.TemplateResponse(request, "depots/import.html",
                                      _import_context(session, result=dict(n=n, errors=errors)))


def _csv_response(rows: list[list], name: str) -> Response:
    buf = io.StringIO()
    csv.writer(buf, delimiter=";").writerows(rows)
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _plain(cent: int) -> str:
    return format_euro(cent).removesuffix(" €").replace(".", "")


@router.get("/export/transactions.csv")
def export_transactions(session: Session = Depends(get_session)):
    rows = [TX_FIELDS]
    names = {d.id: d.name for d in session.execute(select(Depot)).scalars()}
    inv = {v: k for k, v in KIND_CSV.items() if k in ("kauf", "verkauf", "dividende")}
    for t in _transactions(session):
        price = _plain(round(t.amount_cent / t.quantity)) if t.quantity else ""
        rows.append([t.trade_date.isoformat(), names[t.depot_id], t.security.isin, t.security.name, inv[t.kind],
                     _num(t.quantity, 6) if t.quantity else "", price, _plain(t.fee_cent),
                     _plain(t.amount_cent), t.note or ""])
    return _csv_response(rows, "depot_transaktionen.csv")


@router.get("/export/prices.csv")
def export_prices(session: Session = Depends(get_session)):
    secs = {s.id: s for s in _securities(session)}
    rows = [PRICE_FIELDS] + [[p.price_date.isoformat(), secs[p.security_id].isin, _plain(p.price_cent)]
                             for p in sorted(_prices(session), key=lambda p: (p.price_date, p.security_id))]
    return _csv_response(rows, "depot_kurse.csv")


@router.get("/api/summary.json")
def api_summary(session: Session = Depends(get_session)):
    a = _analysis(session, _active_depots(session), date.today())
    return JSONResponse(dict(
        as_of=a["as_of"].isoformat(),
        value_cent=a["total_cent"],
        net_invested_cent=a["net_invested_cent"],
        gain_cent=a["gain_cent"],
        xirr=a["xirr"],
        latent_tax_cent=a["latent_tax_cent"],
        after_tax_cent=a["after_tax_cent"],
        equity_share=a["equity_share"],
        ter=a["ter"],
        monthly_savings_cent=a["savings_cent"],
        by_class={c.value: v for c, v in a["by_class"]},
        hints=[dict(level=h.level, title=h.title, text=h.text) for h in a["hints"]],
        depots=[dict(id=s.depot.id, name=s.depot.name, value_cent=s.value_cent, xirr=s.xirr) for s in a["summaries"]],
    ))
