"""Positionen anlegen, bearbeiten, ausblenden und wieder einblenden.

Positionen werden nie hart gelöscht (Snapshot.position_id hat ondelete='RESTRICT'),
sondern über is_active ausgeblendet und lassen sich wieder einblenden. Der Name ist eindeutig,
auch unter den ausgeblendeten Positionen.

Beim Anlegen wählt man einen Typ aus einer festen Liste (Girokonto, Depot-Aktienanteil, ...)
statt Art und Anlageklasse einzeln zu setzen. Das reduziert die Pflichtfelder auf Name + Typ;
is_liquid/retirement_relevant werden je Typ sinnvoll vorbelegt und lassen sich unter
"Erweiterte Optionen" übersteuern (z. B. eine vermietete Immobilie als ruhestandsrelevant).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.deps import get_session
from app.models import AssetClass, Position, PositionKind
from app.overview import CLASS_LABELS

router = APIRouter(prefix="/positions", tags=["positions"])
templates = Jinja2Templates(directory="app/templates")

# Reihenfolge = Anzeigereihenfolge im Dropdown.
PRESETS: dict[str, dict] = {
    "girokonto": dict(
        label="Girokonto", kind=PositionKind.ASSET, asset_class=AssetClass.LIQUIDITY,
        liquid_default=True, retirement_default=False,
    ),
    "tagesgeld": dict(
        label="Tagesgeld", kind=PositionKind.ASSET, asset_class=AssetClass.LIQUIDITY,
        liquid_default=True, retirement_default=False,
    ),
    "depot_aktien": dict(
        label="Depot – Aktienanteil", kind=PositionKind.ASSET, asset_class=AssetClass.EQUITIES,
        liquid_default=False, retirement_default=True,
    ),
    "depot_anleihen": dict(
        label="Depot – Anleihenanteil", kind=PositionKind.ASSET, asset_class=AssetClass.BONDS,
        liquid_default=False, retirement_default=True,
    ),
    # Nur private Verträge mit Kapitalwert. Gesetzliche Rente und Betriebsrente werden
    # unter /pensions als Anspruch erfasst, nicht als Vermögensposition.
    "rentenversicherung": dict(
        label="Private Rentenversicherung (Riester/Rürup)", kind=PositionKind.ASSET, asset_class=AssetClass.PENSION,
        liquid_default=False, retirement_default=True,
    ),
    "immobilie": dict(
        label="Immobilie", kind=PositionKind.ASSET, asset_class=AssetClass.REAL_ESTATE,
        liquid_default=False, retirement_default=False,
    ),
    "krypto": dict(
        label="Krypto", kind=PositionKind.ASSET, asset_class=AssetClass.CRYPTO,
        liquid_default=False, retirement_default=True,
    ),
    "sonstiges": dict(
        label="Andere Vermögenswerte", kind=PositionKind.ASSET, asset_class=AssetClass.OTHER,
        liquid_default=False, retirement_default=True,
    ),
    "verbindlichkeit": dict(
        label="Verbindlichkeit (Kredit)", kind=PositionKind.LIABILITY, asset_class=None,
        liquid_default=False, retirement_default=False,
    ),
}


def _redirect(ok: str | None = None, error: str | None = None) -> RedirectResponse:
    from urllib.parse import urlencode
    q = urlencode({k: v for k, v in (("ok", ok), ("error", error)) if v})
    return RedirectResponse(url="/positions" + (f"?{q}" if q else ""), status_code=303)


def _preset_key(position: Position) -> str:
    """Passender Typ zu einer bestehenden Position (für die Vorauswahl beim Bearbeiten)."""
    for key, p in PRESETS.items():
        if p["kind"] == position.kind and p["asset_class"] == position.asset_class:
            return key
    return "sonstiges"


def _name_taken(session: Session, name: str, exclude_id: int | None = None) -> Position | None:
    found = session.execute(select(Position).where(Position.name == name)).scalar_one_or_none()
    return found if found is not None and found.id != exclude_id else None


@router.get("")
def list_positions(request: Request, session: Session = Depends(get_session)):
    positions = (
        session.execute(
            select(Position)
            .where(Position.is_active.is_(True))
            .order_by(Position.kind, Position.name)
        )
        .scalars()
        .all()
    )
    hidden = (
        session.execute(select(Position).where(Position.is_active.is_(False)).order_by(Position.name))
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "positions.html",
        {
            "positions": positions,
            "hidden": hidden,
            "presets": PRESETS,
            "preset_key": _preset_key,
            "class_labels": CLASS_LABELS,
            "ok": request.query_params.get("ok"),
            "error": request.query_params.get("error"),
            "preset_defaults": {
                key: {"liquid": p["liquid_default"], "retirement": p["retirement_default"]}
                for key, p in PRESETS.items()
            },
        },
    )


@router.post("")
def create_position(
    name: str = Form(...),
    preset: str = Form(...),
    is_liquid: bool = Form(False),
    retirement_relevant: bool = Form(False),
    is_single_security: bool = Form(False),
    session: Session = Depends(get_session),
):
    if preset not in PRESETS:
        raise HTTPException(status_code=400, detail=f"Unbekannter Typ: {preset}")
    name = name.strip()
    if not name:
        return _redirect(error="Bitte einen Namen eingeben.")
    taken = _name_taken(session, name)
    if taken is not None:
        hint = " – sie ist ausgeblendet und lässt sich unten wieder einblenden" if not taken.is_active else ""
        return _redirect(error=f"Eine Position „{name}“ gibt es schon{hint}.")
    p = PRESETS[preset]
    session.add(Position(
        name=name,
        kind=p["kind"],
        asset_class=p["asset_class"],
        is_liquid=is_liquid,
        retirement_relevant=retirement_relevant,
        is_single_security=is_single_security,
    ))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return _redirect(error=f"Eine Position „{name}“ gibt es schon.")
    return _redirect(ok=f"„{name}“ angelegt. Werte trägst du unter Jahresupdate oder „Stichtag erfassen“ ein.")


@router.post("/{position_id}/edit")
def edit_position(
    position_id: int,
    name: str = Form(...),
    preset: str = Form(...),
    is_liquid: bool = Form(False),
    retirement_relevant: bool = Form(False),
    is_single_security: bool = Form(False),
    session: Session = Depends(get_session),
):
    position = session.get(Position, position_id)
    if position is None:
        return _redirect(error="Position nicht gefunden.")
    if preset not in PRESETS:
        raise HTTPException(status_code=400, detail=f"Unbekannter Typ: {preset}")
    name = name.strip()
    if not name:
        return _redirect(error="Bitte einen Namen eingeben.")
    if _name_taken(session, name, exclude_id=position.id) is not None:
        return _redirect(error=f"Eine andere Position heißt schon „{name}“.")
    p = PRESETS[preset]
    position.name = name
    position.kind = p["kind"]
    position.asset_class = p["asset_class"]
    position.is_liquid = is_liquid
    position.retirement_relevant = retirement_relevant
    position.is_single_security = is_single_security
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return _redirect(error=f"Eine andere Position heißt schon „{name}“.")
    return _redirect(ok=f"„{name}“ gespeichert.")


@router.post("/{position_id}/deactivate")
def deactivate_position(position_id: int, session: Session = Depends(get_session)):
    position = session.get(Position, position_id)
    if position is not None:
        position.is_active = False
        session.commit()
        return _redirect(ok=f"„{position.name}“ ausgeblendet. Unten lässt sie sich wieder einblenden.")
    return _redirect()


@router.post("/{position_id}/activate")
def activate_position(position_id: int, session: Session = Depends(get_session)):
    position = session.get(Position, position_id)
    if position is not None:
        position.is_active = True
        session.commit()
        return _redirect(ok=f"„{position.name}“ wieder eingeblendet.")
    return _redirect()
