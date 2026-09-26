"""Jahresupdate: einmal im Jahr zum 31.12. alles auf Stand bringen – als Checkliste.

Depots brauchen nur noch Kurse zum Stichtag; Wert und Einzahlungen berechnet die App.
Die übrigen Positionen (Konten, Immobilie, Kredite) werden im Stichtagsformular erfasst.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.deps import get_session
from app.models import ExpenseRecord
from app.money import format_euro, parse_flexible_amount
from app.overview import build_overview, due_year_end, year_end, year_steps
from app.routes.depots import templates as depot_templates

router = APIRouter(prefix="/jahresupdate", tags=["jahresupdate"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters.update(depot_templates.env.filters)


@router.get("")
def year_update(request: Request, jahr: int | None = None, session: Session = Depends(get_session)):
    target = year_end(jahr) if jahr else due_year_end(session)
    steps = year_steps(session, target)
    required = [s for s in steps if not s.optional]
    record = session.get(ExpenseRecord, target)
    ov = build_overview(session)
    return templates.TemplateResponse(request, "yearly.html", dict(
        target=target, steps=steps, done=sum(s.done for s in required), total=len(required),
        future=target > date.today(), years=list(range(date.today().year, date.today().year - 6, -1)),
        expenses=format_euro(record.monthly_expenses_cent).removesuffix(" €") if record else "",
        expenses_hint=format_euro(ov.monthly_expenses_cent), ok=request.query_params.get("ok"),
        error=request.query_params.get("error"),
    ))


@router.post("/ausgaben")
async def save_expenses(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    target = date.fromisoformat(form["snapshot_date"])
    try:
        cent = parse_flexible_amount(form.get("expenses") or "")
    except ValueError:
        return RedirectResponse(f"/jahresupdate?jahr={target.year}&error=Bitte+einen+Betrag+eingeben", status_code=303)
    record = session.get(ExpenseRecord, target)
    if record:
        record.monthly_expenses_cent = cent
    else:
        session.add(ExpenseRecord(snapshot_date=target, monthly_expenses_cent=cent))
    session.commit()
    return RedirectResponse(f"/jahresupdate?jahr={target.year}&ok=Ausgaben+gespeichert", status_code=303)
