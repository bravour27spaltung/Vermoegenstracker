"""Zentrale DB-Session für die Web-App. Ein Engine/Session-Factory-Paar pro Prozess."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from app.db import init_db, make_engine, make_session_factory

_engine = make_engine()
init_db(_engine)
_SessionFactory = make_session_factory(_engine)


def get_session() -> Iterator[Session]:
    session = _SessionFactory()
    try:
        yield session
    finally:
        session.close()
