"""Serverseitige SVG-Diagramme für die Depotseiten (keine JS-Bibliothek, kein CDN).

Farben über CSS-Klassen (templates/depots/_style.html). Hover: transparente Spalten mit
``data-tip``; ein kleines Skript in _style.html zeigt Crosshair und Tooltip.
"""
from __future__ import annotations

import html
import math
from datetime import date

W, H = 720, 280
PAD_L, PAD_R, PAD_T, PAD_B = 64, 16, 16, 30


def eur(x: float, stellen: int = 0) -> str:
    s = f"{x:,.{stellen}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} €"


def kurz_eur(x: float) -> str:
    a = abs(x)
    if a >= 1e6:
        return f"{x/1e6:.1f} Mio €".replace(".", ",")
    if a >= 1e3:
        return f"{x/1e3:.0f} T€"
    return f"{x:.0f} €"


def _nice_max(v: float, ticks: int = 4) -> float:
    """Achsenmaximum = 4 × „runder“ Schritt (1/2/2,5/5 × 10^n)."""
    if v <= 0:
        return 1
    roh = v / ticks
    exp = 10 ** math.floor(math.log10(roh))
    for m in (1, 2, 2.5, 5, 10):
        if roh <= m * exp:
            return m * exp * ticks
    return 10 * exp * ticks


def _y_axis(ymax, fy, ticks=4):
    out = []
    for i in range(ticks + 1):
        v = ymax * i / ticks
        y = fy(v)
        out.append(f'<line class="grid" x1="{PAD_L}" x2="{W-PAD_R}" y1="{y:.1f}" y2="{y:.1f}"/>')
        out.append(f'<text class="tick" x="{PAD_L-8}" y="{y+4:.1f}" text-anchor="end">{kurz_eur(v)}</text>')
    return out


def _path(xs, ys):
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))


def _hover_cols(xs, tips):
    out = []
    n = len(xs)
    for i, (x, tip) in enumerate(zip(xs, tips)):
        left = PAD_L if i == 0 else (xs[i - 1] + x) / 2
        right = W - PAD_R if i == n - 1 else (x + xs[i + 1]) / 2
        out.append(
            f'<rect class="hit" x="{left:.1f}" y="{PAD_T}" width="{max(right-left, 1):.1f}" '
            f'height="{H-PAD_T-PAD_B}" data-x="{x:.1f}" data-tip="{html.escape(tip)}"/>'
        )
    return out


def value_series_svg(series: list[tuple]) -> str:
    """series: [(datum, wert_cent, netto_einzahlungen_cent)]"""
    punkte = [{"datum": d.isoformat(), "wert": v / 100, "netto": n / 100} for d, v, n in series]
    if len(punkte) < 2:
        return '<p class="muted">Noch zu wenige Datenpunkte für einen Verlauf – Kurse zu mehreren Stichtagen erfassen.</p>'
    ds = [date.fromisoformat(p["datum"]) for p in punkte]
    t0, t1 = ds[0].toordinal(), ds[-1].toordinal()
    span = max(t1 - t0, 1)
    ymax = _nice_max(max(max(p["wert"], p["netto"]) for p in punkte) * 1.05)
    fx = lambda dd: PAD_L + (dd.toordinal() - t0) / span * (W - PAD_L - PAD_R)
    fy = lambda v: H - PAD_B - v / ymax * (H - PAD_T - PAD_B)
    xs = [fx(x) for x in ds]
    wy = [fy(p["wert"]) for p in punkte]
    ny = [fy(p["netto"]) for p in punkte]
    base = fy(0)
    area = _path(xs, wy) + f" L{xs[-1]:.1f},{base:.1f} L{xs[0]:.1f},{base:.1f} Z"
    parts = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="Depotwert und Netto-Einzahlungen im Zeitverlauf">']
    parts += _y_axis(ymax, fy)
    # x-Achse: Jahre
    for jahr in range(ds[0].year, ds[-1].year + 2):
        dd = date(jahr, 1, 1)
        if ds[0] <= dd <= ds[-1] or (jahr == ds[0].year and len({x.year for x in ds}) == 1):
            dd = max(dd, ds[0])
            parts.append(f'<text class="tick" x="{fx(dd):.1f}" y="{H-8}" text-anchor="middle">{jahr}</text>')
    parts.append(f'<path class="area s1" d="{area}"/>')
    parts.append(f'<path class="line ref" d="{_path(xs, ny)}"/>')
    parts.append(f'<path class="line s1" d="{_path(xs, wy)}"/>')
    parts.append(f'<circle class="dot s1" cx="{xs[-1]:.1f}" cy="{wy[-1]:.1f}" r="4"/>')
    parts.append('<line class="crosshair" x1="0" x2="0" y1="%d" y2="%d"/>' % (PAD_T, H - PAD_B))
    tips = [
        f"{x.strftime('%d.%m.%Y')}|Depotwert: {eur(p['wert'])}|Netto eingezahlt: {eur(p['netto'])}|G/V: {eur(p['wert']-p['netto'])}"
        for x, p in zip(ds, punkte)
    ]
    parts += _hover_cols(xs, tips)
    parts.append("</svg>")
    return "".join(parts)


