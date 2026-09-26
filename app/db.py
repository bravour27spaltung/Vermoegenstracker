from __future__ import annotations

import enum
import os
from pathlib import Path
from typing import Optional

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base

DEFAULT_URL = "sqlite:///data/tracker.db"


def make_engine(url: Optional[str] = None) -> Engine:
    url = url or os.environ.get("DATABASE_URL", DEFAULT_URL)
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA foreign_keys=ON")  # in SQLite standardmäßig AUS
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    return engine


def init_db(engine: Engine) -> None:
    """Legt fehlende Tabellen an und ergänzt fehlende Spalten in bestehenden Tabellen.

    Die Spaltenergänzung deckt nur den einfachsten Fall ab (neue Spalte mit Standardwert).
    Umbenennen, Löschen oder neue Constraints brauchen eine echte Migration (Alembic).
    """
    if engine.url.get_backend_name() == "sqlite" and engine.url.database not in (None, ":memory:"):
        Path(engine.url.database).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)


def _sql_default(column) -> Optional[str]:
    default = column.default.arg if column.default is not None else None
    if default is None or callable(default):
        return None
    if isinstance(default, enum.Enum):
        return f"'{default.name}'"  # SQLAlchemy speichert Enum-Namen
    if isinstance(default, bool):
        return "1" if default else "0"
    if isinstance(default, (int, float)):
        return repr(default)
    return "'" + str(default).replace("'", "''") + "'"


def _add_missing_columns(engine: Engine) -> None:
    existing_tables = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {c["name"] for c in inspect(conn).get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type.compile(engine.dialect)}'
                default = _sql_default(column)
                if default is not None:
                    ddl += f" DEFAULT {default}"
                conn.execute(text(ddl))


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
