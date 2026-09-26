"""Stichtag erfassen: eine Seite für alle von Hand gepflegten Positionen auf einmal.

Standard ist der Jahresstichtag 31.12. (siehe app/overview.due_year_end). Depots und mit
ihnen verknüpfte Positionen erscheinen nur zur Info – ihre Werte berechnet die App.

Vorbelegt wird mit dem jeweils letzten bekannten Wert je Position, damit man nur
ändern muss, was sich wirklich verändert hat.
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_session
from app.models import Position, Snapshot
from app.money import format_euro, parse_flexible_amount
from app.overview import depot_book, due_year_end, manual_positions

router = APIRouter(prefix="/snapshots", tags=["snapshots"])
templates = Jinja2Templates(directory="app/templates")


def _quarter_end(d: date) -> date:
    """Nächstes Quartalsende (31.3./30.6./30.9./31.12.) ab dem gegebenen Datum."""
    quarter_month = ((d.month - 1) // 3 + 1) * 3
    if quarter_month == 12:
        return date(d.year, 12, 31)
    return date(d.year, quarter_month + 1, 1) - timedelta(days=1)


def _rows(session: Session, target_date: date, submitted=None, errors=None):
    """Zeilen für das Formular. Mit `submitted` (Formulardaten nach einem Fehler) bleiben Eingaben erhalten."""
    submitted, errors = submitted or {}, errors or {}
    rows = []
    for position in manual_positions(session):
        last_snapshot = session.execute(
            select(Snapshot)
            .where(Snapshot.position_id == position.id, Snapshot.snapshot_date < target_date)
            .order_by(Snapshot.snapshot_date.desc())
            .limit(1)
        ).scalar_one_or_none()
        existing = session.execute(
            select(Snapshot).where(
                Snapshot.position_id == position.id, Snapshot.snapshot_date == target_date
            )
        ).scalar_one_or_none()

        if existing is not None:
            value_default = format_euro(existing.value_cent)
            contribution_default = format_euro(existing.net_contribution_cent)
        elif last_snapshot is not None:
            value_default = format_euro(last_snapshot.value_cent)
            contribution_default = "0,00 €"
        else:
            value_default = "0,00 €"
            contribution_default = "0,00 €"

        vkey, ckey = f"value_{position.id}", f"contribution_{position.id}"
        rows.append(
            {
                "position": position,
                "previous_value": format_euro(last_snapshot.value_cent) if last_snapshot else "–",
                "previous_cent": last_snapshot.value_cent if last_snapshot else None,
                "has_existing": existing is not None,
                "value_default": submitted.get(vkey, value_default),
                "contribution_default": submitted.get(ckey, contribution_default),
                "value_error": errors.get(vkey),
                "contribution_error": errors.get(ckey),
            }
        )
    return rows


def _render(request: Request, session: Session, target_date: date, rows, error: str | None = None,
            warn_unchanged: bool = False):
    book = depot_book(session)
    depots = [{"name": d.name, "value": format_euro(book.value(target_date, d.id))} for d in book.depots]
    return templates.TemplateResponse(
        request,
        "snapshot_form.html",
        {"target_date": target_date.isoformat(), "target_label": target_date.strftime("%d.%m.%Y"),
         "rows": rows, "depots": depots, "error": error, "warn_unchanged": warn_unchanged},
    )


@router.get("/new")
def new_snapshot_form(
    request: Request,
    snapshot_date: str | None = None,
    session: Session = Depends(get_session),
):
    # Standard: der fällige Jahresstichtag (31.12.); Quartale bleiben über ?snapshot_date= möglich.
    target_date = date.fromisoformat(snapshot_date) if snapshot_date else due_year_end(session)
    return _render(request, session, target_date, _rows(session, target_date))


@router.post("")
async def save_snapshots(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    target_date = date.fromisoformat(form["snapshot_date"])
    submitted = {k: str(v) for k, v in form.multi_items()}

    # Erst alles prüfen, dann schreiben: bei einem Fehler wird nichts gespeichert und die Eingaben bleiben stehen.
    # parse_flexible_amount versteht 1.234,56 ebenso wie 1234,56 und 1234.56 (Punkt + 1–2 Ziffern = Dezimalstelle).
    parsed: dict[int, tuple[int, int]] = {}
    errors: dict[str, str] = {}
    for key, raw_value in form.multi_items():
        if not key.startswith("value_"):
            continue
        position_id = int(key.removeprefix("value_"))
        try:
            value_cent = parse_flexible_amount(str(raw_value))
        except ValueError:
            errors[key] = "Bitte einen Betrag eingeben, z. B. 1.234,56"
            continue
        try:
            contribution_cent = parse_flexible_amount(str(form.get(f"contribution_{position_id}") or "0"))
        except ValueError:
            errors[f"contribution_{position_id}"] = "Bitte einen Betrag eingeben oder 0 lassen"
            continue
        parsed[position_id] = (value_cent, contribution_cent)

    if errors:
        rows = _rows(session, target_date, submitted, errors)
        return _render(request, session, target_date, rows,
                       error="Nichts gespeichert – bitte die markierten Felder prüfen.")

    # Alles unverändert? Dann kurz nachfragen, damit ein versehentliches „Speichern" das Jahr nicht als erledigt markiert.
    if not form.get("confirm_unchanged"):
        rows = _rows(session, target_date)
        known = [r for r in rows if r["previous_cent"] is not None and not r["has_existing"]]
        if known and len(known) == len(rows) and all(
            parsed.get(r["position"].id, (None,))[0] == r["previous_cent"] for r in known
        ):
            return _render(request, session, target_date, _rows(session, target_date, submitted),
                           warn_unchanged=True)

    for position_id, (value_cent, contribution_cent) in parsed.items():
        existing = session.execute(
            select(Snapshot).where(
                Snapshot.position_id == position_id, Snapshot.snapshot_date == target_date
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.value_cent = value_cent
            existing.net_contribution_cent = contribution_cent
        else:
            session.add(
                Snapshot(
                    snapshot_date=target_date,
                    position_id=position_id,
                    value_cent=value_cent,
                    net_contribution_cent=contribution_cent,
                )
            )

    session.commit()
    return RedirectResponse(url="/jahresupdate?jahr=%d" % target_date.year if (target_date.month, target_date.day) == (12, 31)
                            else "/dashboard", status_code=303)
