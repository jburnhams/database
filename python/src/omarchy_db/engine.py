"""Engines and sessions."""
import re
import sqlite3
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .config import normalise_url, url_for


def connect(app_or_url: str, **kw) -> Engine:
    """Engine for an app name ("notes": see config.url_for) or a URL / SQLite path.
    SQLite gets foreign keys on, WAL and a busy timeout, so it behaves like the server in tests."""
    url = url_for(app_or_url) if re.fullmatch(r"[a-z][a-z0-9_-]*", app_or_url) else normalise_url(app_or_url)
    engine = create_engine(url, **kw)
    if engine.dialect.name == "sqlite":
        _sqlite_pragmas(engine)
    return engine


def _sqlite_pragmas(engine: Engine, wal: bool = True):
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _):
        if not isinstance(dbapi_conn, sqlite3.Connection): return
        c = dbapi_conn.cursor()
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA busy_timeout = 30000")
        if wal and engine.url.database not in (None, "", ":memory:"):
            c.execute("PRAGMA journal_mode = WAL")
        c.close()


@contextmanager
def session(engine: Engine, **kw):
    """A Session that is closed afterwards (commit yourself: s.commit())."""
    with Session(engine, **kw) as s:
        yield s
