"""Gesamtübersicht: Positionen (Stichtage) und Depots (live) in einer Sicht.

Schnittstelle zwischen Depotmodul und Tracker:
- Depots werden aus Transaktionen und Kursen berechnet – heute und für jeden
  vergangenen Stichtag. Sie müssen nicht mehr als Stichtag abgetippt werden.
- Positionen, die per „Stichtag übernehmen“ mit einem Depot verknüpft sind
  (DepotPositionLink), werden ausgelassen, damit nichts doppelt zählt.
- Ausgeblendete Positionen (is_active = False) zählen weder heute noch im Verlauf.

Rhythmus: jährlich zum 31.12. (due_year_end); Quartalsstichtage bleiben möglich.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import depot as calc
from app.models import (
    AssetClass,
    Depot,
    DepotPositionLink,
    DepotSettings,
    DepotTransaction,
    ExpenseRecord,
    PensionContract,
    PensionSnapshot,
    PensionType,
    Position,
    PositionKind,
    Profile,
    Security,
    SecurityPrice,
    Snapshot,
)

CLASS_LABELS = {
    **calc.ASSET_CLASS_LABELS,
    AssetClass.LIQUIDITY: "Liquidität",
    AssetClass.PENSION: "Altersvorsorge-Verträge",
}


def year_end(year: int) -> date:
    return date(year, 12, 31)


def linked_position_ids(session: Session) -> set[int]:
    return set(session.execute(select(DepotPositionLink.position_id)).scalars())


def manual_positions(session: Session) -> list[Position]:
    """Aktive Positionen, die von Hand gepflegt werden (ohne Depot-Verknüpfung)."""
    linked = linked_position_ids(session)
    rows = session.execute(
        select(Position).where(Position.is_active.is_(True)).order_by(Position.kind, Position.name)
    ).scalars()
    return [p for p in rows if p.id not in linked]


def due_year_end(session: Session, today: date | None = None) -> date:
    """Der nächste offene Jahresstichtag.

    Ist zum 31.12. des Vorjahrs noch nicht für alle Positionen ein Wert erfasst, ist er
    fällig; sonst der 31.12. des laufenden Jahres (zu erfassen ab Januar).
    """
    today = today or date.today()
    last = year_end(today.year - 1)
    ids = [p.id for p in manual_positions(session)]
    if not ids:
        return last
    done = set(session.execute(
        select(Snapshot.position_id).where(Snapshot.snapshot_date == last, Snapshot.position_id.in_(ids))
    ).scalars())
    return last if len(done) < len(ids) else year_end(today.year)


# --------------------------------------------------------------------------- Depots


@dataclass
class DepotBook:
    depots: list[Depot]
    txs: dict[int, list]
    book: calc.PriceBook
    base_rate: float

    def value_by_class(self, as_of: date, depot_id: int | None = None) -> dict[AssetClass, int]:
        out: dict[AssetClass, int] = defaultdict(int)
        for d in self.depots:
            if depot_id is not None and d.id != depot_id:
                continue
            for cls, v in calc.value_by_class_cent(self.txs.get(d.id, []), self.book, as_of).items():
                out[cls] += v
        return dict(out)

    def value(self, as_of: date, depot_id: int | None = None) -> int:
        return sum(self.value_by_class(as_of, depot_id).values())


def depot_book(session: Session) -> DepotBook:
    depots = list(session.execute(select(Depot).where(Depot.is_active.is_(True)).order_by(Depot.name)).scalars())
    txs = list(session.execute(
        select(DepotTransaction).options(selectinload(DepotTransaction.security))
        .where(DepotTransaction.depot_id.in_([d.id for d in depots]))
    ).scalars()) if depots else []
    by_depot: dict[int, list] = defaultdict(list)
    for t in txs:
        by_depot[t.depot_id].append(t)
    settings = session.get(DepotSettings, 1)
    return DepotBook(depots, by_depot, calc.PriceBook(session.execute(select(SecurityPrice)).scalars(), txs),
                     settings.base_rate if settings else 0.032)


# --------------------------------------------------------------------------- Übersicht


@dataclass
class Row:
    name: str
    is_liability: bool
    asset_class: AssetClass | None
    source: str                 # "depot" | "snapshot" | "none"
    as_of: date | None
    value_cent: int
    prev_cent: int | None       # Wert zum letzten Jahresstichtag
    href: str
    is_liquid: bool = False
    stale: bool = False
    detail: str = ""

    @property
    def signed_cent(self) -> int:
        return -self.value_cent if self.is_liability else self.value_cent

    @property
    def change_cent(self) -> int | None:
        return None if self.prev_cent is None else self.value_cent - self.prev_cent


@dataclass
class Overview:
    today: date
    reference: date                                # letzter vergangener Jahresstichtag
    rows: list[Row]
    series: list[tuple[date, int]]                 # Nettovermögen je Stichtag + heute
    by_class: list[tuple[str, int]]
    assets_cent: int
    liabilities_cent: int
    liquid_cent: int
    depot_cent: int
    reference_net_cent: int | None
    monthly_expenses_cent: int
    expenses_source: str
    emergency_target_months: float
    hints: list[calc.Hint] = field(default_factory=list)

    @property
    def net_cent(self) -> int:
        return self.assets_cent - self.liabilities_cent

    @property
    def change_cent(self) -> int | None:
        return None if self.reference_net_cent is None else self.net_cent - self.reference_net_cent

    @property
    def change_ratio(self) -> float | None:
        if not self.reference_net_cent:
            return None
        return self.change_cent / abs(self.reference_net_cent)

    @property
    def liquid_months(self) -> float | None:
        return self.liquid_cent / self.monthly_expenses_cent if self.monthly_expenses_cent else None


def _snapshot_at(session: Session, position_id: int, d: date, exact: bool = False) -> Snapshot | None:
    stmt = select(Snapshot).where(Snapshot.position_id == position_id)
    stmt = stmt.where(Snapshot.snapshot_date == d) if exact else stmt.where(Snapshot.snapshot_date <= d)
    return session.execute(stmt.order_by(Snapshot.snapshot_date.desc()).limit(1)).scalar_one_or_none()


def build_overview(session: Session, today: date | None = None) -> Overview:
    today = today or date.today()
    reference = year_end(today.year - 1)
    profile = session.get(Profile, 1) or Profile(emergency_months_target=6.0, desired_income_monthly_cent=0)
    db = depot_book(session)
    positions = manual_positions(session)

    rows: list[Row] = []
    for p in positions:
        # Aktueller Wert: jüngster erfasster Stand – auch wenn er auf ein nahes Stichtagsdatum
        # (z. B. Quartalsende) vordatiert wurde.
        snap = _snapshot_at(session, p.id, date.max)
        prev = _snapshot_at(session, p.id, reference)
        rows.append(Row(
            name=p.name, is_liability=p.kind == PositionKind.LIABILITY, asset_class=p.asset_class,
            source="snapshot" if snap else "none", as_of=snap.snapshot_date if snap else None,
            value_cent=snap.value_cent if snap else 0, prev_cent=prev.value_cent if prev else None,
            href="/positions", is_liquid=p.is_liquid,
            stale=bool(snap and (today - snap.snapshot_date).days > 400),
        ))
    for d in db.depots:
        v = db.value(today, d.id)
        prev = db.value(reference, d.id)
        classes = db.value_by_class(today, d.id)
        rows.append(Row(
            name=d.name, is_liability=False,
            asset_class=max(classes, key=classes.get) if classes else AssetClass.EQUITIES,
            source="depot", as_of=today, value_cent=v, prev_cent=prev if prev else None,
            href=f"/depots/{d.id}", detail=", ".join(CLASS_LABELS.get(c, c.value) for c in classes) or "leer",
        ))

    assets = sum(r.value_cent for r in rows if not r.is_liability)
    liabilities = sum(r.value_cent for r in rows if r.is_liability)
    liquid = sum(r.value_cent for r in rows if r.is_liquid and not r.is_liability)
    depot_total = sum(r.value_cent for r in rows if r.source == "depot")

    by_class: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.is_liability or r.source == "depot":
            continue
        by_class[CLASS_LABELS.get(r.asset_class, "Sonstiges")] += r.value_cent
    for cls, v in db.value_by_class(today).items():
        by_class[CLASS_LABELS.get(cls, cls.value)] += v

    # Verlauf: alle erfassten Stichtage und alle Jahresenden seit der ersten Depotbuchung.
    # Positionen: letzter bekannter Wert zum Datum (fortgeschrieben, falls ein Jahr fehlt);
    # Depots: aus Transaktionen und Kursen zum Datum berechnet.
    ids = [p.id for p in positions]
    kinds = {p.id: p.kind for p in positions}
    history: dict[int, list[tuple[date, int]]] = defaultdict(list)
    if ids:
        for snap in session.execute(select(Snapshot).where(Snapshot.position_id.in_(ids))
                                    .order_by(Snapshot.snapshot_date)).scalars():
            history[snap.position_id].append((snap.snapshot_date, snap.value_cent))
    dates = {d for lst in history.values() for d, _ in lst if d < today}
    first_tx = min((t.trade_date for lst in db.txs.values() for t in lst), default=None)
    if first_tx:
        dates |= {year_end(y) for y in range(first_tx.year, today.year) if year_end(y) < today}

    def net_at(d: date) -> int:
        total = 0
        for pid, lst in history.items():
            known = [v for sd, v in lst if sd <= d]
            if known:
                total += -known[-1] if kinds[pid] == PositionKind.LIABILITY else known[-1]
        return total + db.value(d)

    series = [(d, net_at(d)) for d in sorted(dates)] + [(today, assets - liabilities)]
    while len(series) > 1 and series[0][1] == 0:   # leere Anfangsjahre (z. B. vor dem ersten Kauf) weglassen
        series.pop(0)
    has_reference = any(sd <= reference for lst in history.values() for sd, _ in lst) or bool(first_tx and first_tx <= reference)
    reference_net = net_at(reference) if has_reference else None

    record = session.execute(select(ExpenseRecord).order_by(ExpenseRecord.snapshot_date.desc()).limit(1)).scalar_one_or_none()
    if record:
        expenses, source = record.monthly_expenses_cent, f"Ausgaben laut Jahresupdate {record.snapshot_date:%Y}"
    else:
        expenses, source = profile.desired_income_monthly_cent, "Wunscheinkommen als Näherung"

    ov = Overview(today, reference, rows, series, sorted(by_class.items(), key=lambda kv: -kv[1]),
                  assets, liabilities, liquid, depot_total, reference_net, expenses, source,
                  profile.emergency_months_target)
    ov.hints = _hints(session, ov, db)
    return ov


def _hints(session: Session, ov: Overview, db: DepotBook) -> list[calc.Hint]:
    out: list[calc.Hint] = []
    due = due_year_end(session, ov.today)
    if due < ov.today:
        out.append(calc.Hint("warn", f"Jahresupdate {due.year} offen",
                             f"Für den 31.12.{due.year} fehlen noch Werte – unter „Jahresupdate“ Schritt für Schritt nachtragen."))
    months = ov.liquid_months
    if months is not None and months < ov.emergency_target_months:
        out.append(calc.Hint("warn", "Liquiditätsreserve",
                             f"Liquide Mittel reichen für {months:.1f} Monate, Ziel {ov.emergency_target_months:.0f} Monate "
                             f"(Basis: {ov.expenses_source}).".replace(".", ",", 1)))
    stale = [r.name for r in ov.rows if r.stale]
    if stale:
        out.append(calc.Hint("info", "Alte Werte", f"Älter als ein Jahr: {', '.join(stale)}."))
    missing = [r.name for r in ov.rows if r.source == "none"]
    if missing:
        out.append(calc.Hint("info", "Ohne Wert", f"Noch nie erfasst: {', '.join(missing)}."))
    estimated = sum(1 for lst in db.txs.values() for t in lst if t.note and t.note.startswith("Einlieferung –"))
    if estimated:
        out.append(calc.Hint("warn", "Depot: Einstand fehlt",
                             f"{estimated} Einlieferung(en) mit geschätztem Einstand – im Depot unter Transaktionen korrigieren."))
    held = {sid for lst in db.txs.values() for sid, h in calc.holdings(lst, ov.today).items() if h.quantity > 0}
    if held:
        stale_prices = [sid for sid in held if (db.book.at(sid, ov.today)[1] or date.min) < ov.today - timedelta(days=400)]
        if stale_prices:
            out.append(calc.Hint("info", "Depot: Kurse veraltet",
                                 f"{len(stale_prices)} Wertpapier(e) mit Kurs älter als ein Jahr – unter Depots → Kurse aktualisieren."))
    for pos_name, depot_name in possible_double_counts(ov.rows, db.depots):
        out.append(calc.Hint("warn", "Mögliche Doppelzählung",
                             f"Die Position „{pos_name}“ ähnelt dem Depot „{depot_name}“ und zählt zusätzlich zu dessen Live-Wert. "
                             f"Ist es Bargeld auf dem Verrechnungskonto, unter „Positionen“ umbenennen (z. B. „Verrechnungskonto {depot_name}“); "
                             f"sonst ausblenden oder per „Stichtag übernehmen“ mit dem Depot verknüpfen."))
    if not out:
        out.append(calc.Hint("ok", "Alles aktuell", "Keine offenen Punkte."))
    # Dringendes zuerst: Warnungen vor Hinweisen (Reihenfolge innerhalb der Stufe bleibt).
    out.sort(key=lambda h: {"warn": 0, "info": 1, "ok": 2}.get(h.level, 3))
    return out


def possible_double_counts(rows: list[Row], depots: list[Depot]) -> list[tuple[str, str]]:
    """Von Hand gepflegte Vermögenspositionen, deren Name den Namen eines Depots enthält (oder umgekehrt).

    Beispiel: Position „Depot Trade Republic“ neben dem Depot „Trade Republic“. Die App kann nicht wissen, ob es
    das Verrechnungskonto oder das Depot selbst ist – deshalb nur ein Hinweis. Namen unter 4 Zeichen werden
    ignoriert, um Zufallstreffer zu vermeiden; „Verrechnungskonto …“ gilt als bewusst benannt.
    """
    found: list[tuple[str, str]] = []
    for r in rows:
        if r.source != "snapshot" or r.is_liability:
            continue
        pos = r.name.casefold().strip()
        if "verrechnung" in pos:
            continue
        for d in depots:
            dep = d.name.casefold().strip()
            if len(dep) >= 4 and len(pos) >= 4 and (dep in pos or pos in dep):
                found.append((r.name, d.name))
                break
    return found


# --------------------------------------------------------------------------- Jahresupdate


@dataclass
class Step:
    key: str
    title: str
    done: bool
    detail: str
    href: str
    optional: bool = False


def year_steps(session: Session, target: date) -> list[Step]:
    """Checkliste für den Jahresstichtag target (31.12.)."""
    db = depot_book(session)
    steps: list[Step] = []

    # 1. Kurse der Depots zum Stichtag
    held: set[int] = set()
    for lst in db.txs.values():
        held |= {sid for sid, h in calc.holdings(lst, target).items() if h.quantity > 0}
    have = set(session.execute(
        select(SecurityPrice.security_id).where(SecurityPrice.price_date.between(target - timedelta(days=10), target))
    ).scalars())
    missing = held - have
    names = [s.name for s in session.execute(select(Security).where(Security.id.in_(missing))).scalars()] if missing else []
    steps.append(Step("prices", f"Depotkurse zum {target:%d.%m.%Y}", not missing,
                      "alle Wertpapiere haben einen Kurs" if held and not missing else
                      ("keine Wertpapiere im Bestand" if not held else f"fehlt: {', '.join(names)}"),
                      f"/depots/prices?price_date={target.isoformat()}"))

    # 2. Übrige Positionen (Konten, Immobilie, Kredite …)
    positions = manual_positions(session)
    done_ids = set(session.execute(
        select(Snapshot.position_id).where(Snapshot.snapshot_date == target)
    ).scalars())
    open_pos = [p.name for p in positions if p.id not in done_ids]
    steps.append(Step("positions", "Konten, Immobilien und Kredite", not open_pos,
                      "alle erfasst" if not open_pos else f"offen: {', '.join(open_pos)}",
                      f"/snapshots/new?snapshot_date={target.isoformat()}"))

    # 3. Monatliche Ausgaben (für Liquiditätsreserve)
    exp = session.get(ExpenseRecord, target)
    steps.append(Step("expenses", "Monatliche Ausgaben", exp is not None,
                      "erfasst" if exp else "für die Liquiditätsreserve (Notgroschen)", "#expenses", optional=True))

    # 4. Renteninformation des Jahres
    since = date(target.year, 1, 1)
    drv = session.execute(
        select(func.max(PensionSnapshot.as_of)).join(PensionContract)
        .where(PensionContract.pension_type == PensionType.STATUTORY, PensionContract.is_active.is_(True))
    ).scalar()
    steps.append(Step("pension", f"Renteninformation {target.year}", bool(drv and drv >= since),
                      f"letzter Stand {drv:%d.%m.%Y}" if drv else "noch nicht erfasst", "/pensions"))

    # 5. Basiszins für das Folgejahr (erscheint im Januar, BMF)
    s = session.get(DepotSettings, 1)
    year = target.year + 1
    ok = bool(s and s.base_rate_year >= year)
    steps.append(Step("base_rate", f"Basiszins {year} (Vorabpauschale)", ok,
                      "aktuell" if ok else f"veröffentlicht das BMF Anfang Januar {year}", "/depots/assumptions",
                      optional=target >= date.today()))
    return steps
