"""Zugriffsschutz und Sicherheits-Header.

Betriebsmodell: Die App lauscht nur auf 127.0.0.1. Vom Mac aus ruft man sie direkt auf,
vom Handy über `tailscale serve` (privates, Ende-zu-Ende-verschlüsseltes Netz). Serve setzt
den Header `Tailscale-User-Login` und entfernt gleichnamige Header aus eingehenden
Anfragen, sodass er nicht gefälscht werden kann (Tailscale-Doku „Serve“, Identity headers).

Regeln (access_decision):
1. Anfrage über Tailscale Serve (Header vorhanden): nur Logins aus VT_ALLOWED_USERS.
   Ist die Liste leer, wird abgelehnt – so bekommen geteilte Geräte oder Gäste im Tailnet
   keinen Zugriff, nur weil sie im selben Netz sind.
2. Ohne Tailscale-Header: nur direkt vom eigenen Rechner (Loopback) und ohne Proxy-Header.
   Wird die App versehentlich mit --host 0.0.0.0 gestartet, bleibt sie für das WLAN gesperrt.
"""
from __future__ import annotations

import os

from starlette.requests import Request
from starlette.responses import HTMLResponse

# "testclient" ist der feste Hostname von Starlettes TestClient; echte Verbindungen tragen
# immer eine IP-Adresse und können ihn nicht annehmen.
LOCAL_HOSTS = {"127.0.0.1", "::1", "testclient"}
PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip")

CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; "
    "form-action 'self'; base-uri 'none'; object-src 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,          # keine Drittanbieter-Skripte, kein Einbetten
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def allowed_users() -> set[str]:
    return {u.strip().lower() for u in os.environ.get("VT_ALLOWED_USERS", "").split(",") if u.strip()}


def access_decision(client_host: str | None, headers, allowed: set[str]) -> tuple[bool, str]:
    login = (headers.get("tailscale-user-login") or "").strip().lower()
    if login:
        if login in allowed:
            return True, "tailscale"
        if not allowed:
            return False, "Zugriff über Tailscale ist noch nicht freigegeben (VT_ALLOWED_USERS ist leer)."
        return False, f"Das Tailscale-Konto {login} ist nicht freigegeben."
    if client_host in LOCAL_HOSTS and not any(h in headers for h in PROXY_HEADERS):
        return True, "local"
    return False, "Zugriff nur vom eigenen Rechner oder über Tailscale."


_DENIED = """<!doctype html><html lang="de"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Kein Zugriff</title>
<body style="font-family:system-ui;max-width:32rem;margin:3rem auto;padding:0 1rem">
<h1>Kein Zugriff</h1><p>{reason}</p>
<p><small>Freigabe: App mit <code>./start.sh</code> starten (liest dein Tailscale-Konto automatisch)
oder <code>VT_ALLOWED_USERS=deine@mail</code> setzen.</small></p></body></html>"""


async def security_middleware(request: Request, call_next):
    ok, reason = access_decision(request.client.host if request.client else None, request.headers, allowed_users())
    if not ok:
        return HTMLResponse(_DENIED.format(reason=reason), status_code=403, headers=SECURITY_HEADERS)
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    # Finanzdaten nicht im Browser-/Geräte-Cache ablegen (Handy-Verlust, geteilte Rechner)
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response
