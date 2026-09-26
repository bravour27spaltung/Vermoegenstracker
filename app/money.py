"""Geldbeträge werden ausschließlich als ganze Cent (int) gespeichert und gerechnet.

float ist für Geld ungeeignet: Rundungsfehler summieren sich über viele Perioden.
Dieses Modul hat bewusst keine Abhängigkeiten (nur Standardbibliothek).
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def to_cents(value: Decimal | int | str) -> int:
    """Wandelt einen Betrag in Cent um (kaufmännisch gerundet, ROUND_HALF_UP).

    Erlaubt sind Decimal, int und Strings im Format '1234.56'.
    float wird abgelehnt, weil der Wert dann schon vor der Umrechnung ungenau ist.
    """
    if isinstance(value, float):
        raise TypeError("float ist für Geldbeträge nicht erlaubt; Decimal oder str verwenden.")
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"Kein gültiger Betrag: {value!r}") from exc
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_german_amount(text: str) -> int:
    """Liest deutsche Schreibweise ('1.234,56 €', '-0,99', '1.234') und liefert Cent.

    Punkt = Tausendertrenner, Komma = Dezimaltrenner. '1.234' bedeutet also 1234 Euro.
    """
    s = text.strip().replace("€", "").replace("\u00a0", "").replace(" ", "")
    if not s:
        raise ValueError("Leerer Betrag")
    return to_cents(s.replace(".", "").replace(",", "."))


def parse_flexible_amount(text: str) -> int:
    """Wie parse_german_amount, akzeptiert aber zusätzlich den Punkt als Dezimaltrenner,
    wenn eindeutig: '1631.60' oder '12.5' (ein Punkt, danach 1-2 Ziffern, kein Komma).

    '1.234' bleibt 1234 Euro (drei Ziffern nach dem Punkt = Tausendertrenner).
    """
    s = text.strip().replace("€", "").replace("\u00a0", "").replace(" ", "")
    head, dot, tail = s.rpartition(".")
    if dot and "," not in s and "." not in head and 1 <= len(tail) <= 2 and tail.isdigit():
        return to_cents(s)
    return parse_german_amount(s)


def format_euro(cents: int) -> str:
    """Formatiert Cent als deutschen Euro-Betrag, z. B. -123456 -> '-1.234,56 €'."""
    sign = "-" if cents < 0 else ""
    euros, rest = divmod(abs(cents), 100)
    return f"{sign}{euros:,}".replace(",", ".") + f",{rest:02d} €"