def forecast_svg(pg: dict, nominal: bool = False, inflation: float = 2.0) -> str:
    global W, H
    alt = (W, H)
    W, H = 1100, 300
    try:
        return _forecast_svg(pg, nominal, inflation)
    finally:
        W, H = alt


def _forecast_svg(pg: dict, nominal: bool, inflation: float) -> str:
    jahre = pg["jahre"]
    fak = [(1 + inflation / 100) ** j if nominal else 1 for j in jahre]
    ser = {k: [v * f for v, f in zip(pg[k], fak)] for k in ("p10", "p25", "p50", "p75", "p90", "einzahlungen")}
    ser["einzahlungen"] = pg["einzahlungen"] if not nominal else [
        # Einzahlungen nominal: Sparrate dynamisiert → Summe der nominalen Raten
        pg["einzahlungen"][0] + sum((pg["einzahlungen"][1] - pg["einzahlungen"][0]) / 12 * (1 + inflation / 100) ** (m / 12)
                                    for m in range(1, j * 12 + 1))
        for j in jahre
    ]
    ymax = _nice_max(max(ser["p90"]) * 1.05)
    n = len(jahre) - 1 or 1
    fx = lambda j: PAD_L + j / n * (W - PAD_L - PAD_R)
    fy = lambda v: H - PAD_B - v / ymax * (H - PAD_T - PAD_B)
    xs = [fx(j) for j in jahre]

    def band(lo, hi):
        up = [fy(v) for v in ser[hi]]
        dn = [fy(v) for v in ser[lo]]
        return _path(xs, up) + " L" + " L".join(f"{x:.1f},{y:.1f}" for x, y in zip(reversed(xs), reversed(dn))) + " Z"

    parts = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="Prognosekorridor">']
    parts += _y_axis(ymax, fy)
    step = 5 if n > 12 else (2 if n > 6 else 1)
    for j in jahre:
        if j % step == 0:
            parts.append(f'<text class="tick" x="{fx(j):.1f}" y="{H-8}" text-anchor="middle">{"heute" if j == 0 else f"+{j} J"}</text>')
    parts.append(f'<path class="band b1" d="{band("p10", "p90")}"/>')
    parts.append(f'<path class="band b2" d="{band("p25", "p75")}"/>')
    parts.append(f'<path class="line ref" d="{_path(xs, [fy(v) for v in ser["einzahlungen"]])}"/>')
    parts.append(f'<path class="line s1" d="{_path(xs, [fy(v) for v in ser["p50"]])}"/>')
    # Direktlabels am Ende
    xe = xs[-1]
    for k, lab in (("p90", "P90"), ("p50", "Median"), ("p10", "P10")):
        parts.append(f'<text class="dlabel" x="{xe-4:.1f}" y="{fy(ser[k][-1])-6:.1f}" text-anchor="end">{lab}</text>')
    parts.append('<line class="crosshair" x1="0" x2="0" y1="%d" y2="%d"/>' % (PAD_T, H - PAD_B))
    tips = [
        f"{'heute' if j == 0 else f'in {j} Jahren'}|P90: {eur(ser['p90'][i])}|Median: {eur(ser['p50'][i])}"
        f"|P10: {eur(ser['p10'][i])}|Eingezahlt: {eur(ser['einzahlungen'][i])}"
        for i, j in enumerate(jahre)
    ]
    parts += _hover_cols(xs, tips)
    parts.append("</svg>")
    return "".join(parts)


