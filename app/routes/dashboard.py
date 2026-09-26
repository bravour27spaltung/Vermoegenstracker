"""Dashboard: Gesamtvermögen aus Positionen (Stichtage) und Depots (live) – siehe app/overview.py."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.deps import get_session
from app.overview import CLASS_LABELS, build_overview, due_year_end, year_steps
from app.routes.depots import templates as depot_templates
from app.svg_charts import line_svg

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters.update(depot_templates.env.filters)  # gleiche Formatierung wie die Depotseiten
templates.env.globals.update(CLASS_LABEL=lambda c: CLASS_LABELS.get(c, "–"))


@router.get("")
def dashboard(request: Request, session: Session = Depends(get_session)):
    ov = build_overview(session)
    due = due_year_end(session, ov.today)
    steps = year_steps(session, due)
    required = [s for s in steps if not s.optional]
    history = []
    prev = None
    for d, v in ov.series:
        history.append(dict(date=d, value=v, change=None if prev is None else v - prev,
                            is_today=d == ov.today))
        prev = v
    return templates.TemplateResponse(request, "dashboard.html", dict(
        ov=ov, due=due, steps_done=sum(s.done for s in required), steps_total=len(required),
        due_past=due < date.today(),
        chart_svg=line_svg(ov.series), history=list(reversed(history)),
        assets=sorted([r for r in ov.rows if not r.is_liability], key=lambda r: -r.value_cent),
        liabilities=[r for r in ov.rows if r.is_liability],
    ))
