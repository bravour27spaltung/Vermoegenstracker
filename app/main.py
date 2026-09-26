from __future__ import annotations

import mimetypes

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.routes import dashboard, depots, expenses, fire, pensions, positions, snapshots, yearly
from app.security import security_middleware

mimetypes.add_type("application/manifest+json", ".webmanifest")

# Keine öffentliche API-Doku: die App ist nur für dich (siehe app/security.py).
app = FastAPI(title="Vermögenstracker", docs_url=None, redoc_url=None, openapi_url=None)
app.middleware("http")(security_middleware)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(positions.router)
app.include_router(snapshots.router)
app.include_router(dashboard.router)
app.include_router(pensions.router)
app.include_router(depots.router)
app.include_router(fire.router)
app.include_router(yearly.router)
app.include_router(expenses.router)


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")
