"""Throwaway databases for tests and autogenerate.

Postgres: created and dropped through an admin connection on a throwaway server, never the real one.
OMARCHY_DB_TEST_ADMIN_URL names it (in CI the service container, see .github/workflows); unset, a private server is
started for the process (omarchy_db.testserver). A server that holds the shared database (`omarchy`) is refused:
that is the real one.
"""
import os
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager

from sqlalchemy import create_engine, text

from .config import normalise_url


class RealServerRefused(RuntimeError):
    """The test admin URL points at the real server (it holds the shared database)."""


_checked: set[str] = set()


def admin_url() -> str:
    """Admin URL of the throwaway server for test and scratch databases (starts the private one if needed)."""
    from . import testserver
    url = normalise_url(os.environ.get("OMARCHY_DB_TEST_ADMIN_URL") or testserver.url())
    if url not in _checked:
        _refuse_real_server(url)
        _checked.add(url)
    return url


def _refuse_real_server(url: str):
    from .config import DATABASE
    eng = create_engine(url, pool_size=1)
    try:
        with eng.connect() as c:
            real = c.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": DATABASE}).scalar()
    finally:
        eng.dispose()
    if real:
        raise RealServerRefused(
            f"OMARCHY_DB_TEST_ADMIN_URL points at a server that holds the shared database {DATABASE!r}: test and "
            "scratch databases never go on the real server. Unset it (a private throwaway server is started) or "
            "point it at a throwaway one.")


def live_admin_url() -> str:
    """Admin URL for dbtool new-app on the real server: OMARCHY_DB_ADMIN_URL, else you over the local socket.
    Under tests it is the throwaway server instead."""
    from .guard import testing
    if testing():
        return admin_url()
    return normalise_url(os.environ.get("OMARCHY_DB_ADMIN_URL", "postgresql+psycopg:///postgres"))


def _admin():
    return create_engine(admin_url(), isolation_level="AUTOCOMMIT", pool_size=1)


def pg_available() -> bool:
    """A throwaway Postgres can be had: OMARCHY_DB_TEST_ADMIN_URL reachable, or server binaries for a private one."""
    from . import testserver
    if not os.environ.get("OMARCHY_DB_TEST_ADMIN_URL") and not testserver.available():
        return False
    try:
        eng = create_engine(admin_url(), pool_size=1)
        with eng.connect() as c: c.execute(text("SELECT 1"))
        eng.dispose(); return True
    except RealServerRefused:
        raise
    except Exception:
        return False


def pg_url(dbname: str) -> str:
    u = create_engine(admin_url()).url.set(database=dbname)
    return u.render_as_string(hide_password=False)


def create_pg(name: str, template: str | None = None) -> str:
    eng = _admin()
    with eng.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else "")))
    eng.dispose()
    return pg_url(name)


def drop_pg(name: str):
    from .guard import is_throwaway
    if not is_throwaway(name):
        raise ValueError(f"drop_pg only drops throwaway databases (omdb_*), not {name!r}")
    eng = _admin()
    with eng.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    eng.dispose()


def unique(prefix: str) -> str:
    return f"{prefix}_{os.getpid()}_{uuid.uuid4().hex[:8]}"


@contextmanager
def scratch_database(postgres: bool, prefix: str = "omdb_scratch"):
    """URL of an empty database that is gone afterwards: a temp SQLite file, or a new Postgres database."""
    if postgres:
        name = unique(prefix)
        url = create_pg(name)
        try: yield url
        finally: drop_pg(name)
    else:
        with tempfile.TemporaryDirectory() as d:
            yield "sqlite:///" + os.path.join(d, "scratch.db")


def sqlite_copy_to_memory(path: str) -> sqlite3.Connection:
    """An in-memory SQLite connection holding a copy of the file (fast per-test clone of a migrated template)."""
    src = sqlite3.connect(path)
    mem = sqlite3.connect(":memory:", check_same_thread=False)
    src.backup(mem); src.close()
    return mem