def fire_curve_svg(ages: list[int], success: list[float], target: float) -> str:
    """Erfolgswahrscheinlichkeit je FIRE-Alter (Linie) mit Zielwert als Referenzlinie."""
    if len(ages) < 2:
        return '<p class="muted">Zu wenige Jahre bis zum Rentenbeginn für eine Kurve.</p>'
    w, h, pl, pr, pt, pb = 900, 280, 48, 16, 16, 30
    fx = lambda a: pl + (a - ages[0]) / (ages[-1] - ages[0]) * (w - pl - pr)
    fy = lambda v: h - pb - v * (h - pt - pb)
    parts = [f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="Erfolgswahrscheinlichkeit nach FIRE-Alter">']
    for v in (0, 0.25, 0.5, 0.75, 1.0):
        parts.append(f'<line class="grid" x1="{pl}" x2="{w - pr}" y1="{fy(v):.1f}" y2="{fy(v):.1f}"/>')
        parts.append(f'<text class="tick" x="{pl - 8}" y="{fy(v) + 4:.1f}" text-anchor="end">{v * 100:.0f} %</text>')
    step = 5 if len(ages) > 15 else 2
    for a in ages:
        if a % step == 0 or a == ages[-1]:
            parts.append(f'<text class="tick" x="{fx(a):.1f}" y="{h - 8}" text-anchor="middle">{a}</text>')
    ty = fy(target)
    parts.append(f'<line class="line ref" x1="{pl}" x2="{w - pr}" y1="{ty:.1f}" y2="{ty:.1f}"/>')
    parts.append(f'<text class="dlabel" x="{pl + 6}" y="{ty - 6:.1f}">Ziel {target * 100:.0f} %</text>')
    xs = [fx(a) for a in ages]
    ys = [fy(v) for v in success]
    parts.append(f'<path class="area s1" d="{_path(xs, ys)} L{xs[-1]:.1f},{fy(0):.1f} L{xs[0]:.1f},{fy(0):.1f} Z"/>')
    parts.append(f'<path class="line s1" d="{_path(xs, ys)}"/>')
    first = next((i for i, v in enumerate(success) if v >= target), None)
    if first is not None:
        parts.append(f'<circle class="dot s1" cx="{xs[first]:.1f}" cy="{ys[first]:.1f}" r="5"/>')
        anchor = "start" if xs[first] < w * 0.7 else "end"
        dx = 8 if anchor == "start" else -8
        parts.append(f'<text class="dlabel" x="{xs[first] + dx:.1f}" y="{ys[first] + 16:.1f}" text-anchor="{anchor}">ab {ages[first]}</text>')
    parts.append(f'<line class="crosshair" x1="0" x2="0" y1="{pt}" y2="{h - pb}"/>')
    n = len(xs)
    for i, (x, a, v) in enumerate(zip(xs, ages, success)):
        left = pl if i == 0 else (xs[i - 1] + x) / 2
        right = w - pr if i == n - 1 else (x + xs[i + 1]) / 2
        tip = html.escape(f"Ausstieg mit {a}|Erfolgswahrscheinlichkeit: {v * 100:.0f} %".replace(".", ","))
        parts.append(f'<rect class="hit" x="{left:.1f}" y="{pt}" width="{max(right - left, 1):.1f}" height="{h - pt - pb}" '
                     f'data-x="{x:.1f}" data-tip="{tip}"/>')
    parts.append("</svg>")
    return "".join(parts)


def line_svg(points: list[tuple], label: str = "Nettovermögen") -> str:
    """Einfache Linie über Datum (points: [(datum, wert_cent)]); ersetzt Chart.js im Dashboard,
    damit keine Skripte von Drittanbietern (CDN) geladen werden."""
    if len(points) < 2:
        return '<p class="muted">Für einen Verlauf werden mindestens zwei Stichtage gebraucht.</p>'
    ds = [p[0] for p in points]
    vals = [p[1] / 100 for p in points]
    lo = min(0.0, min(vals))
    hi = _nice_max(max(vals) * 1.05) if max(vals) > 0 else 1
    t0, t1 = ds[0].toordinal(), ds[-1].toordinal()
    fx = lambda d: PAD_L + (d.toordinal() - t0) / max(t1 - t0, 1) * (W - PAD_L - PAD_R)
    fy = lambda v: H - PAD_B - (v - lo) / (hi - lo) * (H - PAD_T - PAD_B)
    xs, ys = [fx(d) for d in ds], [fy(v) for v in vals]
    parts = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="{html.escape(label)} im Zeitverlauf">']
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        parts.append(f'<line class="grid" x1="{PAD_L}" x2="{W-PAD_R}" y1="{fy(v):.1f}" y2="{fy(v):.1f}"/>')
        parts.append(f'<text class="tick" x="{PAD_L-8}" y="{fy(v)+4:.1f}" text-anchor="end">{kurz_eur(v)}</text>')
    marks = [date(y, 1, 1) for y in range(ds[0].year + 1, ds[-1].year + 1)]
    if len(marks) > 8:  # bei langen Zeiträumen nur jedes n-te Jahr beschriften
        step = -(-len(marks) // 8)
        marks = marks[::step]
    labels = [(d, str(d.year)) for d in marks] or [(ds[0], ds[0].strftime("%m/%Y")), (ds[-1], ds[-1].strftime("%m/%Y"))]
    for d, text in labels:
        parts.append(f'<text class="tick" x="{fx(d):.1f}" y="{H-8}" text-anchor="middle">{text}</text>')
    base = fy(max(lo, 0))
    parts.append(f'<path class="area s1" d="{_path(xs, ys)} L{xs[-1]:.1f},{base:.1f} L{xs[0]:.1f},{base:.1f} Z"/>')
    parts.append(f'<path class="line s1" d="{_path(xs, ys)}"/>')
    for x, y in zip(xs, ys):
        parts.append(f'<circle class="dot s1" cx="{x:.1f}" cy="{y:.1f}" r="3.5"/>')
    parts.append('<line class="crosshair" x1="0" x2="0" y1="%d" y2="%d"/>' % (PAD_T, H - PAD_B))
    parts += _hover_cols(xs, [f"{d.strftime('%d.%m.%Y')}|{label}: {eur(v)}" for d, v in zip(ds, vals)])
    parts.append("</svg>")
    return "".join(parts)
