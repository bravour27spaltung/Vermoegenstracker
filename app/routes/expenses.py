"""Wiederkehrende Ausgaben nach Kategorie (Miete, Versicherungen, Abos, ...).

Getrennt von ExpenseRecord (der einzigen Jahressumme, die die Liquiditätsquote im
Jahresupdate speist): hier geht es um die Entwicklung einzelner Kategorien über die Zeit,
damit sich z. B. eine steigende Versicherungsprämie erkennen lässt. Gleiches Muster wie
Position/Snapshot: eine Kategorie wird einmal angelegt, ihr Betrag dann zu beliebig vielen
Stichtagen erfasst.
"""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.deps import get_session
from app.models import ExpenseCategory, ExpenseCategoryRecord
from app.money import format_euro, parse_flexible_amount
from app.svg_charts import line_svg

router = APIRouter(prefix="/expenses", tags=["expenses"])
templates = Jinja2Templates(directory="app/templates")
try:
    # Gleiche Formatierung (eur, pct, dt, ...) wie die übrigen Seiten.
    from app.routes import depots as _depots
    templates.env.filters.update(_depots.templates.env.filters)
except ImportError:
    pass


def _quarter_end(d: date) -> date:
    """Nächstes Quartalsende (31.3./30.6./30.9./31.12.) ab dem gegebenen Datum."""
    quarter_month = ((d.month - 1) // 3 + 1) * 3
    if quarter_month == 12:
        return date(d.year, 12, 31)
    return date(d.year, quarter_month + 1, 1) - timedelta(days=1)


def _redirect(ok: str | None = None, error: str | None = None) -> RedirectResponse:
    q = urlencode({k: v for k, v in (("ok", ok), ("error", error)) if v})
    return RedirectResponse(url="/expenses" + (f"?{q}" if q else ""), status_code=303)


def _name_taken(session: Session, name: str, exclude_id: int | None = None) -> ExpenseCategory | None:
    found = session.execute(
        select(ExpenseCategory).where(ExpenseCategory.name == name)
    ).scalar_one_or_none()
    return found if found is not None and found.id != exclude_id else None


def _active_categories(session: Session) -> list[ExpenseCategory]:
    return list(
        session.execute(
            select(ExpenseCategory).where(ExpenseCategory.is_active.is_(True)).order_by(ExpenseCategory.name)
        ).scalars()
    )


def _latest_record(session: Session, category_id: int) -> ExpenseCategoryRecord | None:
    return session.execute(
        select(ExpenseCategoryRecord)
        .where(ExpenseCategoryRecord.category_id == category_id)
        .order_by(ExpenseCategoryRecord.snapshot_date.desc())
        .limit(1)
    ).scalar_one_or_none()


def _totals_by_date(session: Session) -> list[tuple[date, int]]:
    """Summe aller Kategorien je Stichtag, aufsteigend – Grundlage für den Verlauf."""
    rows = session.execute(
        select(ExpenseCategoryRecord.snapshot_date, ExpenseCategoryRecord.monthly_amount_cent)
    ).all()
    totals: dict[date, int] = {}
    for d, cent in rows:
        totals[d] = totals.get(d, 0) + cent
    return sorted(totals.items())


@router.get("")
def overview(request: Request, session: Session = Depends(get_session)):
    categories = _active_categories(session)
    hidden = list(
        session.execute(
            select(ExpenseCategory).where(ExpenseCategory.is_active.is_(False)).order_by(ExpenseCategory.name)
        ).scalars()
    )

    latest = {cat.id: _latest_record(session, cat.id) for cat in categories}
    rows = [
        {
            "category": cat,
            "latest": format_euro(latest[cat.id].monthly_amount_cent) if latest[cat.id] else "–",
            "latest_date": latest[cat.id].snapshot_date.strftime("%d.%m.%Y") if latest[cat.id] else None,
        }
        for cat in categories
    ]
    known = [r for r in latest.values() if r is not None]
    total_latest = format_euro(sum(r.monthly_amount_cent for r in known)) if known else "–"

    totals = _totals_by_date(session)
    chart_svg = line_svg(totals, label="Ausgaben pro Monat")

    return templates.TemplateResponse(
        request,
        "expenses.html",
        {
            "rows": rows,
            "hidden": hidden,
            "total_latest": total_latest,
            "chart_svg": chart_svg,
            "ok": request.query_params.get("ok"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("")
def create_category(name: str = Form(...), session: Session = Depends(get_session)):
    name = name.strip()
    if not name:
        return _redirect(error="Bitte einen Namen eingeben.")
    taken = _name_taken(session, name)
    if taken is not None:
        hint = " – sie ist ausgeblendet und lässt sich unten wieder einblenden" if not taken.is_active else ""
        return _redirect(error=f"Eine Kategorie „{name}“ gibt es schon{hint}.")
    session.add(ExpenseCategory(name=name))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return _redirect(error=f"Eine Kategorie „{name}“ gibt es schon.")
    return _redirect(ok=f"„{name}“ angelegt. Betrag trägst du unten unter „Stichtag erfassen“ ein.")


@router.post("/{category_id}/deactivate")
def deactivate_category(category_id: int, session: Session = Depends(get_session)):
    category = session.get(ExpenseCategory, category_id)
    if category is None:
        return _redirect(error="Kategorie nicht gefunden.")
    category.is_active = False
    session.commit()
    return _redirect(ok=f"„{category.name}“ ausgeblendet.")


@router.post("/{category_id}/activate")
def activate_category(category_id: int, session: Session = Depends(get_session)):
    category = session.get(ExpenseCategory, category_id)
    if category is None:
        return _redirect(error="Kategorie nicht gefunden.")
    category.is_active = True
    session.commit()
    return _redirect(ok=f"„{category.name}“ wieder eingeblendet.")


def _rows(session: Session, target_date: date, submitted=None, errors=None):
    submitted, errors = submitted or {}, errors or {}
    rows = []
    for category in _active_categories(session):
        last = session.execute(
            select(ExpenseCategoryRecord)
            .where(ExpenseCategoryRecord.category_id == category.id, ExpenseCategoryRecord.snapshot_date < target_date)
            .order_by(ExpenseCategoryRecord.snapshot_date.desc())
            .limit(1)
        ).scalar_one_or_none()
        existing = session.execute(
            select(ExpenseCategoryRecord).where(
                ExpenseCategoryRecord.category_id == category.id,
                ExpenseCategoryRecord.snapshot_date == target_date,
            )
        ).scalar_one_or_none()

        if existing is not None:
            default = format_euro(existing.monthly_amount_cent)
        elif last is not None:
            default = format_euro(last.monthly_amount_cent)
        else:
            default = "0,00 €"

        key = f"amount_{category.id}"
        rows.append(
            {
                "category": category,
                "previous": format_euro(last.monthly_amount_cent) if last else "–",
                "default": submitted.get(key, default),
                "error": errors.get(key),
            }
        )
    return rows


@router.get("/erfassen")
def new_record_form(request: Request, snapshot_date: str | None = None, session: Session = Depends(get_session)):
    target_date = date.fromisoformat(snapshot_date) if snapshot_date else _quarter_end(date.today())
    return templates.TemplateResponse(
        request,
        "expense_form.html",
        {
            "target_date": target_date.isoformat(),
            "target_label": target_date.strftime("%d.%m.%Y"),
            "rows": _rows(session, target_date),
            "error": None,
        },
    )


@router.post("/erfassen")
async def save_records(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    target_date = date.fromisoformat(form["snapshot_date"])
    submitted = {k: str(v) for k, v in form.multi_items()}

    parsed: dict[int, int] = {}
    errors: dict[str, str] = {}
    for key, raw_value in form.multi_items():
        if not key.startswith("amount_"):
            continue
        category_id = int(key.removeprefix("amount_"))
        try:
            parsed[category_id] = parse_flexible_amount(str(raw_value))
        except ValueError:
            errors[key] = "Bitte einen Betrag eingeben, z. B. 45,90"

    if errors:
        return templates.TemplateResponse(
            request,
            "expense_form.html",
            {
                "target_date": target_date.isoformat(),
                "target_label": target_date.strftime("%d.%m.%Y"),
                "rows": _rows(session, target_date, submitted, errors),
                "error": "Nichts gespeichert – bitte die markierten Felder prüfen.",
            },
        )

    for category_id, amount_cent in parsed.items():
        existing = session.execute(
            select(ExpenseCategoryRecord).where(
                ExpenseCategoryRecord.category_id == category_id,
                ExpenseCategoryRecord.snapshot_date == target_date,
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.monthly_amount_cent = amount_cent
        else:
            session.add(
                ExpenseCategoryRecord(
                    category_id=category_id, snapshot_date=target_date, monthly_amount_cent=amount_cent
                )
            )
    session.commit()
    return RedirectResponse(url="/expenses", status_code=303)
